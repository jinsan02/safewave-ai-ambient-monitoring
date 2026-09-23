"""M3 AST 기반 환경음 분석 전문가 모델.

SafeWave 전용으로 파인튜닝한 AST 6-class 모델(end-to-end ONNX)로 환경음을 분류한다.
전처리(게인 정규화 + Kaldi log-mel filterbank)가 ONNX 그래프 안에 포함되어 있어
이 모듈은 파형을 16kHz mono 3초로 맞춰 넣기만 한다. torch/transformers 불필요.

ONNX 규격 (ast-base 저장소 `scripts/export_onnx.py` 산출물):
    입력  waveform : float32 [batch, 48000]   16kHz mono, -1.0~1.0
    출력  probs    : float32 [batch, 6]       softmax 확률
          logits   : float32 [batch, 6]
          raw_peak : float32 [batch]          게인 정규화 '전' 원본 peak

판정 규칙:
    raw_peak < M3_SILENCE_GATE(0.005)          → silence 확정 (모델 결과 무시)
    probs[impact] >= M3_IMPACT_THRESHOLD(0.6)  → impact_alert

모델 파일이 없으면 파형 휴리스틱으로 대체한다 (env_sound_source="heuristic").
자세한 내용은 docs/m3-env-sound-onnx.md 참고.
"""

import os

import numpy as np
import onnxruntime as ort

DEFAULT_SAMPLE_RATE = 16000
DEFAULT_WINDOW_MS = 3000
ENV_SOUND_LABELS = ("silence", "speech", "impact", "noise", "alarm", "unknown")
IMPACT_INDEX = ENV_SOUND_LABELS.index("impact")
# 디렉터리로 지정됐을 때 찾아볼 파일명 (앞에서부터 우선)
ONNX_CANDIDATES = ("m3_env_sound.onnx", "v34_homepos.onnx", "ast.onnx")


