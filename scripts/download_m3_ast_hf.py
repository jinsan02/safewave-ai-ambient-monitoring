"""M3 AST Hugging Face 모델 다운로드 (ONNX 변환 없음).

MIT/ast-finetuned-audioset-10-10-0.4593 가중치를 volumes/models/ast_hf 에 저장한다.
"""

import argparse
from pathlib import Path

from huggingface_hub import snapshot_download

DEFAULT_MODEL_ID = "MIT/ast-finetuned-audioset-10-10-0.4593"
DEFAULT_OUTPUT = Path("./volumes/models/ast_hf")


def download(model_id: str, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=model_id,
        local_dir=str(output_dir),
        local_dir_use_symlinks=False,
    )
    config = output_dir / "config.json"
    if not config.exists():
        raise RuntimeError(f"download incomplete: {config} not found")
    print(f"[m3] downloaded: {model_id}")
    print(f"[m3] local path: {output_dir.resolve()}")
    return output_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Download AST model from Hugging Face")
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()
    download(args.model_id, Path(args.output))


if __name__ == "__main__":
    main()
