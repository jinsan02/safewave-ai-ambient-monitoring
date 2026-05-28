"""M1 WiFi 포즈 전문가 모델.

CSI 입력을 ONNX 모델 입력 형식으로 정규화하고,
낙상 위험 점수로 변환한다.
"""

import os

import numpy as np
import onnxruntime as ort

from utils import get_ort_providers


class WifiPoseModel:
    def __init__(self, model_path):
        self.model_path = model_path
        self.effective_model_path = model_path
        self.session = None
        if os.path.isdir(self.model_path):
            self.effective_model_path = os.path.join(self.model_path, "m1_wifi_pose.onnx")

        if os.path.exists(self.effective_model_path):
            self.session = ort.InferenceSession(self.effective_model_path, providers=get_ort_providers())

    def _preprocess(self, sensor_data):
        data = np.asarray(sensor_data, dtype=np.float32)
        if data.ndim == 4:
            return data

        flat = data.reshape(-1)
        target = 192 * 100
        if flat.size < target:
            flat = np.pad(flat, (0, target - flat.size), mode="constant")
        else:
            flat = flat[:target]
        return flat.reshape(1, 1, 192, 100)

    def _infer_onnx(self, data):
        input_name = self.session.get_inputs()[0].name
        output = self.session.run(None, {input_name: data})[0]
        output_arr = np.asarray(output)
        if output_arr.ndim == 3 and output_arr.shape[-1] == 2:
            # DT-Pose keypoint 좌표 [1, 17, 2]를 fall score로 매핑
            keypoints = output_arr[0]
            motion_energy = float(np.mean(np.linalg.norm(keypoints, axis=-1)))
            score = motion_energy / 1.5
        else:
            score = float(output_arr.reshape(-1)[0])
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
