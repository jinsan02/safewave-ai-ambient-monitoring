# 이대경 M4 인계 wrapper(m4_whisper/onnx_runtime.py) 평가 전용 환경.
# 서비스 이미지와 분리한다 — 인계 요구 버전(Python 3.10, ORT 1.23.2, transformers 4.57.6)이 서비스와 다르다.
#
#   docker build -f scripts/m4_handoff_eval.Dockerfile -t m4-handoff-eval m4_whisper
#   docker run --rm -e M4_ORT_THREADS=2 \
#     -v "$PWD:/repo:ro" -v "$PWD/data/m4_eval_2398:/eval:ro" -v "$PWD/reports:/reports" \
#     -v "$PWD/volumes/models:/models:ro" m4-handoff-eval \
#     python /repo/scripts/eval_m4_stt.py --backend handoff --model /models/whisper_onnx_int8_ft \
#       --manifest /eval/manifest.csv --label handoff-int8 --limit 28
FROM python:3.10-slim-bookworm

# CUDA 부가 패키지 없이 CPU torch 먼저 (인계 README 2절).
# CPU 인덱스만 쓰면 의존 패키지 해석이 실패하므로 PyPI를 보조로 두고, torch는 +cpu로 고정한다.
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir "torch==2.13.0+cpu" \
      --index-url https://download.pytorch.org/whl/cpu --extra-index-url https://pypi.org/simple
COPY requirements-onnx.txt /tmp/requirements-onnx.txt
RUN pip install --no-cache-dir -r /tmp/requirements-onnx.txt

ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
WORKDIR /repo
