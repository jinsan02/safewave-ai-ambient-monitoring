#!/usr/bin/env python3
"""모델 단독 추론 지연 측정 (M1·M3·M4) — ai-experts 이미지 안에서 실행한다.

운영 코드(ai/experts)를 그대로 불러 모델만 반복 호출한다. 파이프라인 대기열·Redis는 포함하지 않는다.
CPU/GPU는 이미지와 ORT_USE_GPU로 정해진다(실행 EP는 결과의 providers에 남긴다).
M4 입력은 평가 세트의 실제 음성을 manifest 순서대로 앞에서부터 쓴다(CPU·GPU가 같은 파일).
사용 예:
  docker compose ... run --rm -v "$PWD:/repo" ai-experts python /repo/scripts/bench_models.py \
      --manifest /repo/data/m4_eval_2398/manifest.csv --out /repo/reports/laptop/models_cpu.json
"""
import argparse
import csv
import json
import os
import statistics
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, os.getenv("APP_DIR", "/app"))
import numpy as np  # noqa: E402


def dist(ms):
    ms = sorted(ms)
    return {"n": len(ms), "p50": round(statistics.median(ms), 2),
            "p95": round(ms[min(len(ms) - 1, int(len(ms) * 0.95))], 2),
            "mean": round(statistics.fmean(ms), 2), "max": round(ms[-1], 2)}


def timed(fn, sample, n, warmup):
    for _ in range(warmup):
        fn(sample)
    out = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn(sample)
        out.append((time.perf_counter() - t0) * 1000)
    return out


def providers(model):
    for attr in ("session", "_session", "encoder_session"):
        s = getattr(model, attr, None)
        if s is not None and hasattr(s, "get_providers"):
            return s.get_providers()
    pipe = getattr(model, "asr_pipe", None)  # M4: optimum ORTModel
    return getattr(getattr(pipe, "model", None), "providers", None)


def load_waves(manifest, count):
    rows = list(csv.DictReader(open(manifest, encoding="utf-8-sig")))[:count]
    waves = []
    for r in rows:
        with wave.open(str(Path(manifest).parent / r["wav_path"]), "rb") as w:
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        waves.append(pcm.astype(np.float32) / 32768.0)
    return waves


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--n-m1", type=int, default=300)
    ap.add_argument("--n-m3", type=int, default=50)
    ap.add_argument("--n-m4", type=int, default=30, help="M4에 쓸 음성 파일 수(파일당 1회)")
    ap.add_argument("--warmup", type=int, default=3)
    ap.add_argument("--out")
    args = ap.parse_args()

    from experts import m1_wifi_pose, m3_ast_base, m4_whisper_small

    model_dir = os.getenv("MODEL_PATH", "/app/models")
    rng = np.random.default_rng(0)
    res = {"ort_use_gpu": os.getenv("ORT_USE_GPU", "0"), "models": {}}

    m1_name = os.getenv("FALL_DETECTION_MODEL", "m1_wifi_pose_onnx")
    m1 = m1_wifi_pose.WifiPoseModel(os.path.join(model_dir, m1_name))
    x = rng.standard_normal((1, int(os.getenv("M1_MAX_NODES", "5")), 64, 100)).astype(np.float32)
    res["models"]["M1"] = {"model": m1_name, "providers": providers(m1),
                           "latency_ms": dist(timed(m1.infer, x, args.n_m1, args.warmup))}

    waves = load_waves(args.manifest, args.n_m4)
    m3_name = os.getenv("M3_ENV_SOUND_MODEL", "ast_onnx")
    m3 = m3_ast_base.EnvSoundAnalysisModel(os.path.join(model_dir, m3_name))
    res["models"]["M3"] = {"model": m3_name, "providers": providers(m3),
                           "latency_ms": dist(timed(m3.infer, waves[0], args.n_m3, args.warmup))}

    m4_name = os.getenv("M4_KO_STT_MODEL", "whisper_onnx_int8_ft_svc")
    m4 = m4_whisper_small.WhisperSmallModel(os.path.join(model_dir, m4_name))
    for w in waves[:args.warmup]:
        m4.infer({"waveform": w, "sample_rate": 16000})
    ms, audio_s = [], []
    for w in waves:
        t0 = time.perf_counter()
        m4.infer({"waveform": w, "sample_rate": 16000})
        ms.append((time.perf_counter() - t0) * 1000)
        audio_s.append(len(w) / 16000)
    res["models"]["M4"] = {"model": m4_name, "providers": providers(m4), "latency_ms": dist(ms),
                           "audio_s_mean": round(statistics.fmean(audio_s), 2),
                           "rtf_mean": round(statistics.fmean(m / 1000 / a for m, a in zip(ms, audio_s)), 3)}

    text = json.dumps(res, ensure_ascii=False, indent=1)
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
