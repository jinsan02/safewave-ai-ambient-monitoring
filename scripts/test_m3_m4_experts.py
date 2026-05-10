"""M3/M4 전문가 단위 검증 스크립트.

환경 설정을 읽어 AST/Whisper 전문가를 직접 호출하고,
기본 더미 입력으로 추론 결과를 점검한다.
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

    from m3_ast_base import ActivityClassificationModel
    from m4_whisper_small import WhisperSmallModel

    m3_path = model_dir / env_file.get("ACTIVITY_MODEL", "ast_onnx")
    m4_path = model_dir / env_file.get("OCCUPANCY_MODEL", "whisper_onnx/encoder_model.onnx")

    m3 = ActivityClassificationModel(str(m3_path))
    m4 = WhisperSmallModel(str(m4_path))

    dummy = np.sin(np.linspace(0.0, 8.0 * np.pi, 512)).astype(np.float32)

    m3_result = m3.infer(dummy)
    m4_result = m4.infer(dummy)

    print("[m3] model:", m3_path)
    print("[m3] result:", json.dumps(m3_result, ensure_ascii=False))
    print("[m4] model:", m4_path)
    print("[m4] result:", json.dumps(m4_result, ensure_ascii=False))


if __name__ == "__main__":
    main()