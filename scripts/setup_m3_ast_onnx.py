"""M3 환경음 모델(AST 6-class end-to-end ONNX) 설치·검증 스크립트.

모델은 이 저장소에서 export하지 않는다. 모델 담당(ast-base 저장소)이 파인튜닝·검증해
배포한 ONNX를 받아 `volumes/models/ast_onnx/` 에 놓고, 런타임 계약을 만족하는지 확인한다.

모델 받기 (Hugging Face 버킷):

    hf sync hf://buckets/sobh6498/ast-finetuned-audioset-10-10-0.4593-bucket/ast-base/exports/ast_onnx ./ast_onnx

설치·검증:

    python scripts/setup_m3_ast_onnx.py --src ./ast_onnx          # 복사 후 검증
    python scripts/setup_m3_ast_onnx.py --verify-only             # 이미 놓인 모델만 검증

검증 항목: 입력 이름/형상, 출력 3종, 클래스 수, 실제 추론 1회, raw_peak 일치, sha256.
"""

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

import numpy as np
import onnxruntime as ort

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DEST = ROOT / "volumes" / "models" / "ast_onnx"

ENV_LABELS = ["silence", "speech", "impact", "noise", "alarm", "unknown"]
EXPECTED_INPUT = "waveform"
EXPECTED_SAMPLES = 48000  # 16kHz * 3s
EXPECTED_OUTPUTS = {"probs", "logits", "raw_peak"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_onnx(directory: Path) -> Path | None:
    candidates = sorted(directory.glob("*.onnx"))
    return candidates[0] if candidates else None


def install(src: Path, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        files = sorted(list(src.glob("*.onnx")) + list(src.glob("*.json")))
        if not files:
            raise SystemExit(f"{src} 에 .onnx 파일이 없습니다")
        for f in files:
            shutil.copy2(f, dest / f.name)
            print(f"[setup] 복사: {f.name} ({f.stat().st_size / 2**20:.1f} MB)")
        model = find_onnx(dest)
    else:
        shutil.copy2(src, dest / src.name)
        print(f"[setup] 복사: {src.name} ({src.stat().st_size / 2**20:.1f} MB)")
        model = dest / src.name
    if model is None:
        raise SystemExit("복사 후 .onnx 를 찾지 못했습니다")
    return model


def verify(model_path: Path) -> bool:
    print(f"[verify] {model_path}")
    print(f"[verify] sha256 = {sha256(model_path)}")

    session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])

    ok = True
    input_meta = session.get_inputs()[0]
    shape = list(input_meta.shape)
    print(f"[verify] 입력  {input_meta.name} {shape}")
    if input_meta.name != EXPECTED_INPUT:
        print(f"         ! 입력 이름이 '{EXPECTED_INPUT}' 이 아닙니다")
        ok = False
    if len(shape) != 2 or (isinstance(shape[1], int) and shape[1] != EXPECTED_SAMPLES):
        print(f"         ! 입력 형상이 [batch, {EXPECTED_SAMPLES}] 이 아닙니다")
        ok = False

    output_names = {o.name for o in session.get_outputs()}
    print(f"[verify] 출력  {sorted(output_names)}")
    missing = EXPECTED_OUTPUTS - output_names
    if missing:
        print(f"         ! 누락된 출력: {sorted(missing)}")
        ok = False

    # 실제 추론 1회 — 게이트 판정에 쓰는 raw_peak 가 입력 peak 과 같은지까지 확인
    rng = np.random.default_rng(0)
    sample = (rng.normal(size=(1, EXPECTED_SAMPLES)) * 0.2).astype(np.float32)
    outputs = dict(zip([o.name for o in session.get_outputs()],
                       session.run(None, {input_meta.name: sample})))

    probs = np.asarray(outputs["probs"]).reshape(-1)
    print(f"[verify] 추론  probs={np.round(probs, 4).tolist()}")
    if probs.size != len(ENV_LABELS):
        print(f"         ! 클래스 수가 {len(ENV_LABELS)} 가 아닙니다 (실제 {probs.size})")
        ok = False
    elif abs(float(probs.sum()) - 1.0) > 1e-3:
        print(f"         ! 확률 합이 1이 아닙니다 ({float(probs.sum()):.4f})")
        ok = False

    if "raw_peak" in outputs:
        raw_peak = float(np.asarray(outputs["raw_peak"]).reshape(-1)[0])
        expected_peak = float(np.max(np.abs(sample)))
        print(f"[verify] raw_peak={raw_peak:.6f} (입력 peak {expected_peak:.6f})")
        if abs(raw_peak - expected_peak) > 1e-5:
            print("         ! raw_peak 이 입력 peak 과 다릅니다")
            ok = False

    print(f"[verify] 라벨  {ENV_LABELS}  (impact index = {ENV_LABELS.index('impact')})")
    print("[verify] PASS" if ok else "[verify] FAIL")
    return ok


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="M3 환경음 ONNX 설치·검증")
    parser.add_argument("--src", default=None, help="받아온 ONNX 파일 또는 폴더")
    parser.add_argument("--dest", default=str(DEFAULT_DEST),
                        help="설치 위치 (기본: volumes/models/ast_onnx)")
    parser.add_argument("--verify-only", action="store_true", help="복사 없이 설치된 모델만 검증")
    args = parser.parse_args()

    dest = Path(args.dest)

    if args.verify_only or args.src is None:
        model = find_onnx(dest)
        if model is None:
            raise SystemExit(f"{dest} 에 .onnx 가 없습니다. --src 로 받아온 경로를 지정하세요.")
    else:
        model = install(Path(args.src), dest)

    sys.exit(0 if verify(model) else 1)


if __name__ == "__main__":
    main()
