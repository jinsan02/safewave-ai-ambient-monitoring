"""M3 AST 기반 환경음 분석 전문가 모델.

입력 신호를 AST ONNX 입력 형식으로 맞춰 추론하고,
환경음 라벨/신뢰도를 반환한다.
"""

import os

import numpy as np
import onnxruntime as ort

from utils import get_ort_providers

class EnvSoundAnalysisModel:
    ENV_LABELS = ["silence", "speech", "music", "impact", "noise", "alarm", "unknown"]

    def __init__(self, model_path):
        self.model_path = model_path
        self.effective_model_path = model_path
        self.session = None
        if os.path.isdir(self.model_path):
            self.effective_model_path = os.path.join(self.model_path, "m3_activity.onnx")

        if os.path.exists(self.effective_model_path):
            self.session = ort.InferenceSession(self.effective_model_path, providers=get_ort_providers())

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
        label_idx = int(np.argmax(logits)) if logits.size > 0 else 0
        if logits.size > 0:
            shifted = logits - np.max(logits)
            probs = np.exp(shifted)
            denom = float(np.sum(probs))
            confidence = float(probs[label_idx] / denom) if denom > 0.0 else 0.0
        else:
            confidence = 0.0
        return {
            "onnx_top_class": f"class_{label_idx}",   # 하위 호환
            "ast_top_class": f"class_{label_idx}",
            "onnx_confidence": confidence,             # 하위 호환 (infer 내부 참조용)
            "ast_top_confidence": confidence,
        }

    def _heuristic_label(self, data):
        x = np.asarray(data, dtype=np.float32).reshape(-1)
        if x.size == 0:
            return "unknown", 0.0

        energy = float(np.mean(np.abs(x)))
        if energy < 0.01:
            return "silence", 0.95

        zcr = float(np.mean(np.abs(np.diff(np.sign(x)))) / 2.0) if x.size > 1 else 0.0
        fft_mag = np.abs(np.fft.rfft(x))
        if fft_mag.size <= 1:
            return "noise", 0.5

        peak = float(np.max(fft_mag))
        mean_mag = float(np.mean(fft_mag) + 1e-6)
        tonal_ratio = peak / mean_mag

        if tonal_ratio > 20.0:
            return "alarm", 0.8
        if zcr < 0.08 and 0.02 <= energy <= 0.25:
            return "speech", 0.72
        if zcr < 0.05 and energy > 0.25:
            return "music", 0.68
        if energy > 0.6:
            return "impact", 0.7
        return "noise", 0.62

    def _classify_fallback(self, data):
        label, confidence = self._heuristic_label(data)
        return {
            "env_sound_label": label,   # 하위 호환
            "label": label,
            "env_sound_confidence": confidence,  # 하위 호환
            "confidence": confidence,
        }

    def infer(self, input_data):
        data = self._preprocess(input_data)
        onnx = None
        if self.session is not None:
            try:
                onnx = self._classify_onnx(data)
            except Exception:
                onnx = None

        fallback = self._classify_fallback(data)
        label = fallback["env_sound_label"]
        confidence = float(fallback["env_sound_confidence"])

        if onnx is not None:
            confidence = float(np.clip(0.5 * confidence + 0.5 * float(onnx["onnx_confidence"]), 0.0, 1.0))

        source = "onnx" if onnx is not None else "heuristic"
        result = {
            "env_sound_label": label,
            "env_sound_confidence": confidence,
            "env_sound_source": source,
            "activity": label,
            "activity_confidence": confidence,
        }
        if onnx is not None:
            result.update(onnx)  # onnx_top_class, ast_top_class, onnx_confidence, ast_top_confidence
        return result


class ActivityClassificationModel(EnvSoundAnalysisModel):
    """기존 코드 호환용 별칭 클래스."""
