#!/usr/bin/env python3
"""M3 v34 낙상 50회 재현 — 담당자 results.csv와 우리 통합 모듈(EnvSoundAnalysisModel) 결과 비교.

(a) 슬라이딩: 3초 창·1초 홉으로 운영 모듈의 ONNX 분류를 돌려 최대 impact → 담당자 max_impact와 대조
(b) 운영 경로: 클립 전체를 infer()에 넣음(M3_WINDOW_MODE=peak가 3초 선택) → impact_alert
ai-experts 이미지 안에서 실행:
  docker run --rm -v C:\\rp5:/repo -v C:\\rp5\\volumes\\models:/app/models rp5-ai-experts \\
    python /repo/scripts/dev/m3_fall_repro.py --root /repo/data/m3_eval_v34/core
"""
import argparse
import csv
import os
import sys
import wave

sys.path.insert(0, os.getenv("APP_DIR", "/app"))
import numpy as np  # noqa: E402

from experts.m3_ast_base import EnvSoundAnalysisModel  # noqa: E402


def load(path):
    with wave.open(path, "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--threshold", type=float, default=0.6)
    args = ap.parse_args()
    m3 = EnvSoundAnalysisModel(os.path.join(os.getenv("MODEL_PATH", "/app/models"), "ast_onnx"))
    rows = list(csv.DictReader(open(os.path.join(args.root, "fall_test", "results.csv"), encoding="utf-8-sig")))
    out, diffs = [], []
    for r in rows:
        rel = r["wav"].replace("\\", "/").split("fall_test/", 1)[1]
        x = load(os.path.join(args.root, "fall_test", rel))
        best = 0.0
        for s in range(0, max(1, len(x) - 48000 + 1), 16000):
            win = x[s:s + 48000]
            if len(win) < 48000:
                win = np.pad(win, (48000 - len(win), 0))
            if float(np.abs(win).max()) < m3.silence_gate:
                continue
            best = max(best, float(m3._classify_onnx(win.reshape(1, -1))["probs"][2]))
        pipe = m3.infer({"waveform": x, "sample_rate": 16000})
        ref = float(r["max_impact"])
        diffs.append(abs(best - ref))
        out.append((r["scenario"], best >= args.threshold, ref >= args.threshold, bool(pipe["impact_alert"])))
    print(f"model={m3.model_name} backend={m3.backend} n={len(out)} threshold={args.threshold}")
    print(f"max_impact 차이(우리 슬라이딩 vs 담당자): 최대 {max(diffs):.5f}, 평균 {np.mean(diffs):.5f}")
    for scen in sorted({o[0] for o in out}):
        sub = [o for o in out if o[0] == scen]
        print(f"  {scen}: 슬라이딩 {sum(o[1] for o in sub)}/{len(sub)} | 담당자 CSV {sum(o[2] for o in sub)}/{len(sub)}"
              f" | 운영 경로(peak 3초) {sum(o[3] for o in sub)}/{len(sub)}")
    print(f"합계: 슬라이딩 {sum(o[1] for o in out)}/{len(out)} | 담당자 CSV {sum(o[2] for o in out)}/{len(out)}"
          f" | 운영 경로 {sum(o[3] for o in out)}/{len(out)}")


if __name__ == "__main__":
    main()
