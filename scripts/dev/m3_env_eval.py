#!/usr/bin/env python3
"""M3 환경음 평가 묶음 — 운영 경로(EnvSoundAnalysisModel.infer)로 정확도·오탐·지연을 한 번에 잰다.

  1) 낙상 50회(core/fall_test, 10초): 클립 전체를 infer(M3_WINDOW_MODE=peak가 3초 선택) → impact_alert 감지율(조건별)
  2) 오탐 사례 16구간(core/fp_examples_20260911, 5초): 지금 모델·게이트에서 impact_alert가 다시 나는지
  3) 생활 소음 4시간(ambient/20260911): sensing과 같은 VAD(-45 dB, 300 ms, 여운 250 ms, 최대 6 s)로 자른 이벤트에
     게인 보정(peak < 0.12 → 0.85)과 원본 peak(raw_peak)을 실어 infer → 시간당 impact·alarm 판정
  4) 추론 1회 지연(p50·p95, 위 전부)
ai-experts 이미지 안에서 실행:
  docker run --rm [--gpus all -e ORT_USE_GPU=1] -v C:/rp5:/repo -v C:/rp5/volumes/models:/app/models -w /repo \\
    --entrypoint python3 rp5-ai-experts-gpu scripts/dev/m3_env_eval.py --out reports/laptop/val/m3_env.json
"""
import argparse
import csv
import glob
import json
import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.getenv("APP_DIR", "/app"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np  # noqa: E402

from experts.m3_ast_base import EnvSoundAnalysisModel  # noqa: E402
from m3_fall_repro import load  # noqa: E402  (16 kHz int16 wav)
from m4_voice_emergency_eval import SR, amplify, vad_events  # noqa: E402


def timed(m3, payload, lat):
    t = time.perf_counter()
    out = m3.infer(payload)
    lat.append((time.perf_counter() - t) * 1000)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--core", default="data/m3_eval_v34/core")
    ap.add_argument("--ambient", default="data/m3_eval_v34/ambient/20260911")
    ap.add_argument("--vad-db", type=float, default=-45.0)
    ap.add_argument("--ambient-files", type=int, default=-1, help="생활 소음 파일 수(-1=전부, 0=건너뜀)")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    m3 = EnvSoundAnalysisModel(os.path.join(os.getenv("MODEL_PATH", "/app/models"), "ast_onnx"))
    lat, rep = [], {"model": m3.model_name, "backend": m3.backend, "impact_threshold": m3.impact_threshold,
                    "silence_gate": m3.silence_gate, "window_mode": os.getenv("M3_WINDOW_MODE", "peak")}

    # 1) 낙상 50회
    rows = list(csv.DictReader(open(os.path.join(a.core, "fall_test", "results.csv"), encoding="utf-8-sig")))
    by = {}
    for r in rows:
        rel = r["wav"].replace("\\", "/").split("fall_test/", 1)[1]
        x = load(os.path.join(a.core, "fall_test", rel))
        out = timed(m3, {"waveform": x, "sample_rate": SR}, lat)
        by.setdefault(r["scenario"], []).append(bool(out["impact_alert"]))
    hit = sum(sum(v) for v in by.values())
    rep["fall50"] = {"detected": f"{hit}/{len(rows)}", "rate": round(hit / len(rows), 4),
                     "by_condition": {k: f"{sum(v)}/{len(v)}" for k, v in sorted(by.items())}}

    # 2) 오탐 사례 16구간
    fps = sorted(glob.glob(os.path.join(a.core, "fp_examples_20260911", "*.wav")))
    labels = Counter()
    alerts = 0
    for p in fps:
        x = load(p)
        out = timed(m3, {"waveform": x, "sample_rate": SR}, lat)
        alerts += bool(out["impact_alert"])
        labels[out["env_sound_label"]] += 1
    rep["fp_examples16"] = {"impact_alert": f"{alerts}/{len(fps)}", "labels": dict(labels)}

    # 3) 생활 소음 4시간 (운영 VAD 이벤트)
    files = sorted(glob.glob(os.path.join(a.ambient, "*.wav")))
    files = files if a.ambient_files < 0 else files[:a.ambient_files]
    total_s, events, lab, impact, alarm = 0.0, 0, Counter(), 0, 0
    for p in files:
        x = load(p)
        total_s += len(x) / SR
        for _, ev in vad_events(x, a.vad_db):
            raw_peak = float(np.max(np.abs(ev))) if ev.size else 0.0
            out = timed(m3, {"waveform": amplify(ev), "sample_rate": SR, "raw_peak": raw_peak}, lat)
            events += 1
            lab[out["env_sound_label"]] += 1
            impact += bool(out["impact_alert"])
            alarm += out["env_sound_label"] == "alarm" and float(out.get("env_sound_confidence", 0)) >= 0.8
    hours = max(total_s / 3600, 1e-9)
    rep["ambient"] = {"hours": round(hours, 2), "files": len(files), "vad_events": events,
                      "labels": dict(lab), "impact_alerts": impact, "impact_per_h": round(impact / hours, 2),
                      "alarm_conf80": alarm, "alarm_per_h": round(alarm / hours, 2)}
    rep["latency_ms"] = {"n": len(lat), "p50": round(float(np.percentile(lat, 50)), 1),
                         "p95": round(float(np.percentile(lat, 95)), 1), "max": round(float(np.max(lat)), 1)}
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    json.dump(rep, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(rep, ensure_ascii=False))


if __name__ == "__main__":
    main()
