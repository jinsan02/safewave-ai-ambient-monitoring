import os

import numpy as np
import onnxruntime as ort


class FallDetectionModel:
    def __init__(self, model_path):
        self.model_path = model_path
        self.session = None
        if os.path.exists(self.model_path):
            self.session = ort.InferenceSession(self.model_path, providers=["CPUExecutionProvider"])

    def _preprocess(self, sensor_data):
        data = np.asarray(sensor_data, dtype=np.float32)
        if data.ndim == 1:
            data = data.reshape(1, -1)
        return data

    def _infer_onnx(self, data):
        input_name = self.session.get_inputs()[0].name
        output = self.session.run(None, {input_name: data})[0]
        score = float(np.asarray(output).reshape(-1)[0])
        score = float(np.clip(score, 0.0, 1.0))
        return {"fall_score": score, "fall_detected": score >= 0.7}

    def _infer_fallback(self, data):
        variance = float(np.var(data))
        score = float(np.clip(variance * 10.0, 0.0, 1.0))
        return {"fall_score": score, "fall_detected": score >= 0.7}

    def infer(self, sensor_data):
        data = self._preprocess(sensor_data)
        if self.session is not None:
            return self._infer_onnx(data)
        return self._infer_fallback(data)