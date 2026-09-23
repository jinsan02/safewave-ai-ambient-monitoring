"""모델별 메모리(RSS) 증가량 측정 — ai-experts 이미지 안에서 실행한다.

각 전문가 모델을 차례로 올리고(load) 1회 추론(warm)한 뒤 프로세스 RSS 증가량을 기록한다.
같은 프로세스에 누적해서 올리므로 공유 라이브러리(onnxruntime·torch·transformers) 비용은
처음 그 라이브러리를 쓰는 모델에 붙는다. 운영과 같이 M1→M2→M3→M4 순서로 올린다.

  docker compose run --rm --no-deps -v ./scripts:/scripts:ro ai-experts \
      python /scripts/measure_model_memory.py
"""

import json
import os
import sys
import time

sys.path.insert(0, "/app")

import numpy as np  # noqa: E402


def rss_mb() -> float:
    with open("/proc/self/status") as fh:
        for line in fh:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    return 0.0


def peak_mb() -> float:
    with open("/proc/self/status") as fh:
        for line in fh:
            if line.startswith("VmHWM:"):
                return int(line.split()[1]) / 1024
    return 0.0


def main() -> None:
    model_dir = os.getenv("MODEL_PATH", "/app/models")
    rows = [{"step": "python+numpy", "rss_mb": round(rss_mb(), 1)}]

    from experts import m1_wifi_pose, m2_frenel_vital, m3_ast_base, m4_whisper_small

    rows.append({"step": "import experts", "rss_mb": round(rss_mb(), 1)})
    wave = np.zeros(16000 * 3, dtype=np.float32)
    specs = [
        ("M1", lambda: m1_wifi_pose.WifiPoseModel(
            os.path.join(model_dir, os.getenv("FALL_DETECTION_MODEL", "m1_wifi_pose_onnx"))),
         np.zeros((1, int(os.getenv("M1_MAX_NODES", "5")), 64, 100), dtype=np.float32)),
        ("M2", lambda: m2_frenel_vital.FrenelVitalModel(
            os.path.join(model_dir, os.getenv("VITAL_SENSING_MODEL", "m2_frenel_vital_onnx"))),
         {"resp": np.zeros(1000, dtype=np.float32), "heart": np.zeros(1000, dtype=np.float32)}),
        ("M3", lambda: m3_ast_base.EnvSoundAnalysisModel(
            os.path.join(model_dir, os.getenv("M3_ENV_SOUND_MODEL", "ast_onnx"))), wave),
        ("M4", lambda: m4_whisper_small.WhisperSmallModel(
            os.path.join(model_dir, os.getenv("M4_KO_STT_MODEL", "whisper_onnx_int8_ft_svc"))),
         {"waveform": wave, "sample_rate": 16000}),
    ]
    for name, build, sample in specs:
        before = rss_mb()
        t0 = time.perf_counter()
        model = build()
        loaded = rss_mb()
        load_s = time.perf_counter() - t0
        t0 = time.perf_counter()
        try:
            model.infer(sample)
            error = None
        except Exception as exc:  # 측정은 계속한다
            error = str(exc)[:120]
        warm = rss_mb()
        rows.append({
            "step": name,
            "load_mb": round(loaded - before, 1),
            "warm_mb": round(warm - loaded, 1),
            "total_mb": round(warm - before, 1),
            "rss_mb": round(warm, 1),
            "load_s": round(load_s, 1),
            "first_infer_s": round(time.perf_counter() - t0, 2),
            "error": error,
        })
        globals()[f"_keep_{name}"] = model  # 운영처럼 모두 상주시킨 채 다음 모델을 올린다
    rows.append({"step": "peak", "rss_mb": round(peak_mb(), 1)})
    print(json.dumps(rows, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
