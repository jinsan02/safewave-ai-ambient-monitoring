FROM python:3.12-slim-bookworm AS cpu-runtime

RUN apt-get update && apt-get install -y \
	build-essential \
	libgomp1 \
	&& rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# ARM64/RPi 포함 범용 CPU 런타임에서는 onnxruntime CPU 패키지로 교체 설치
RUN sed 's/^onnxruntime-gpu==/onnxruntime==/' requirements.txt > requirements.cpu.txt \
	&& pip install --no-cache-dir --timeout 300 -r requirements.cpu.txt

COPY . .

CMD ["python", "main.py"]


# ── GGUF(llama.cpp) 런타임 — ai-qwen(M5) 전용 ───────────────────────────────
# M5는 llama.cpp Q5_K_M로 구동(M5 한정 예외 — M1~M4 ONNX 파이프라인 무관).
# RPi5(ARM64)는 prebuilt wheel이 없어 소스 빌드 → cmake 필요.
# cpu-runtime을 베이스로 유지: qwen_15b.py(부모 클래스)가 onnxruntime을 import한다.
FROM cpu-runtime AS gguf-runtime

RUN apt-get update && apt-get install -y cmake \
	&& rm -rf /var/lib/apt/lists/* \
	&& pip install --no-cache-dir --timeout 600 "llama-cpp-python>=0.3.0"

ENV SLM_BACKEND=gguf \
	SLM_MODEL=qwen_15b_gguf_q5 \
	SLM_TOKENIZER=qwen_15b

CMD ["python", "qwen_service.py"]


FROM nvidia/cuda:11.8.0-cudnn8-devel-ubuntu22.04 AS gpu-runtime

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
	python3 \
	python3-pip \
	python3-venv \
	build-essential \
	libgomp1 \
	&& rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir --timeout 300 -r requirements.txt

COPY . .

CMD ["python3", "main.py"]