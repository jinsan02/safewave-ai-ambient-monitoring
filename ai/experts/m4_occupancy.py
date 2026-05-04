# M4: Occupancy Detection Model

import os

import numpy as np
import onnxruntime as ort

class OccupancyModel:
    def __init__(self, model_path):
        self.model_path = model_path
        self.session = None
        if os.path.exists(self.model_path):
            self.session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])

    def _preprocess(self, input_data):
        data = np.asarray(input_data, dtype=np.float32)
        if data.ndim == 1:
            data = data.reshape(1, -1)
        return data

    def _predict_onnx(self, data):
        input_name = self.session.get_inputs()[0].name
        output = np.asarray(self.session.run(None, {input_name: data})[0]).reshape(-1)
        score = float(np.max(output)) if output.size > 0 else 0.0
        score = float(np.clip(score, 0.0, 1.0))
        return {"occupied": score >= 0.5, "occupancy_score": score}

    def _predict_fallback(self, data):
        score = float(np.clip(np.mean(np.abs(data)) * 3.0, 0.0, 1.0))
        return {"occupied": score >= 0.5, "occupancy_score": score}

    def infer(self, input_data):
        data = self._preprocess(input_data)
        if self.session is not None:
            return self._predict_onnx(data)
        return self._predict_fallback(data)