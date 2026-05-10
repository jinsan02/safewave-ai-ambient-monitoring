"""M3 AST-Base ONNX 내보내기 스크립트.

Hugging Face AST 분류 모델을 ONNX로 변환하고,
런타임 로드/추론 가능 여부를 검증한다.
"""

import argparse
from pathlib import Path

import numpy as np
import onnx
import onnxruntime as ort
import torch
from transformers import ASTForAudioClassification


DEFAULT_MODEL_ID = "MIT/ast-finetuned-audioset-10-10-0.4593"
DEFAULT_OUTPUT = Path("./volumes/models/ast_onnx/m3_activity.onnx")


def export_ast_to_onnx(model_id: str, output_path: Path, opset: int = 17) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    model = ASTForAudioClassification.from_pretrained(model_id)
    model.eval()

    dummy = torch.zeros((1, 1024, 128), dtype=torch.float32)

    torch.onnx.export(
        model,
        args=(dummy,),
        f=str(output_path),
        export_params=True,
        opset_version=opset,
        do_constant_folding=True,
        input_names=["input_values"],
        output_names=["logits"],
        dynamic_axes=None,
    )


def verify_onnx(output_path: Path) -> None:
    onnx_model = onnx.load(str(output_path))
    onnx.checker.check_model(onnx_model)

    session = ort.InferenceSession(str(output_path), providers=["CPUExecutionProvider"])
    input_name = session.get_inputs()[0].name
    output_name = session.get_outputs()[0].name

    test_input = np.zeros((1, 1024, 128), dtype=np.float32)
    result = session.run([output_name], {input_name: test_input})
    print(f"[m3] onnx load ok: {output_path}")
    print(f"[m3] output shape: {result[0].shape}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Export AST-Base to ONNX (FP32)")
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--opset", type=int, default=17)
    args = parser.parse_args()

    output_path = Path(args.output)
    export_ast_to_onnx(args.model_id, output_path, opset=args.opset)
    verify_onnx(output_path)


if __name__ == "__main__":
    main()
