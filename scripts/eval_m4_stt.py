#!/usr/bin/env python3
"""M4 한국어 STT 평가 — 고정 평가 세트(manifest.csv)로 CER/WER/키워드/지연 측정.

운영 코드(ai/experts/m4_whisper_small.py)의 WhisperSmallModel을 그대로 불러 같은 디코딩 경로로 잰다.
ai-experts 이미지 안에서 실행한다 (onnxruntime·optimum·transformers 필요).

    # RPi5 (~/safewave), 격리 측정 시 운영 ai-experts/ai-qwen은 먼저 정지
    docker compose run --rm --no-deps \\
      -v ./data/m4_eval_2398:/eval:ro -v ./scripts:/scripts:ro -v ./reports:/reports \\
      -e M4_ORT_THREADS=2 ai-experts \\
      python3 /scripts/eval_m4_stt.py --manifest /eval/manifest.csv \\
        --model /app/models/whisper_onnx --label base-fp32 --limit 200

지표 정의 (이대경 M4 인계 CT2_REFERENCE.md와 동일)
- CER: 소문자화, 공백·문장부호 제거 후 문자 편집 거리. 전체 (S+D+I) 합 / 전체 정답 길이.
- WER: 앞뒤 공백 제거 후 공백 분할(문장부호 유지). 전체 합산 방식.
- 키워드: matched_keyword(없으면 정답 전체)가 CER 정규화된 출력에 포함되는지.
정규화 규칙은 같지만 디코딩 경로(여기선 transformers pipeline greedy)가 CT2와 다르므로
CT2 수치와 절대 비교하지 말고, 이 도구로 잰 모델끼리 비교한다.

--limit 표본은 출력과 무관하게 실행 전에 고정된다: 직접 녹음 전부 + AI Hub를 라벨별 균등,
audio_sha256 정렬 순서로 선택. --ids-file로 같은 표본을 다른 모델에 재사용한다.
중간에 끊겨도 --out 폴더의 results.jsonl을 이어서 진행한다.
평가 세트는 개발 판단에 쓰인 세트라 최종 독립 테스트 결과로 보고하지 않는다.
"""
import argparse
import csv
import json
import os
import statistics
import sys
import time
import unicodedata
import wave
from array import array
from pathlib import Path


# ── 지표 ────────────────────────────────────────────────────────
def norm_chars(text):
    text = unicodedata.normalize("NFC", text or "").lower()
    return "".join(ch for ch in text if not ch.isspace() and not unicodedata.category(ch).startswith("P"))


def edit_ops(ref, hyp):
    """(대치, 삭제, 삽입) — 레벤슈타인 역추적."""
    n, m = len(ref), len(hyp)
    d = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        d[i][0] = i
    for j in range(m + 1):
        d[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (ref[i - 1] != hyp[j - 1]))
    s = de = ins = 0
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0 and d[i][j] == d[i - 1][j - 1] + (ref[i - 1] != hyp[j - 1]):
            s += ref[i - 1] != hyp[j - 1]
            i, j = i - 1, j - 1
        elif i > 0 and d[i][j] == d[i - 1][j] + 1:
            de, i = de + 1, i - 1
        else:
            ins, j = ins + 1, j - 1
    return s, de, ins


def score_row(ref, hyp, keyword):
    rc, hc = norm_chars(ref), norm_chars(hyp)
    rw, hw = (ref or "").strip().split(), (hyp or "").strip().split()
    cs, cd, ci = edit_ops(rc, hc)
    ws, wd, wi = edit_ops(rw, hw)
    kw = norm_chars(keyword or ref)
    return {"ref_chars": len(rc), "char_s": cs, "char_d": cd, "char_i": ci,
            "ref_words": len(rw), "word_s": ws, "word_d": wd, "word_i": wi,
            "keyword_hit": bool(kw) and kw in hc}


# ── 입력 ────────────────────────────────────────────────────────
def read_wav(path):
    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2 or w.getframerate() != 16000:
            raise ValueError(f"16kHz mono 16bit 아님: ch={w.getnchannels()} "
                             f"width={w.getsampwidth()} rate={w.getframerate()}")
        pcm = array("h")
        pcm.frombytes(w.readframes(w.getnframes()))
    if sys.byteorder == "big":
        pcm.byteswap()
    return pcm


