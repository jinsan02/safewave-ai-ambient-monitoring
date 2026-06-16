"""
GPU 추론 벤치마크 — M1, M2, M3, M4, Qwen(M5)
각 모델의 실제 파이프라인 입력 포맷을 그대로 사용.
"""
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, "/app")
os.environ.setdefault("ORT_USE_GPU", "1")

MODEL_DIR = os.getenv("MODEL_PATH", "/app/models")
N_M1   = int(os.getenv("N_M1",   "200"))
N_M2   = int(os.getenv("N_M2",   "200"))
N_M3   = int(os.getenv("N_M3",   "50"))
N_M4   = int(os.getenv("N_M4",   "20"))
N_QWEN = int(os.getenv("N_QWEN", "10"))


def stat(label, times_ms, note=""):
    arr = np.array(times_ms)
    d = {
        "model":  label,
        "n":      len(arr),
        "avg_ms": round(float(arr.mean()), 3),
        "min_ms": round(float(arr.min()), 3),
        "p95_ms": round(float(np.percentile(arr, 95)), 3),
        "max_ms": round(float(arr.max()), 3),
    }
    if note:
        d["note"] = note
    print(json.dumps(d), flush=True)


# ── M1 ──────────────────────────────────────────────────────────────────────
# 실제 입력: np.ndarray (192, 100) = 3 nodes × 64 channels × 100 frames
def bench_m1():
    from experts.m1_wifi_pose import WifiPoseModel
    m = WifiPoseModel(os.path.join(MODEL_DIR, "m1_wifi_pose_onnx"))
    dummy = np.random.rand(192, 100).astype(np.float32)

    # 워밍업
    for _ in range(5):
        m.infer(dummy)

    times = []
    for _ in range(N_M1):
        t0 = time.perf_counter()
        m.infer(dummy)
        times.append((time.perf_counter() - t0) * 1000)
    stat("M1_wifi_pose", times,
         note="input=(192,100) 3nodes×64ch×100f, ONNX" if m.session else "heuristic")


# ── M2 ──────────────────────────────────────────────────────────────────────
# 실제 입력: {"resp": (N,), "heart": (N,)} — per-node deque 1000프레임
def bench_m2():
    from experts.m2_frenel_vital import FrenelVitalModel
    m = FrenelVitalModel(os.path.join(MODEL_DIR, "m2_frenel_vital_onnx"))
    dummy = {
        "resp":  np.random.randn(1000).astype(np.float32) * 0.5,
        "heart": np.random.randn(1000).astype(np.float32) * 0.3,
    }

    for _ in range(5):
        m.infer(dummy)

    times = []
    for _ in range(N_M2):
        t0 = time.perf_counter()
        m.infer(dummy)
        times.append((time.perf_counter() - t0) * 1000)
    stat("M2_frenel_vital", times,
         note="input=dict(resp(1000,),heart(1000,)), ONNX" if m.session else "fallback FFT")


# ── M3 ──────────────────────────────────────────────────────────────────────
# 실제 입력: np.ndarray (N,) — 1D waveform (주의: dict 전달 시 ONNX 실행 안 됨)
def bench_m3():
    from experts.m3_ast_base import EnvSoundAnalysisModel
    m = EnvSoundAnalysisModel(os.path.join(MODEL_DIR, "ast_onnx"))
    if m.session is None:
        print(json.dumps({"model": "M3_ast", "error": "ONNX session not loaded"}), flush=True)
        return
    dummy = np.random.randn(32000).astype(np.float32) * 0.3  # 2s @ 16kHz

    for _ in range(3):
        m.infer(dummy)

    times = []
    for _ in range(N_M3):
        t0 = time.perf_counter()
        m.infer(dummy)
        times.append((time.perf_counter() - t0) * 1000)
    stat("M3_ast", times,
         note="input=(32000,) waveform 2s@16kHz. 파이프라인에서는 dict 입력 → ONNX 미실행 버그 있음")


# ── M4 ──────────────────────────────────────────────────────────────────────
# 실제 입력: {"waveform": np.ndarray, "sample_rate": int}
def bench_m4():
    from experts.m4_whisper_small import WhisperSmallModel
    m = WhisperSmallModel(os.path.join(MODEL_DIR, "whisper_onnx"))
    waveform = np.random.randn(32000).astype(np.float32) * 0.3
    dummy = {"waveform": waveform, "sample_rate": 16000}

    for _ in range(2):
        m.infer(dummy)

    times = []
    for _ in range(N_M4):
        t0 = time.perf_counter()
        m.infer(dummy)
        times.append((time.perf_counter() - t0) * 1000)
    stat("M4_whisper", times,
         note="input=dict(waveform(32000,)) 2s@16kHz")


# ── Qwen (M5) ───────────────────────────────────────────────────────────────
def bench_qwen():
    from logic.qwen_05b import QwenLogic
    qwen = QwenLogic(os.path.join(MODEL_DIR, "qwen_05b"))
    dummy_experts = {
        "fall":      {"fall_score": 0.65, "infer_confidence": 0.9},
        "vital":     {"heart_rate": 88.0, "breathing_rate": 20.0, "infer_confidence": 0.8},
        "env_sound": {"label": "speech", "confidence": 0.7, "infer_confidence": 0.7},
        "speech_ko": {"keywords": [], "stt_confidence": 0.5,
                      "speech_detected": True, "infer_confidence": 0.6},
    }
    # is_merged_kv 확인
    qwen._ensure_model_loaded()
    merged = getattr(qwen, "_is_merged_kv", False)
    print(json.dumps({"event": "qwen_mode", "is_merged_kv": merged}), flush=True)

    # 워밍업 2회
    qwen.evaluate(dummy_experts)
    qwen.evaluate(dummy_experts)

    times = []
    for _ in range(N_QWEN):
        t0 = time.perf_counter()
        result = qwen.evaluate(dummy_experts)
        times.append((time.perf_counter() - t0) * 1000)
        print(json.dumps({"qwen_step": len(times),
                          "infer_ms": round(times[-1], 1),
                          "risk_level": result.get("risk_level", "?")}), flush=True)
    stat("M5_qwen05b", times,
         note=f"merged_kv={merged}, max_new_tokens=64, GPU ORT")


if __name__ == "__main__":
    import onnxruntime as ort
    providers = ort.get_available_providers()
    print(json.dumps({
        "event":       "benchmark_start",
        "ORT_USE_GPU": os.getenv("ORT_USE_GPU"),
        "MODEL_DIR":   MODEL_DIR,
        "ort_providers": providers,
    }), flush=True)

    bench_m1()
    bench_m2()
    bench_m3()
    bench_m4()
    bench_qwen()

    print(json.dumps({"event": "benchmark_done"}), flush=True)
