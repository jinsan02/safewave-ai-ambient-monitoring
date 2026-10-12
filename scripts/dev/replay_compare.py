#!/usr/bin/env python3
"""replay_bench 결과(환경별 1회씩)와 원래 실측 구간을 나란히 비교한다. 실험용.

비교 항목: 오디오 처리 지연(M3+M4, audio:result 스트림 ID − 이벤트 ts_ms), 긴급 문장 인식,
경보 목록·시점(재생 시작 기준)·기록 지연(ai:emergency 스트림 ID − 스냅샷 ts_ms), M5 추론 시간(ai-qwen 로그),
게이트 등급 분포·M1 확정 횟수, 자원(CPU·메모리·온도).
결과: <out>/COMPARE.md + 그림 4장. matplotlib이 있는 파이썬으로 실행한다.

사용: python scripts/dev/replay_compare.py --base data/clips/20261012_2307-2310_demo_2256 \
        --runs data/replay_bench/<gpu> data/replay_bench/<cpu> data/replay_bench/<rpi5> --out data/replay_bench/compare_1
"""
import argparse
import csv
import json
import statistics as st
from pathlib import Path


def sid_ms(s: str) -> int:
    return int(str(s).split("-")[0])


def pct(v, q):
    if not v:
        return None
    s = sorted(v)
    return s[min(len(s) - 1, int(q * len(s)))]


def load(folder: Path, name: str, t0: int, t1: int | None) -> dict:
    v = folder / "validation"
    within = lambda ms: ms >= t0 and (t1 is None or ms < t1)
    audio = []
    for line in open(v / "raw_audio_result.jsonl", encoding="utf-8"):
        r = json.loads(line)
        ms, d = sid_ms(r["stream_id"]), r["data"]
        if within(ms):
            m3, m4 = d.get("env_sound") or {}, d.get("speech_ko") or {}
            audio.append({"off": (ms - t0) / 1000, "lat": ms - int(d["ts_ms"]), "m3": m3.get("env_sound_label"),
                          "text": m4.get("transcript_ko", ""), "phrase": bool(m4.get("emergency_phrase_detected"))})
    alerts = []
    for line in open(v / "raw_ai_emergency.jsonl", encoding="utf-8"):
        r = json.loads(line)
        ms, d = sid_ms(r["stream_id"]), r["data"]
        if within(ms):
            alerts.append({"off": (ms - t0) / 1000, "lat": ms - int(d.get("ts_ms") or ms), "mode": d.get("slm_mode"),
                           "level": d.get("risk_level"), "score": d.get("risk_score"),
                           "summary": (d.get("summary") or d.get("qwen_reason") or "")[:60]})
    levels, fall_rises, prev = {}, 0, False
    for row in csv.DictReader(open(v / "gate.csv", encoding="utf-8-sig")):
        if within(int(row["stream_ms"])):
            levels[row["risk_level"]] = levels.get(row["risk_level"], 0) + 1
    for row in csv.DictReader(open(v / "m1_fall.csv", encoding="utf-8-sig")):
        if within(int(row["stream_ms"])):
            cur = row["fall_detected"] == "True"
            fall_rises += cur and not prev
            prev = cur
    m5 = []
    if (folder / "m5_log.jsonl").exists():
        for line in open(folder / "m5_log.jsonl", encoding="utf-8"):
            r = json.loads(line)
            if r.get("event") == "qwen_invoked":
                m5.append({"off": (r["ts_ms"] - t0) / 1000, "ms": r.get("qwen_infer_ms"), "level": r.get("risk_level")})
    res = []
    if (folder / "resources.csv").exists():
        for row in csv.DictReader(open(folder / "resources.csv", encoding="utf-8")):
            ms = int(row["ts_ms"])
            if within(ms):
                res.append({"off": (ms - t0) / 1000, "cpu": float(row["cpu_pct"]),
                            "temp": float(row["temp_c"]) if row["temp_c"] not in ("", "None") else None,
                            "mem": float(row["mem_used_gb"])})
    return {"name": name, "audio": audio, "alerts": alerts, "levels": levels, "fall_rises": fall_rises,
            "m5": m5, "res": res}