def select_rows(rows, limit, ids_file):
    if ids_file:
        wanted = [line.strip() for line in Path(ids_file).read_text(encoding="utf-8").splitlines() if line.strip()]
        by_id = {r["audio_id"]: r for r in rows}
        return [by_id[i] for i in wanted]
    if not limit or limit >= len(rows):
        return rows
    direct = sorted((r for r in rows if r["evaluation_source"] != "aihub"), key=lambda r: r["audio_sha256"])
    if limit <= len(direct):
        return direct[:limit]  # 소량 확인용: 직접 녹음에서만
    rest = sorted((r for r in rows if r["evaluation_source"] == "aihub"), key=lambda r: r["audio_sha256"])
    labels = sorted({r["label"] for r in rest})
    per = max(0, limit - len(direct)) // max(1, len(labels))
    picked = list(direct)
    for lb in labels:
        picked += [r for r in rest if r["label"] == lb][:per]
    return picked


def pct(values, q):
    s = sorted(values)
    return s[min(len(s) - 1, int(round(q / 100 * (len(s) - 1))))] if s else None


def aggregate(items):
    if not items:
        return {"files": 0}
    t = {k: sum(x[k] for x in items) for k in
         ("ref_chars", "char_s", "char_d", "char_i", "ref_words", "word_s", "word_d", "word_i")}
    lat = [x["latency_s"] for x in items]
    rtf = [x["latency_s"] / x["duration_s"] for x in items if x["duration_s"]]
    return {
        "files": len(items),
        "cer": round((t["char_s"] + t["char_d"] + t["char_i"]) / t["ref_chars"], 4) if t["ref_chars"] else None,
        "wer": round((t["word_s"] + t["word_d"] + t["word_i"]) / t["ref_words"], 4) if t["ref_words"] else None,
        "keyword_hit_rate": round(sum(x["keyword_hit"] for x in items) / len(items), 4),
        "empty_outputs": sum(1 for x in items if not norm_chars(x["hyp"])),
        "non_whisper_source": sum(1 for x in items if x["source"] != "whisper-stt"),
        "latency_s": {"mean": round(statistics.fmean(lat), 3), "p50": round(pct(lat, 50), 3),
                      "p95": round(pct(lat, 95), 3), "max": round(max(lat), 3)},
        "rtf": {"mean": round(statistics.fmean(rtf), 3), "p95": round(pct(rtf, 95), 3)} if rtf else None,
        "audio_s_total": round(sum(x["duration_s"] for x in items), 1),
        **t,
    }


