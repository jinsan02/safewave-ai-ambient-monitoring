import os

import numpy as np
import onnxruntime as ort


def get_ort_providers():
	# ort.get_available_providers()는 TensorRT/CUDA EP 라이브러리를 즉시 초기화하여
	# GPU 드라이버 미설치 환경(RPi5, 개발PC)에서 segfault를 일으킴.
	# ORT_USE_GPU=1 환경변수로 명시 요청된 경우에만 CUDA 시도.
	if os.getenv("ORT_USE_GPU", "0") == "1":
		return ["CUDAExecutionProvider", "CPUExecutionProvider"]
	return ["CPUExecutionProvider"]


class TurboQuant:
	def optimize(self, data):
		array = np.asarray(data, dtype=np.float32)
		if array.size == 0:
			return np.zeros(128, dtype=np.float32)
		return array
