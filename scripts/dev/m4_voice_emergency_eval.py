#!/usr/bin/env python3
"""M4 긴급 음성(환각 필터 + 긴급 문장 유사 매칭) 측정 — 오경보/시간과 감지율.

음성 아닌 쪽: 생활 소음(M3 ambient, 4시간)을 sensing과 같은 VAD(-45 dB, 최소 300 ms, 여운 250 ms,
최대 6 s)로 잘라 증폭 규칙(peak < 0.12 → 0.85)까지 적용한 이벤트를 운영 M4(infer)에 넣는다.
  예전 방식: 원문 전사에 긴급 키워드 줄기(살려·도와·119·불·화재 등)가 있으면 긴급으로 본 경우
  새 방식: emergency_phrase_detected(환각 필터 통과 + 유사도 ≥ 기준)
음성 쪽: M4 평가셋(data/m4_eval_2398) 도움 요청·낙상 신고 발화의 감지율과 환각 오판(실제 말을 버림) 수.
ai-experts 이미지 안에서 실행:
  docker run --rm --gpus all -e ORT_USE_GPU=1 -v C:/rp5:/repo -v C:/rp5/volumes/models:/app/models -w /repo \\
    --entrypoint python3 rp5-ai-experts-gpu scripts/dev/m4_voice_emergency_eval.py
"""
import argparse
import csv
import glob
import json
import os
import random
import sys
import wave
from collections import Counter

sys.path.insert(0, os.getenv("APP_DIR", "/app"))
import numpy as np  # noqa: E402

from experts.m4_whisper_small import WhisperSmallModel  # noqa: E402

SR, BLOCK = 16000, 1024
OLD_STEMS = ("살려", "도와", "응급", "위험", "119", "불", "화재")


def load_wav(path):
    with wave.open(path, "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0


def rms_db(x):
    r = float(np.sqrt(np.mean(np.square(x), dtype=np.float64))) if x.size else 0.0
    return 20 * np.log10(r) if r > 1e-9 else -120.0


def vad_events(x, thr_db=-45.0, min_ms=300, hang_ms=250, max_s=6):
    """sensing/audio_main.run_audio_loop와 같은 규칙."""
    min_n, hang_n, max_n = SR * min_ms // 1000, SR * hang_ms // 1000, SR * max_s
    events, buf, active, act_n, sil_n = [], [], False, 0, 0
    for s in range(0, len(x) - BLOCK + 1, BLOCK):
        chunk = x[s:s + BLOCK]
        loud = rms_db(chunk) >= thr_db
        if loud:
            if not active:
                active, act_n, sil_n, buf = True, 0, 0, []
            buf.append(chunk)
            act_n += BLOCK
            sil_n = 0
        elif active:
            buf.append(chunk)
            act_n += BLOCK
            sil_n += BLOCK
        if not active:
            continue
        if act_n >= max_n or (act_n >= min_n and sil_n >= hang_n):
            if act_n >= min_n:
                events.append((s / SR, np.concatenate(buf)))
            active, act_n, sil_n, buf = False, 0, 0, []
    return events


def amplify(x, target=0.85, below=0.12):
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    return x if peak <= 1e-9 or peak >= below else np.clip(x * (target / peak), -1, 1).astype(np.float32)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="whisper_onnx_int8_ft_svc")
    ap.add_argument("--ambient", default="data/m3_eval_v34/ambient/20260911")
    ap.add_argument("--speech", default="data/m4_eval_2398")
    ap.add_argument("--vad-db", type=float, default=-45.0)
    ap.add_argument("--out", default="reports/laptop/m4_voice_emergency")
    args = ap.parse_args()
    m4 = WhisperSmallModel(os.path.join(os.getenv("MODEL_PATH", "/app/models"), args.model))
    rows = []

    hours = 0.0
    for path in sorted(glob.glob(os.path.join(args.ambient, "*.wav"))):
        x = load_wav(path)
        hours += len(x) / SR / 3600
        for t0, ev in vad_events(x, args.vad_db):
            out = m4.infer({"waveform": amplify(ev), "sample_rate": SR})
            raw = out.get("transcript_raw") or ""
            rows.append(["ambient", f"{os.path.basename(path)}@{t0:.1f}s", round(len(ev) / SR, 2), raw,
                         out["transcript_ko"], out["no_speech_prob"], out["avg_logprob"],
                         out["hallucination_filtered"], any(k in raw for k in OLD_STEMS),
                         out["emergency_phrase"], out["emergency_phrase_sim"], out["emergency_phrase_detected"]])

    rng = random.Random(924)
    man = list(csv.DictReader(open(os.path.join(args.speech, "manifest.csv"), encoding="utf-8-sig")))
    groups = {
        "help_direct": rng.sample([r for r in man if r["label"] == "help_direct"], 300),
        "fall_keyword": rng.sample([r for r in man if r["label"] == "fall_related" and r["matched_keyword"]], 150),
        "fall_other": rng.sample([r for r in man if r["label"] == "fall_related" and not r["matched_keyword"]], 100),
    }
    for g, items in groups.items():
        for r in items:
            out = m4.infer({"waveform": load_wav(os.path.join(args.speech, r["wav_path"])), "sample_rate": SR})
            raw = out.get("transcript_raw") or ""
            rows.append([g, r["transcript"], r["audio_duration"], raw, out["transcript_ko"],
                         out["no_speech_prob"], out["avg_logprob"], out["hallucination_filtered"],
                         any(k in raw for k in OLD_STEMS), out["emergency_phrase"],
                         out["emergency_phrase_sim"], out["emergency_phrase_detected"]])

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    head = ["set", "source", "dur_s", "transcript_raw", "transcript_kept", "no_speech_prob", "avg_logprob",
            "hallucination_filtered", "old_keyword_hit", "phrase", "phrase_sim", "phrase_detected"]
    with open(args.out + ".csv", "w", newline="", encoding="utf-8-sig") as f:
        csv.writer(f).writerows([head] + rows)
    summary = {"ambient_hours": round(hours, 2), "vad_db": args.vad_db}
    for g in ("ambient", "help_direct", "fall_keyword", "fall_other"):
        sub = [r for r in rows if r[0] == g]
        s = {"n": len(sub), "filtered_as_hallucination": sum(r[7] for r in sub),
             "old_keyword_hit": sum(r[8] for r in sub), "new_phrase_detected": sum(r[11] for r in sub)}
        if g == "ambient":
            s["old_per_hour"] = round(s["old_keyword_hit"] / hours, 2) if hours else None
            s["new_per_hour"] = round(s["new_phrase_detected"] / hours, 2) if hours else None
            s["new_hits"] = [(r[1], r[3], r[9], r[10]) for r in sub if r[11]]
            s["top_raw"] = Counter(r[3] for r in sub if r[3]).most_common(8)
        else:
            s["missed_examples"] = [(r[1], r[4] or r[3], r[10]) for r in sub if not r[11]][:12]
        summary[g] = s
    json.dump(summary, open(args.out + ".json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