# ── 메인 ────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--model", required=True, help="Optimum Whisper ONNX 폴더")
    ap.add_argument("--label", required=True)
    ap.add_argument("--limit", type=int, default=0, help="0이면 전체")
    ap.add_argument("--ids-file", help="이 audio_id 목록만 같은 순서로 평가")
    ap.add_argument("--out", default="/reports/m4eval")
    ap.add_argument("--app", default="/app", help="ai 서비스 코드 위치 (experts/, utils/)")
    args = ap.parse_args()

    manifest = Path(args.manifest)
    with manifest.open(encoding="utf-8-sig", newline="") as f:
        rows = select_rows(list(csv.DictReader(f)), args.limit, args.ids_file)

    out = Path(args.out) / args.label
    out.mkdir(parents=True, exist_ok=True)
    (out / "ids.txt").write_text("\n".join(r["audio_id"] for r in rows) + "\n", encoding="utf-8")
    results_path = out / "results.jsonl"
    done = {}
    if results_path.exists():
        for line in results_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                done[rec["audio_id"]] = rec

    sys.path.insert(0, args.app)
    from experts.m4_whisper_small import WhisperSmallModel  # noqa: E402  (이미지 안의 운영 코드)
    import numpy as np  # noqa: E402

    t_load = time.perf_counter()
    model = WhisperSmallModel(args.model)
    load_s = time.perf_counter() - t_load
    if model.asr_pipe is None:
        raise SystemExit(f"ASR 파이프라인 초기화 실패: {args.model}")

    first = rows[0]
    wav0 = np.frombuffer(read_wav(manifest.parent / first["wav_path"]), dtype=np.int16).astype(np.float32) / 32768.0
    t = time.perf_counter()
    model.infer({"waveform": wav0})  # 워밍업 (지연 통계 제외)
    warmup_s = time.perf_counter() - t

    meta = {
        "label": args.label, "model_dir": args.model, "manifest": str(manifest), "files": len(rows),
        "limit": args.limit, "ids_file": args.ids_file,
        "m4_ort_threads": os.getenv("M4_ORT_THREADS", "2(default)"),
        "ort_intra_op_threads": os.getenv("ORT_INTRA_OP_THREADS"),
        "model_load_s": round(load_s, 2), "warmup_s": round(warmup_s, 2),
        "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    print(f"[m4eval] {args.label}: {len(rows)}개 (이미 완료 {len(done)}), 로드 {load_s:.1f}s, 워밍업 {warmup_s:.1f}s",
          flush=True)

    t_run = time.time()
    with results_path.open("a", encoding="utf-8") as fout:
        for idx, r in enumerate(rows, 1):
            if r["audio_id"] in done:
                continue
            pcm = read_wav(manifest.parent / r["wav_path"])
            wav = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
            t = time.perf_counter()
            res = model.infer({"waveform": wav})
            lat = time.perf_counter() - t
            hyp = res.get("transcript_ko") or ""
            rec = {"audio_id": r["audio_id"], "label": r["label"], "source": res.get("stt_source"),
                   "evaluation_source": r["evaluation_source"], "duration_s": round(len(wav) / 16000, 3),
                   "latency_s": round(lat, 3), "ref": r["transcript"], "hyp": hyp,
                   **score_row(r["transcript"], hyp, r.get("matched_keyword"))}
            fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fout.flush()
            done[r["audio_id"]] = rec
            if idx % 20 == 0 or idx == len(rows):
                print(f"[m4eval] {idx}/{len(rows)}  최근 {lat:.2f}s  경과 {(time.time() - t_run) / 60:.1f}분",
                      flush=True)

    items = [done[r["audio_id"]] for r in rows if r["audio_id"] in done]
    summary = {
        "meta": meta,
        "all": aggregate(items),
        "by_label": {lb: aggregate([x for x in items if x["label"] == lb]) for lb in sorted({x["label"] for x in items})},
        "by_source": {s: aggregate([x for x in items if x["evaluation_source"] == s])
                      for s in sorted({x["evaluation_source"] for x in items})},
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")

    def line(name, a):
        if not a.get("files"):
            return f"| {name} | 0 | — | — | — | — | — |"
        return (f"| {name} | {a['files']} | {a['cer']:.2%} | {a['wer']:.2%} | {a['keyword_hit_rate']:.2%} | "
                f"{a['latency_s']['p50']:.2f} / {a['latency_s']['p95']:.2f} | {a['rtf']['mean']:.2f} |")

    md = [f"# M4 STT 평가 — {args.label}", "",
          f"- 모델: `{args.model}`  /  M4_ORT_THREADS={meta['m4_ort_threads']}",
          f"- 표본: {len(items)}개 (limit={args.limit or '전체'}{', ids=' + args.ids_file if args.ids_file else ''})",
          f"- 로드 {meta['model_load_s']}s, 워밍업 {meta['warmup_s']}s",
          f"- 빈 출력 {summary['all']['empty_outputs']}개, whisper 외 경로 {summary['all']['non_whisper_source']}개", "",
          "| 구분 | 파일 | CER | WER | 키워드 | 지연 p50 / p95 (s) | RTF 평균 |",
          "|---|---:|---:|---:|---:|---:|---:|",
          line("전체", summary["all"])]
    md += [line(f"라벨 {k}", v) for k, v in summary["by_label"].items()]
    md += [line(f"출처 {k}", v) for k, v in summary["by_source"].items()]
    md += ["", "> 지표 정의는 이대경 CT2_REFERENCE.md와 같지만 디코딩 경로가 달라 CT2 수치와 직접 비교하지 않는다.",
           "> 이 평가 세트는 개발 판단에 쓰인 세트라 최종 독립 테스트 결과가 아니다."]
    (out / "summary.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))
    print(f"[m4eval] 저장: {out}")


if __name__ == "__main__":
    main()