class EnvSoundAnalysisModel:
    # 파인튜닝 모델의 고정 라벨 순서 — 인덱스를 바꾸면 안 된다
    ENV_LABELS = list(ENV_SOUND_LABELS)
    IMPACT_INDEX = IMPACT_INDEX

    def __init__(self, model_path):
        self.model_path = model_path
        self.effective_model_path = self._resolve_model_file(model_path)
        self.session = None
        self.backend = "heuristic"
        self.input_name = "waveform"
        self.output_names = ["probs", "logits", "raw_peak"]
        self.sample_rate = int(os.getenv("M3_SAMPLE_RATE", str(DEFAULT_SAMPLE_RATE)))
        self.window_samples = int(self.sample_rate * (
            int(os.getenv("M3_AUDIO_WINDOW_MS", str(DEFAULT_WINDOW_MS))) / 1000.0
        ))
        self.silence_gate = float(os.getenv("M3_SILENCE_GATE", "0.005"))
        self.impact_threshold = float(os.getenv("M3_IMPACT_THRESHOLD", "0.6"))
        # 3초보다 긴 입력에서 분석할 3초 선택 — peak(충격 중심) / latest(마지막 3초)
        self.window_mode = os.getenv("M3_WINDOW_MODE", "peak").strip().lower() or "peak"
        self.model_name = os.path.splitext(os.path.basename(self.effective_model_path or ""))[0]
        self._resample_warned = False

        self._init_onnx()

    # ------------------------------------------------------------------ 로딩

    @staticmethod
    def _resolve_model_file(model_path):
        """디렉터리면 그 안에서 .onnx 를 찾고, 파일이면 그대로 쓴다."""
        if not model_path:
            return model_path
        if not os.path.isdir(model_path):
            return model_path

        explicit = os.getenv("M3_ENV_SOUND_ONNX")
        if explicit:
            return os.path.join(model_path, explicit)

        for name in ONNX_CANDIDATES:
            candidate = os.path.join(model_path, name)
            if os.path.exists(candidate):
                return candidate

        found = sorted(f for f in os.listdir(model_path) if f.endswith(".onnx"))
        if found:
            return os.path.join(model_path, found[0])
        return os.path.join(model_path, ONNX_CANDIDATES[0])

    def _init_onnx(self) -> None:
        if not self.effective_model_path or not os.path.exists(self.effective_model_path):
            return
        try:
            from utils import get_ort_providers, get_session_opts

            providers = get_ort_providers()
            # 스레드·스핀 제한(ORT 기본값은 전 코어). RPi5에서 M1·sensing과 코어를 나눠 쓴다.
            sess_options = get_session_opts(int(os.getenv("M3_ORT_THREADS", "2")))
        except Exception:
            providers = ["CPUExecutionProvider"]
            sess_options = None

        try:
            self.session = ort.InferenceSession(
                self.effective_model_path, sess_options=sess_options, providers=providers
            )
        except Exception as exc:
            print(f"[m3] onnx 로드 실패, 휴리스틱으로 동작: {exc}", flush=True)
            self.session = None
            return

        input_meta = self.session.get_inputs()[0]
        self.input_name = input_meta.name
        shape = list(input_meta.shape)
        if len(shape) == 2 and isinstance(shape[1], int) and shape[1] > 0:
            self.window_samples = int(shape[1])
        self.output_names = [o.name for o in self.session.get_outputs()]
        self.backend = "onnx"

    # ------------------------------------------------------------------ 전처리

    def _extract(self, input_data):
        """(waveform, sample_rate, gate_peak) 추출. dict / ndarray 양쪽을 받는다.

        gate_peak 는 sensing 이 **게인 보정 전에** 잰 원본 peak 이다. audio-sensing 은
        조용한 이벤트를 전송 전에 0.85까지 증폭하므로, 받은 파형으로 peak 를 재면
        무음 게이트가 무력화된다. 메타에 raw_peak 가 있으면 그것으로 판정한다.
        """
        if input_data is None:
            return None, self.sample_rate, None

        sample_rate = self.sample_rate
        gate_peak = None
        data = input_data
        if isinstance(input_data, dict):
            sample_rate = int(input_data.get("sample_rate") or self.sample_rate)
            meta_peak = input_data.get("raw_peak")
            if meta_peak is not None:
                try:
                    gate_peak = float(meta_peak)
                except (TypeError, ValueError):
                    gate_peak = None
            data = None
            for key in ("waveform", "samples", "audio", "pcm"):
                value = input_data.get(key)
                if value is not None:
                    data = value
                    break
            if data is None:
                return None, sample_rate, gate_peak

        array = np.asarray(data, dtype=np.float32).reshape(-1)
        return (array if array.size else None), sample_rate, gate_peak

    def _resample(self, waveform, sample_rate):
        """캡처가 16kHz가 아닐 때의 안전 경로 (audio-sensing 기본값은 16kHz)."""
        if sample_rate == self.sample_rate or waveform.size == 0:
            return waveform
        if not self._resample_warned:
            # cp949 콘솔에서도 깨지지 않도록 ASCII 기호만 쓴다
            print(f"[m3] sample_rate={sample_rate} != {self.sample_rate}, 선형 보간 리샘플 적용", flush=True)
            self._resample_warned = True
        duration = waveform.size / float(sample_rate)
        target_size = max(1, int(round(duration * self.sample_rate)))
        src = np.linspace(0.0, duration, num=waveform.size, endpoint=False, dtype=np.float64)
        dst = np.linspace(0.0, duration, num=target_size, endpoint=False, dtype=np.float64)
        return np.interp(dst, src, waveform).astype(np.float32)

    def _fit_window(self, waveform):
        """3초 윈도우 선택. 짧으면 앞쪽을 0으로 채운다.

        입력이 3초보다 길 때(audio-sensing VAD 이벤트는 최대 6초) 어디를 자르느냐가
        낙상 감지율을 좌우한다. 낙상은 '쿵' 이후 신음·뒤척임이 이어져 이벤트가
        길어지는데, 단순히 마지막 3초를 쓰면 정작 충격음이 창 밖으로 밀려난다.
        실측(낙상 50개): 충격이 창 안이면 46/50, 창 끝보다 3.5초 앞이면 12/50.

        그래서 기본값은 'peak' — 가장 큰 진폭(충격 후보)을 창 안에 넣되, 충격이
        창 끝에서 약 1.2초 앞에 오도록 앞쪽 여유를 둔다(실측 최적 구간 0.3~2.2초).
        M3_WINDOW_MODE=latest 로 두면 마지막 3초를 쓴다.
        """
        need = self.window_samples
        if waveform.size == need:
            return waveform
        if waveform.size < need:
            return np.pad(waveform, (need - waveform.size, 0), mode="constant")

        if self.window_mode != "peak":
            return waveform[-need:]

        peak_idx = int(np.argmax(np.abs(waveform)))
        lead = int(need * 0.6)  # 피크 앞쪽 여유 (3초 창 기준 약 1.8초)
        start = int(np.clip(peak_idx - lead, 0, waveform.size - need))
        return waveform[start:start + need]

    def _preprocess(self, input_data):
        """(모델 입력 [1, N], 게이트용 peak 또는 None) 반환."""
        waveform, sample_rate, gate_peak = self._extract(input_data)
        if waveform is None:
            return None, gate_peak
        waveform = self._resample(waveform, sample_rate)
        waveform = self._fit_window(waveform)
        return np.clip(waveform, -1.0, 1.0).reshape(1, -1).astype(np.float32), gate_peak

    # ------------------------------------------------------------------ 추론

    def _classify_onnx(self, data):
        outputs = self.session.run(None, {self.input_name: data})
        named = dict(zip(self.output_names, outputs))

        probs = np.asarray(named.get("probs", outputs[0]), dtype=np.float32).reshape(-1)
        if probs.size != len(self.ENV_LABELS):
            raise ValueError(f"예상 출력 {len(self.ENV_LABELS)}개, 실제 {probs.size}개")

        raw_peak_out = named.get("raw_peak")
        if raw_peak_out is not None:
            raw_peak = float(np.asarray(raw_peak_out).reshape(-1)[0])
        else:  # raw_peak 출력이 없는 그래프 — 입력에서 직접 계산
            raw_peak = float(np.max(np.abs(data)))

        top = int(np.argmax(probs))
        return {
            "label": self.ENV_LABELS[top],
            "confidence": float(probs[top]),
            "probs": probs,
            "raw_peak": raw_peak,
        }

    def _heuristic_label(self, waveform):
        x = np.asarray(waveform, dtype=np.float32).reshape(-1)
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
        if energy > 0.6:
            return "impact", 0.7
        return "noise", 0.62

    def _result(self, label, confidence, source, probs=None, raw_peak=0.0, gated=False):
        impact_prob = float(probs[self.IMPACT_INDEX]) if probs is not None else 0.0
        result = {
            "env_sound_label": label,
            "env_sound_confidence": confidence,
            "env_sound_source": source,
            "activity": label,
            "activity_confidence": confidence,
            "raw_peak": round(float(raw_peak), 6),
            "impact_prob": round(impact_prob, 4),
            "impact_alert": bool(not gated and impact_prob >= self.impact_threshold),
            "silence_gated": bool(gated),
        }
        if probs is not None:
            result["env_sound_probs"] = {
                name: round(float(p), 4) for name, p in zip(self.ENV_LABELS, probs)
            }
            result["env_sound_model"] = self.model_name
        return result

    def infer(self, input_data):
        data, gate_peak = self._preprocess(input_data)
        if data is None:
            return {
                "env_sound_label": "silence",
                "env_sound_confidence": 0.0,
                "env_sound_source": "no-audio",
                "activity": "silence",
                "activity_confidence": 0.0,
                "raw_peak": 0.0,
                "impact_prob": 0.0,
                "impact_alert": False,
                "silence_gated": False,
            }

        onnx = None
        if self.session is not None:
            try:
                onnx = self._classify_onnx(data)
            except Exception as exc:
                print(f"[m3] onnx 추론 실패, 휴리스틱으로 대체: {exc}", flush=True)
                onnx = None

        if onnx is None:
            label, confidence = self._heuristic_label(data)
            peak = float(np.max(np.abs(data))) if gate_peak is None else gate_peak
            return self._result(label, confidence, "heuristic", raw_peak=peak)

        # silence 게이트: 학습 데이터에 silence가 없어 모델이 '조용함'을 예측하지 않는다.
        # 증폭 전 원본 peak으로 규칙 판정한다. sensing 이 보정 전 peak(raw_peak)을
        # 실어 보냈으면 그 값을 쓰고, 없으면 그래프가 돌려준 값으로 판정한다.
        peak = onnx["raw_peak"] if gate_peak is None else gate_peak
        if self.silence_gate > 0 and peak < self.silence_gate:
            return self._result("silence", 1.0, "onnx",
                                probs=onnx["probs"], raw_peak=peak, gated=True)

        return self._result(onnx["label"], onnx["confidence"], "onnx",
                            probs=onnx["probs"], raw_peak=peak)


class ActivityClassificationModel(EnvSoundAnalysisModel):
    """기존 코드 호환용 별칭 클래스."""
