import numpy as np
import onnxruntime as ort


def get_ort_providers():
	available = set(ort.get_available_providers())
	providers = []
	if "CUDAExecutionProvider" in available:
		providers.append("CUDAExecutionProvider")
	providers.append("CPUExecutionProvider")
	return providers


class TurboQuant:
	def optimize(self, data):
		array = np.asarray(data, dtype=np.float32)
		if array.size == 0:
			return np.zeros(128, dtype=np.float32)
		return array