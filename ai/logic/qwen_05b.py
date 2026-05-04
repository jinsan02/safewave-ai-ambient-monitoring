import os

import numpy as np
import onnxruntime as ort


class QwenLogic:
    def __init__(self, model_path):
        self.model_path = model_path
        self.session = None
        if os.path.exists(self.model_path):
            self.session = ort.InferenceSession(self.model_path, providers=["CPUExecutionProvider"])

    def _flatten_features(self, expert_results):
        fall = expert_results.get("fall", {})
        vital = expert_results.get("vital", {})
        activity = expert_results.get("activity", {})
        occupancy = expert_results.get("occupancy", {})

        activity_score = {
            "idle": 0.2,
            "sitting": 0.35,
            "walking": 0.55,
            "lying": 0.9,
            "unknown": 0.5,
        }.get(activity.get("activity", "unknown"), 0.5)

        return np.array(
            [
                float(fall.get("fall_score", 0.0)),
                float(vital.get("heart_rate", 70.0)),
                float(vital.get("breathing_rate", 16.0)),
                float(activity_score),
                float(occupancy.get("occupancy_score", 0.0)),
            ],
            dtype=np.float32,
        )

    def _evaluate_onnx(self, feature_vec):
        input_name = self.session.get_inputs()[0].name
        output = np.asarray(self.session.run(None, {input_name: feature_vec.reshape(1, -1)})[0]).reshape(-1)
        risk_score = float(np.clip(output[0] if output.size > 0 else 0.0, 0.0, 1.0))
        return risk_score

    def _evaluate_rule(self, expert_results, feature_vec):
        fall = expert_results.get("fall", {})
        vital = expert_results.get("vital", {})

        risk = float(np.clip(feature_vec[0] * 0.5 + feature_vec[3] * 0.2 + feature_vec[4] * 0.3, 0.0, 1.0))
        if fall.get("fall_detected", False):
            risk = max(risk, 0.9)

        hr = float(vital.get("heart_rate", 70.0))
        rr = float(vital.get("breathing_rate", 16.0))
        if hr > 130.0 or rr > 30.0:
            risk = max(risk, 0.8)

        return risk

    def evaluate(self, expert_results):
        feature_vec = self._flatten_features(expert_results)
        if self.session is not None:
            risk_score = self._evaluate_onnx(feature_vec)
        else:
            risk_score = self._evaluate_rule(expert_results, feature_vec)

        if risk_score >= 0.85:
            level = "critical"
        elif risk_score >= 0.6:
            level = "warning"
        else:
            level = "normal"

        return {
            "emergency": risk_score >= 0.6,
            "risk_level": level,
            "risk_score": round(risk_score, 4),
            "experts": expert_results,
        }