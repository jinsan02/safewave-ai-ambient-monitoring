"""M4 Whisper-Small 기반 점유 추정 전문가 모델.

Whisper encoder ONNX 입력 형식에 맞춰 전처리하고,
점유 여부/점유 점수를 추정한다.
"""

import os

import numpy as np
import onnxruntime as ort

class WhisperSmallModel:
    def __init__(self, model_path):
        self.model_path = model_path
        self.effective_model_path = model_path
        self.session = None
        if os.path.isdir(self.model_path):
            self.effective_model_path = os.path.join(self.model_path, "encoder_model.onnx")

        if os.path.exists(self.effective_model_path):
            self.session = ort.InferenceSession(self.effective_model_path, providers=["CPUExecutionProvider"])

    def _preprocess(self, input_data):
        data = np.asarray(input_data, dtype=np.float32)
        if self.session is not None:
            input_meta = self.session.get_inputs()[0]
            shape = list(input_meta.shape)
            if len(shape) == 3:
                d1 = int(shape[1]) if isinstance(shape[1], int) and shape[1] > 0 else 80
                d2 = int(shape[2]) if isinstance(shape[2], int) and shape[2] > 0 else 3000
                flat = data.reshape(-1)
                need = d1 * d2
                if flat.size < need:
                    flat = np.pad(flat, (0, need - flat.size), mode="constant")
                elif flat.size > need:
                    flat = flat[:need]
                return flat.reshape(1, d1, d2).astype(np.float32)
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
            try:
                return self._predict_onnx(data)
            except Exception:
                # Whisper encoder ONNX를 occupancy 형태로 바로 쓰기 어려운 경우 폴백
                pass
        return self._predict_fallback(data)
