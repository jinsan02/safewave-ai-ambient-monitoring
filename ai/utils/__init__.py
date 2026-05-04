import numpy as np


class TurboQuant:
	def optimize(self, data):
		array = np.asarray(data, dtype=np.float32)
		if array.size == 0:
			return np.zeros(128, dtype=np.float32)
		return array