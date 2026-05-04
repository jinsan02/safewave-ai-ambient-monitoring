# Human Activity Classification Model (M3)

import os

import numpy as np
import onnxruntime as ort

class ActivityClassificationModel:
    LABELS = ["idle", "walking", "sitting", "lying", "unknown"]

    def __init__(self, model_path):
        self.model_path = model_path
        self.session = None
        if os.path.exists(self.model_path):
            self.session = ort.InferenceSession(self.model_path, providers=["CPUExecutionProvider"])

    def _preprocess(self, input_data):
        data = np.asarray(input_data, dtype=np.float32)
        if data.ndim == 1:
            data = data.reshape(1, -1)
        return data

    def _classify_onnx(self, data):
        input_name = self.session.get_inputs()[0].name
        logits = np.asarray(self.session.run(None, {input_name: data})[0]).reshape(-1)
        label_idx = int(np.argmax(logits)) if logits.size > 0 else len(self.LABELS) - 1
        confidence = float(np.max(logits)) if logits.size > 0 else 0.0
        label = self.LABELS[label_idx] if label_idx < len(self.LABELS) else "unknown"
        return {
            "activity": label,
            "activity_confidence": confidence,
        }

    def _classify_fallback(self, data):
        energy = float(np.mean(np.abs(data)))
        if energy < 0.05:
            label = "idle"
            confidence = 0.75
        elif energy < 0.2:
            label = "sitting"
            confidence = 0.65
        else:
            label = "walking"
            confidence = 0.7
        return {
            "activity": label,
            "activity_confidence": confidence,
        }

    def infer(self, input_data):
        data = self._preprocess(input_data)
        if self.session is not None:
            return self._classify_onnx(data)
        return self._classify_fallback(data)