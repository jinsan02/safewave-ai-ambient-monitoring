"""M1~M4 전문가 단위 검증 스크립트.

환경 설정을 읽어 전문가 모델을 직접 호출하고,
더미 입력으로 핵심 출력 키를 점검한다.
"""

import json
import os
import sys
from pathlib import Path

import numpy as np


def _load_env_file(env_path: Path) -> dict[str, str]:
    env_map: dict[str, str] = {}
    if not env_path.exists():
        return env_map

    for line in env_path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        k, v = raw.split("=", 1)
        env_map[k.strip()] = v.strip()
    return env_map


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    expert_dir = repo_root / "ai" / "experts"
    env_file = _load_env_file(repo_root / ".env")
    model_dir = Path(env_file.get("MODEL_PATH", str(repo_root / "volumes" / "models")))
    model_dir_str = str(model_dir)
    if model_dir_str.startswith("/app") or model_dir_str.startswith("\\app"):
        model_dir = repo_root / "volumes" / "models"

    sys.path.insert(0, str(expert_dir))

    from m1_wifi_pose import WifiPoseModel
    from m2_frenel_vital import FrenelVitalModel
    from m3_ast_base import EnvSoundAnalysisModel
    from m4_whisper_small import WhisperSmallModel

    m1_path = model_dir / env_file.get("FALL_DETECTION_MODEL", "m1_wifi_pose_onnx")
    m2_path = model_dir / env_file.get("VITAL_SENSING_MODEL", "m2_frenel_vital_onnx")
    m3_path = model_dir / env_file.get("M3_ENV_SOUND_MODEL", env_file.get("ACTIVITY_MODEL", "ast_hf"))
    m4_path = model_dir / env_file.get("M4_KO_STT_MODEL", env_file.get("OCCUPANCY_MODEL", "whisper_onnx"))

    m1 = WifiPoseModel(str(m1_path))
    m2 = FrenelVitalModel(str(m2_path))
    m3 = EnvSoundAnalysisModel(str(m3_path))
    m4 = WhisperSmallModel(str(m4_path))

    dummy = np.sin(np.linspace(0.0, 8.0 * np.pi, 512)).astype(np.float32)
    audio_event = {
        "waveform": dummy.tolist(),
        "text_ko": "도와줘",
    }

    m1_result = m1.infer(dummy)
    m1_shape = tuple(m1._preprocess(dummy).shape)
    m2_result = m2.infer(dummy)
    m3_result = m3.infer(dummy)
    m4_result = m4.infer(audio_event)

    required_m1 = {"fall_score", "fall_detected"}
    required_m2 = {"heart_rate", "breathing_rate"}
    required_m3 = {"env_sound_label", "env_sound_confidence"}
    required_m4 = {"transcript_ko", "speech_detected", "stt_confidence"}

    print("[m1] model:", m1_path)
    print("[m1] preprocess shape:", m1_shape)
    print("[m1] result:", json.dumps(m1_result, ensure_ascii=False))
    print("[m2] model:", m2_path)
    print("[m2] result:", json.dumps(m2_result, ensure_ascii=False))
    print("[m3] model:", m3_path)
    print("[m3] result:", json.dumps(m3_result, ensure_ascii=False))
    print("[m4] model:", m4_path)
    print("[m4] result:", json.dumps(m4_result, ensure_ascii=False))

    if not required_m1.issubset(m1_result):
        raise RuntimeError("m1 output key mismatch")
    if m1_shape != (1, 1, 192, 100):
        raise RuntimeError(f"m1 input shape mismatch: {m1_shape}")
    if not required_m2.issubset(m2_result):
        raise RuntimeError("m2 output key mismatch")
    if not required_m3.issubset(m3_result):
        raise RuntimeError("m3 output key mismatch")
    if not required_m4.issubset(m4_result):
        raise RuntimeError("m4 output key mismatch")
    print("[ok] m1~m4 expert smoke test passed")


if __name__ == "__main__":
    main()