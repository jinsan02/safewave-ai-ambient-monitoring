FROM python:3.12-slim-bookworm AS cpu-runtime

RUN apt-get update && apt-get install -y \
	build-essential \
	libgomp1 \
	&& rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# optimum(M4 생성 루프)이 끌어오는 torch를 CPU 전용 휠로 먼저 고정한다.
# 기본 PyPI 휠은 CUDA 패키지 수 GB를 함께 설치한다(RPi5에는 GPU 없음).
RUN pip install --no-cache-dir --timeout 600 torch --index-url https://download.pytorch.org/whl/cpu
# ARM64/RPi 포함 범용 CPU 런타임에서는 onnxruntime CPU 패키지로 교체 설치
RUN sed 's/^onnxruntime-gpu==/onnxruntime==/' requirements.txt > requirements.cpu.txt \
	&& pip install --no-cache-dir --timeout 300 -r requirements.cpu.txt \
	&& ! pip list 2>/dev/null | grep -qi '^nvidia-'

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


# ── GPU 런타임 — 노트북 비교 실험용(RPi5 미사용) ──────────────────────────────
# RTX 50 시리즈(Blackwell, 연산 능력 12.0)는 CUDA 12.8 이상이 필요하다.
# onnxruntime-gpu 1.22+ PyPI 휠은 CUDA 12 + cuDNN 9 빌드다. torch도 CUDA(cu128) 휠: M4(optimum)가
# 디코더 출력을 torch 텐서로 모델 장치에 올리므로 CPU 전용 torch로는 GPU 추론이 되지 않는다.
FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu22.04 AS gpu-runtime

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
	python3 \
	python3-pip \
	libgomp1 \
	&& rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir --timeout 900 torch --index-url https://download.pytorch.org/whl/cu128
RUN sed 's/^onnxruntime-gpu==.*/onnxruntime-gpu==1.22.0/' requirements.txt > requirements.gpu.txt \
	&& pip3 install --no-cache-dir --timeout 300 -r requirements.gpu.txt

COPY . .

CMD ["python3", "main.py"]


# ── GGUF(llama.cpp) CUDA 런타임 — 노트북 GPU 실험용 ai-qwen(M5) ─────────────────
# 빌드에는 nvcc가 필요해 devel 이미지에서 휠만 만들고 gpu-runtime에 설치한다.
FROM nvidia/cuda:12.8.1-cudnn-devel-ubuntu22.04 AS gguf-cuda-build

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y python3 python3-pip cmake ninja-build git \
	&& rm -rf /var/lib/apt/lists/* \
	&& pip3 install --no-cache-dir -U pip
# 빌드 컨테이너에는 GPU 드라이버가 없으므로 libcuda는 CUDA 스텁으로 링크한다.
RUN ln -s /usr/local/cuda/lib64/stubs/libcuda.so /usr/local/cuda/lib64/stubs/libcuda.so.1 \
	&& LD_LIBRARY_PATH=/usr/local/cuda/lib64/stubs:$LD_LIBRARY_PATH \
	CMAKE_ARGS="-DGGML_CUDA=on -DCMAKE_CUDA_ARCHITECTURES=120" \
	pip3 wheel --no-deps --wheel-dir /wheels "llama-cpp-python>=0.3.0"

FROM gpu-runtime AS gguf-gpu-runtime

COPY --from=gguf-cuda-build /wheels /wheels
RUN pip3 install --no-cache-dir /wheels/*.whl diskcache jinja2 typing-extensions && rm -rf /wheels

ENV SLM_BACKEND=gguf \
	SLM_MODEL=qwen_15b_gguf_q5 \
	SLM_TOKENIZER=qwen_15b

CMD ["python3", "qwen_service.py"]
