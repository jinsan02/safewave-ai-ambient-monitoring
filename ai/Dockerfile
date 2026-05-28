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