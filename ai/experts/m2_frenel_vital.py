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
            return {
                "heart_rate": 0.0, "breathing_rate": 0.0,
                "infer_source": "onnx", "infer_confidence": 0.0,
            }
        return {
            "heart_rate": max(0.0, heart_rate),
            "breathing_rate": max(0.0, breathing_rate),
            "infer_source": "onnx",
            "infer_confidence": 0.70,
        }

    def _spectral_confidence(self, spectrum, freqs, low_hz, high_hz) -> float:
        band_mask = (freqs >= low_hz) & (freqs <= high_hz)
        if not np.any(band_mask):
            return 0.30
        band_power = spectrum[band_mask]
        peak = float(np.max(band_power))
        mean_p = float(np.mean(band_power) + 1e-9)
        snr = peak / mean_p
        return float(np.clip(0.30 + (snr - 1.0) / 20.0, 0.30, 0.65))

    def _estimate_peak_frequency(self, spectrum, freqs, low_hz, high_hz):
        band_mask = (freqs >= low_hz) & (freqs <= high_hz)
        if not np.any(band_mask):
            return 0.0
        band_power = spectrum[band_mask]
        band_freqs = freqs[band_mask]
        peak_idx = int(np.argmax(band_power))
        return float(band_freqs[peak_idx])

    def _infer_fallback_dual(self, resp: np.ndarray, heart: np.ndarray) -> dict:
        """ESP IIR 필터 완료 2밴드 시간 시리즈 — 채널 평균된 (N,) 배열.
        N < 10 이면 버퍼 누적 미달로 confidence=0.0 반환."""
        if resp.size < 10 or heart.size < 10:
            return {
                "heart_rate": 0.0, "breathing_rate": 0.0,
                "infer_source": "fft", "infer_confidence": 0.0,
            }

        def _peak_hz(sig: np.ndarray, lo: float, hi: float) -> tuple[float, float]:
            if sig.size < 4:
                return 0.0, 0.30
            centered = sig - float(np.mean(sig))
            spectrum = np.abs(np.fft.rfft(centered))
            freqs    = np.fft.rfftfreq(centered.size, d=1.0 / self.sampling_rate)
            hz   = self._estimate_peak_frequency(spectrum, freqs, lo, hi)
            conf = self._spectral_confidence(spectrum, freqs, lo, hi)
            return hz, conf

        rr_hz, rr_conf = _peak_hz(resp,  0.1, 0.6)
        hr_hz, hr_conf = _peak_hz(heart, 0.8, 3.0)
        return {
            "heart_rate":      float(np.clip(hr_hz * 60.0, 40.0, 180.0)),
            "breathing_rate":  float(np.clip(rr_hz * 60.0,  6.0,  40.0)),
            "infer_source":    "fft",
            "infer_confidence": round(float(min(hr_conf, rr_conf)), 3),
        }

    def infer(self, signal_data):
        if isinstance(signal_data, dict):
            resp  = np.asarray(signal_data.get("resp",  []), dtype=np.float32).reshape(-1)
            heart = np.asarray(signal_data.get("heart", []), dtype=np.float32).reshape(-1)
        else:
            resp  = np.asarray(signal_data, dtype=np.float32).reshape(-1)
            heart = resp
        if self.session is not None:
            if resp.size < 10 or heart.size < 10:
                return {
                    "heart_rate": 0.0, "breathing_rate": 0.0,
                    "infer_source": "onnx", "infer_confidence": 0.0,
                }
            # 최신 128프레임 × 2밴드 = 256샘플 (_preprocess target과 일치)
            data = self._preprocess(np.concatenate([resp[-128:], heart[-128:]]))
            return self._infer_onnx(data)
        return self._infer_fallback_dual(resp, heart)
