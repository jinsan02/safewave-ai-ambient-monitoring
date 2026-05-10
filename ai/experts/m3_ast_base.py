"""M3 AST 기반 활동 분류 전문가 모델.

오디오/센싱 특징을 AST ONNX 입력 형식으로 맞추고,
활동 클래스와 신뢰도를 반환한다.
"""

import os

import numpy as np
import onnxruntime as ort

class ActivityClassificationModel:
    LABELS = ["idle", "walking", "sitting", "lying", "unknown"]

    def __init__(self, model_path):
        self.model_path = model_path
        self.effective_model_path = model_path
        self.session = None
        if os.path.isdir(self.model_path):
            self.effective_model_path = os.path.join(self.model_path, "m3_activity.onnx")

        if os.path.exists(self.effective_model_path):
            self.session = ort.InferenceSession(self.effective_model_path, providers=["CPUExecutionProvider"])

    def _preprocess(self, input_data):
        data = np.asarray(input_data, dtype=np.float32)
        if self.session is None:
            if data.ndim == 1:
                return data.reshape(1, -1)
            return data

        input_meta = self.session.get_inputs()[0]
        shape = list(input_meta.shape)

        # AST 기본 입력: [1, 1024, 128]
        if len(shape) == 3:
            target_h = int(shape[1]) if isinstance(shape[1], int) and shape[1] > 0 else 1024
            target_w = int(shape[2]) if isinstance(shape[2], int) and shape[2] > 0 else 128
            flat = data.reshape(-1)
            need = target_h * target_w
            if flat.size < need:
                flat = np.pad(flat, (0, need - flat.size), mode="constant")
            elif flat.size > need:
                flat = flat[:need]
            return flat.reshape(1, target_h, target_w).astype(np.float32)

        if data.ndim == 1:
            return data.reshape(1, -1)
        return data

    def _classify_onnx(self, data):
        input_name = self.session.get_inputs()[0].name
        logits = np.asarray(self.session.run(None, {input_name: data})[0]).reshape(-1)
        label_idx = int(np.argmax(logits)) if logits.size > 0 else len(self.LABELS) - 1
        if logits.size > 0:
            shifted = logits - np.max(logits)
            probs = np.exp(shifted)
            denom = float(np.sum(probs))
            confidence = float(probs[label_idx] / denom) if denom > 0.0 else 0.0
        else:
            confidence = 0.0
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
            try:
                return self._classify_onnx(data)
            except Exception:
                # 입력 shape 불일치 등 ONNX 런타임 오류 시 폴백
                pass
        return self._classify_fallback(data)
