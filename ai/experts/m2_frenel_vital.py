"""M2 Fresnel 생체신호 전문가 모델.

입력 신호를 고정 길이로 전처리하고,
호흡수/심박수를 ONNX 또는 FFT 폴백으로 추정한다.
"""

import os

import numpy as np
import onnxruntime as ort

from utils import get_ort_providers


class FrenelVitalModel:
    def __init__(self, model_path, sampling_rate=100.0):
        self.model_path = model_path
        self.effective_model_path = model_path
        self.sampling_rate = sampling_rate
        self.session = None
        if os.path.isdir(self.model_path):
            self.effective_model_path = os.path.join(self.model_path, "m2_frenel_vital.onnx")

        if os.path.exists(self.effective_model_path):
            self.session = ort.InferenceSession(self.effective_model_path, providers=get_ort_providers())

    def _preprocess(self, signal_data):
        data = np.asarray(signal_data, dtype=np.float32).reshape(-1)
        target = 256
        if data.size == 0:
            return np.zeros(target, dtype=np.float32)
        if data.size < target:
            data = np.pad(data, (0, target - data.size), mode="constant")
        else:
            data = data[:target]
        return data

    def _infer_onnx(self, data):
        input_name = self.session.get_inputs()[0].name
        feed = data.reshape(1, -1).astype(np.float32)
        output = np.asarray(self.session.run(None, {input_name: feed})[0]).reshape(-1)
        if output.size >= 2:
            breathing_rate = float(output[0])
            heart_rate = float(output[1])
        else:
            heart_rate, breathing_rate = self._infer_fallback(data).values()
        return {
            "heart_rate": max(0.0, heart_rate),
            "breathing_rate": max(0.0, breathing_rate),
        }

    def _estimate_peak_frequency(self, spectrum, freqs, low_hz, high_hz):
        band_mask = (freqs >= low_hz) & (freqs <= high_hz)
        if not np.any(band_mask):
            return 0.0
        band_power = spectrum[band_mask]
        band_freqs = freqs[band_mask]
        peak_idx = int(np.argmax(band_power))
        return float(band_freqs[peak_idx])

    def _infer_fallback(self, data):
        centered = data - float(np.mean(data))
        spectrum = np.abs(np.fft.rfft(centered))
        freqs = np.fft.rfftfreq(centered.size, d=1.0 / self.sampling_rate)

        hr_hz = self._estimate_peak_frequency(spectrum, freqs, 0.8, 3.0)
        rr_hz = self._estimate_peak_frequency(spectrum, freqs, 0.1, 0.6)

        heart_rate = hr_hz * 60.0
        breathing_rate = rr_hz * 60.0
        return {
            "heart_rate": float(np.clip(heart_rate, 40.0, 180.0)),
            "breathing_rate": float(np.clip(breathing_rate, 6.0, 40.0)),
        }

    def infer(self, signal_data):
        data = self._preprocess(signal_data)
        if self.session is not None:
            return self._infer_onnx(data)
        return self._infer_fallback(data)
