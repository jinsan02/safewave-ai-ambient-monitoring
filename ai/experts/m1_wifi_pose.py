"""M1 WiFi 포즈 전문가 — 낙상 감지 (CNN-GRU 백본).

이전 버전 대비 세 가지가 달라졌다.

1. 서브캐리어 192 -> 64
   ESP block_raw 가 64채널이다. 192는 레거시 PulseFi 잔재.

2. 출력에 시그모이드 적용
   모델 출력은 fall_logit (시그모이드 전) 이라 음수가 나온다.
   구버전은 clip(0,1) 만 해서 음수 logit 이 전부 0.0 이 됐다.
     logit -0.475 -> 구버전 0.000 / 올바른 값 0.383
     logit +1.000 -> 구버전 1.000 / 올바른 값 0.731
   사실상 이진 계단 함수였다.

3. 노드별 100프레임 롤링 버퍼 (신규)
   모델 입력은 (1,1,64,100) = 1초 윈도우. 그런데 Redis csi:raw 는
   프레임 하나당 메시지 하나(100Hz)라, 구버전은 1프레임을 192*100 으로
   zero-pad 해서 넣었다 — 99%가 0인 텐서로 추론했다.

주의: sensing/filters.preprocess_csi 가 592 float 을 만든다
  [0:192]   normalize_amplitude(raw)   <- ESP 가 이미 peak 정규화해서 사실상 항등
  [192:384] resp 대역 (의미 없음, 축을 착각한 필터)
  [384:576] heart 대역 (동상)
  [576:592] FFT 특징
  M1 은 [0:64] 만 쓴다 = block_raw.
"""

import os
from collections import deque

import numpy as np
import onnxruntime as ort

from utils import get_ort_providers

N_SUBCARRIERS = 64
N_FRAMES = 100
PARTIAL_POLICY = os.getenv("M1_PARTIAL_POLICY", "wait")
# 몇 프레임마다 추론할지. 1이면 매 프레임(100Hz), 10이면 0.1초마다(10Hz).
# 낙상은 2초쯤 지속되는 사건이라 10Hz 로도 충분하고 CPU 를 10배 아낀다.
INFER_STRIDE = int(os.getenv("M1_INFER_STRIDE", "10"))
FALL_THRESHOLD = float(os.getenv("M1_FALL_THRESHOLD", "0.7"))


def _sigmoid(x):
    return float(1.0 / (1.0 + np.exp(-np.clip(x, -60.0, 60.0))))


class WifiPoseModel:
    def __init__(self, model_path):
        self.model_path = model_path
        self.effective_model_path = model_path
        self.session = None
        self.input_name = None
        if os.path.isdir(self.model_path):
            self.effective_model_path = os.path.join(self.model_path, "m1_wifi_pose.onnx")

        if os.path.exists(self.effective_model_path):
            self.session = ort.InferenceSession(
                self.effective_model_path, providers=get_ort_providers()
            )
            self.input_name = self.session.get_inputs()[0].name

        self._buffers = {}
        self._since_infer = {}
        self._last_result = {}

    def _extract_frame(self, sensor_data):
        data = np.asarray(sensor_data, dtype=np.float32).reshape(-1)
        if data.size >= N_SUBCARRIERS:
            return data[:N_SUBCARRIERS].copy()
        out = np.zeros(N_SUBCARRIERS, dtype=np.float32)
        out[: data.size] = data
        return out

    def _preprocess(self, sensor_data, node_id=0):
        data = np.asarray(sensor_data, dtype=np.float32)

        if data.ndim == 4:
            return data
        if data.ndim == 2 and data.shape == (N_SUBCARRIERS, N_FRAMES):
            return data[None, None, :, :]

        buf = self._buffers.setdefault(node_id, deque(maxlen=N_FRAMES))
        buf.append(self._extract_frame(data))

        if len(buf) < N_FRAMES:
            if PARTIAL_POLICY != "pad":
                return None
            win = np.zeros((N_FRAMES, N_SUBCARRIERS), dtype=np.float32)
            win[-len(buf):] = np.stack(buf)
        else:
            win = np.stack(buf)

        return win.T[None, None, :, :].astype(np.float32)

    def _infer_onnx(self, data):
        output = self.session.run(None, {self.input_name: data})[0]
        arr = np.asarray(output)
        if arr.ndim == 3 and arr.shape[-1] == 2:
            motion_energy = float(np.mean(np.linalg.norm(arr[0], axis=-1)))
            score = float(np.clip(motion_energy / 1.5, 0.0, 1.0))
        else:
            score = _sigmoid(float(arr.reshape(-1)[0]))
        return {"fall_score": score, "fall_detected": score >= FALL_THRESHOLD}

    def _infer_fallback(self, data):
        variance = float(np.var(data))
        score = float(np.clip(variance * 10.0, 0.0, 1.0))
        return {"fall_score": score, "fall_detected": score >= FALL_THRESHOLD}

    def infer(self, sensor_data, node_id=0):
        data = self._preprocess(sensor_data, node_id=node_id)

        # 버퍼가 찬 뒤에는 INFER_STRIDE 프레임마다만 실제 추론하고,
        # 사이 프레임은 직전 결과를 그대로 돌려준다.
        if data is not None and INFER_STRIDE > 1:
            n = self._since_infer.get(node_id, INFER_STRIDE) + 1
            if n < INFER_STRIDE and node_id in self._last_result:
                self._since_infer[node_id] = n
                return dict(self._last_result[node_id], cached=True)
            self._since_infer[node_id] = 0

        if data is None:
            buf = self._buffers.get(node_id)
            return {
                "fall_score": 0.0,
                "fall_detected": False,
                "warming_up": True,
                "frames": len(buf) if buf else 0,
                "frames_needed": N_FRAMES,
            }
        out = self._infer_onnx(data) if self.session is not None \
              else self._infer_fallback(data)
        self._last_result[node_id] = out
        return out

    def buffer_status(self):
        return {nid: len(b) for nid, b in self._buffers.items()}