def fmt(v, unit="ms"):
    if v is None:
        return "—"
    return f"{v / 1000:.2f}s" if unit == "ms" and v >= 1000 else f"{v:.0f}{unit}" if unit == "ms" else f"{v:.1f}{unit}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="원래 실측 구간 폴더(clips/...) — summary.json의 window_ms 사용")
    ap.add_argument("--runs", nargs="+", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    base = Path(args.base)
    win = json.loads((base / "summary.json").read_text(encoding="utf-8"))["window_ms"]
    data = [load(base, "실측(노트북 GPU, 라이브)", win[0], win[1])]
    for run in args.runs:
        run = Path(run)
        meta = json.loads((run / "run.json").read_text(encoding="utf-8"))
        data.append(load(run, f"재생 {meta['env']}", meta["replay_start_ms"], None))

    lines = ["# 같은 입력 재생 비교 (실측 23:07~23:10 구간)", "",
             f"기준: `{base}` · 재생: " + ", ".join(f"`{r}`" for r in args.runs), "",
             "## 요약", "",
             "| 환경 | 오디오 건수 | 오디오 지연 p50 / p95 / 최대 | 긴급 문장 인식 | 경보 (규칙/M5) | M5 호출 | M5 추론 p50 / 최대 | M1 확정 | CPU 평균/최고 | 메모리 최고 | 온도 최고 |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for d in data:
        lat = [a["lat"] for a in d["audio"]]
        m5ms = [m["ms"] for m in d["m5"] if m["ms"] is not None]
        cpu = [x["cpu"] for x in d["res"]]
        temps = [x["temp"] for x in d["res"] if x["temp"] is not None]
        rule = sum(a["mode"] == "rule" for a in d["alerts"])
        lines.append(
            f"| {d['name']} | {len(lat)} | {fmt(pct(lat, .5))} / {fmt(pct(lat, .95))} / {fmt(max(lat) if lat else None)} | "
            f"{sum(a['phrase'] for a in d['audio'])} | {rule}/{len(d['alerts']) - rule} | {len(d['m5'])} | "
            f"{fmt(pct(m5ms, .5))} / {fmt(max(m5ms) if m5ms else None)} | {d['fall_rises']} | "
            + (f"{st.mean(cpu):.0f}% / {max(cpu):.0f}%" if cpu else "—") + " | "
            + (f"{max(x['mem'] for x in d['res']):.1f}GB" if d["res"] else "—") + " | "
            + (f"{max(temps):.0f}°C" if temps else "—") + " |")
    lines += ["", "## 경보 목록 (재생 시작 기준 초)", ""]
    for d in data:
        lines.append(f"### {d['name']}")
        lines.append("| 시점 | 경로 | 등급 | 점수 | 기록 지연 | 요약 |")
        lines.append("|---|---|---|---|---|---|")
        for a in d["alerts"]:
            lines.append(f"| {a['off']:.1f}s | {'규칙' if a['mode'] == 'rule' else 'M5'} | {a['level']} | {a['score']} | {fmt(a['lat'])} | {a['summary']} |")
        if not d["alerts"]:
            lines.append("| — | — | — | — | — | 경보 없음 |")
        lines.append("")
    lines += ["## 긴급 문장으로 인식된 전사", ""]
    for d in data:
        hits = [f"{a['off']:.0f}s '{a['text']}'" for a in d["audio"] if a["phrase"]]
        lines.append(f"- **{d['name']}**: " + (", ".join(hits) if hits else "없음"))
    lines += ["", "## 게이트 등급 분포(스냅샷 수)", ""]
    for d in data:
        lines.append(f"- **{d['name']}**: {d['levels']}")

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        plt.rcParams["font.family"] = "Malgun Gothic"
        plt.rcParams["axes.unicode_minus"] = False
        names = [d["name"] for d in data]
        fig, ax = plt.subplots(figsize=(9, 4))
        ax.boxplot([[a["lat"] / 1000 for a in d["audio"]] or [0] for d in data], labels=names)
        ax.set_ylabel("초"); ax.set_title("오디오 처리 지연 (M3+M4)")
        fig.tight_layout(); fig.savefig(out / "1_audio_latency.png", dpi=130); plt.close(fig)
        fig, ax = plt.subplots(figsize=(9, 3.5))
        for i, d in enumerate(data):
            for a in d["alerts"]:
                ax.scatter(a["off"], i, marker="s" if a["mode"] == "rule" else "o", s=70,
                           color="#dc2626" if a["level"] == "critical" else "#f59e0b")
        ax.set_yticks(range(len(data))); ax.set_yticklabels(names); ax.set_xlabel("재생 시작 기준 초")
        ax.set_title("경보 시점 (■ 규칙, ● M5)"); ax.grid(axis="x", alpha=.3)
        fig.tight_layout(); fig.savefig(out / "2_alert_timeline.png", dpi=130); plt.close(fig)
        fig, ax = plt.subplots(figsize=(9, 3.5))
        m5d = [d for d in data if d["m5"]]
        if m5d:
            ax.boxplot([[m["ms"] / 1000 for m in d["m5"] if m["ms"]] or [0] for d in m5d], labels=[d["name"] for d in m5d])
        ax.set_ylabel("초"); ax.set_title("M5 추론 시간")
        fig.tight_layout(); fig.savefig(out / "3_m5_infer.png", dpi=130); plt.close(fig)
        fig, ax = plt.subplots(figsize=(9, 3.5))
        for d in data:
            if d["res"]:
                ax.plot([x["off"] for x in d["res"]], [x["cpu"] for x in d["res"]], label=d["name"])
        ax.set_ylabel("CPU %"); ax.set_xlabel("재생 시작 기준 초"); ax.legend(); ax.set_title("CPU 사용률")
        fig.tight_layout(); fig.savefig(out / "4_cpu.png", dpi=130); plt.close(fig)
        lines += ["", "## 그림", "", "![](1_audio_latency.png)", "![](2_alert_timeline.png)", "![](3_m5_infer.png)", "![](4_cpu.png)"]
    except ImportError:
        lines += ["", "(matplotlib 없음 — 그림 생략)"]

    (out / "COMPARE.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(out / "COMPARE.md")


if __name__ == "__main__":
    main()
