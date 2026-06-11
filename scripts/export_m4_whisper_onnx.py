"""M4 Whisper-Small ONNX 내보내기 스크립트.

optimum-cli를 사용해 Whisper 모델을 ONNX로 변환하고,
필수 산출물과 API 로딩 스니펫을 확인한다.
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


# SungBeom/whisper-small-ko: Korean AI Hub fine-tune, same architecture as whisper-small,
# decoder_with_past 사용 가능, 한국어 WER 개선
DEFAULT_MODEL_ID = "SungBeom/whisper-small-ko"
DEFAULT_OUTPUT_DIR = Path("./volumes/models/whisper_onnx")


def ensure_optimum_cli() -> None:
    if shutil.which("optimum-cli") is None:
        raise RuntimeError(
            "optimum-cli not found. Install with: pip install \"optimum[onnxruntime]\" transformers onnx onnxruntime"
        )


def export_whisper(model_id: str, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "optimum-cli",
        "export",
        "onnx",
        "--model",
        model_id,
        "--task",
        "automatic-speech-recognition-with-past",
        str(output_dir),
    ]
    print("[m4] running:", " ".join(cmd))
    subprocess.run(cmd, check=True)


def checklist(output_dir: Path) -> None:
    expected = [
        "encoder_model.onnx",
        "decoder_model.onnx",
    ]
    optional = [
        "decoder_with_past_model.onnx",
        "generation_config.json",
        "config.json",
        "preprocessor_config.json",
    ]

    print("\n[m4] checklist")
    for name in expected:
        path = output_dir / name
        print(f"- {'OK' if path.exists() else 'MISSING'}: {name}")

    for name in optional:
        path = output_dir / name
        print(f"- {'OK' if path.exists() else 'OPTIONAL-MISSING'}: {name}")


def print_api_loading_snippet(output_dir: Path) -> None:
    print("\n[m4] api/main.py 참고 로딩 코드")
    snippet = f'''
from pathlib import Path
from optimum.onnxruntime import ORTModelForSpeechSeq2Seq
from transformers import AutoProcessor, pipeline

WHISPER_DIR = Path(r"{output_dir}")

processor = AutoProcessor.from_pretrained(WHISPER_DIR)
asr_model = ORTModelForSpeechSeq2Seq.from_pretrained(
    WHISPER_DIR,
    file_name="encoder_model.onnx",
    decoder_file_name="decoder_model.onnx",
    decoder_with_past_file_name="decoder_with_past_model.onnx",
)

asr_pipe = pipeline(
    task="automatic-speech-recognition",
    model=asr_model,
    tokenizer=processor.tokenizer,
    feature_extractor=processor.feature_extractor,
)

# result = asr_pipe(audio_array_or_path)
# text = result.get("text", "")
'''.strip()
    print(snippet)


def main() -> None:
    parser = argparse.ArgumentParser(description="Export Whisper-small to ONNX (FP32)")
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    ensure_optimum_cli()
    export_whisper(args.model_id, output_dir)
    checklist(output_dir)
    print_api_loading_snippet(output_dir)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        print(f"[m4] export failed: {exc}", file=sys.stderr)
        raise
