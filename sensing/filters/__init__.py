"""
CSI 신호 전처리 모듈
- Butterworth 밴드패스 필터 (심박/호흡 대역)
- FFT 기반 주파수 특징 추출
- 진폭 정규화
"""

import numpy as np
from scipy.signal import butter, sosfilt


def butterworth_bandpass(data: np.ndarray, low_hz: float, high_hz: float,
                         fs: float = 100.0, order: int = 4) -> np.ndarray:
    """Butterworth 밴드패스 필터 — 심박(0.8~3 Hz) 또는 호흡(0.1~0.6 Hz) 대역 추출."""
    nyq = fs / 2.0
    sos = butter(order, [low_hz / nyq, high_hz / nyq], btype="band", output="sos")
    return sosfilt(sos, data).astype(np.float32)


def fft_features(data: np.ndarray, fs: float = 100.0, top_k: int = 8) -> np.ndarray:
    """FFT 후 상위 k개 주파수 성분의 (주파수, 진폭) 쌍을 반환."""
    centered = data - float(np.mean(data))
    spectrum = np.abs(np.fft.rfft(centered))
    freqs = np.fft.rfftfreq(centered.size, d=1.0 / fs)

    k = min(top_k, spectrum.size)
    top_idx = np.argpartition(spectrum, -k)[-k:]
    top_idx = top_idx[np.argsort(spectrum[top_idx])[::-1]]

    result = np.empty(k * 2, dtype=np.float32)
    result[0::2] = freqs[top_idx]
    result[1::2] = spectrum[top_idx]
    return result


def normalize_amplitude(data: np.ndarray, eps: float = 1e-8) -> np.ndarray:
    """진폭을 [-1, 1] 범위로 정규화."""
    peak = float(np.max(np.abs(data))) + eps
    return (data / peak).astype(np.float32)


def preprocess_csi(raw: np.ndarray, fs: float = 100.0) -> np.ndarray:
    """
    CSI 원시 배열 → 모델 입력 특징 벡터 변환 파이프라인.
    1. 진폭 정규화
    2. 호흡 대역 필터 (0.1–0.6 Hz)
    3. 심박 대역 필터 (0.8–3.0 Hz)
    4. FFT 특징 8개 (freqs + amps)
    최종 출력: normalize + resp_filtered + heart_filtered + fft_features 연결
    """
    if raw.size < 16:
        return np.zeros(raw.size * 3 + 16, dtype=np.float32)

    normed = normalize_amplitude(raw)

    resp = butterworth_bandpass(normed, 0.1, 0.6, fs=fs)
    heart = butterworth_bandpass(normed, 0.8, 3.0, fs=fs)
    feats = fft_features(normed, fs=fs, top_k=8)

    return np.concatenate([normed, resp, heart, feats]).astype(np.float32)
