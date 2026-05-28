"""M3 AST 기반 환경음 분석 전문가 모델.

Hugging Face AST(Audio Spectrogram Transformer)를 로드해
waveform → mel spectrogram 전처리 후 환경음 라벨/신뢰도를 반환한다.
"""

import os

import numpy as np

DEFAULT_HF_MODEL_ID = "MIT/ast-finetuned-audioset-10-10-0.4593"
DEFAULT_SAMPLE_RATE = 16000
ENV_SOUND_LABELS = ("silence", "speech", "impact", "noise", "alarm", "unknown")


def _normalize_quiet_waveform(waveform: np.ndarray) -> np.ndarray:
    """멀리서/작게 들리는 신호를 AST 입력 전에 보정한다."""
    if os.getenv("M3_GAIN_NORMALIZE", "1") == "0":
        return waveform

    x = np.asarray(waveform, dtype=np.float32).reshape(-1)
    if x.size == 0:
        return x

    peak = float(np.max(np.abs(x)))
    if peak <= 1e-9:
        return x

    target_peak = float(os.getenv("M3_TARGET_PEAK", "0.9"))
    normalize_below = float(os.getenv("M3_NORMALIZE_BELOW_PEAK", "0.15"))
    if peak >= normalize_below:
        return x

    scaled = x * (target_peak / peak)
    return np.clip(scaled, -1.0, 1.0).astype(np.float32)


def _map_audioset_label(label: str) -> str:
    text = label.lower()
    if any(k in text for k in ("silence", "quiet", "mute")):
        return "silence"
    if any(
        k in text
        for k in (
            "speech",
            "conversation",
            "narration",
            "shout",
            "yell",
            "scream",
            "whisper",
            "babbling",
            "chatter",
            "talk",
        )
    ):
        return "speech"
    if any(k in text for k in ("music", "singing", "song", "melody", "musical", "television", "tv")):
        return "noise"
    if any(
        k in text
        for k in (
            "siren",
            "alarm",
            "smoke detector",
            "fire alarm",
            "car alarm",
            "buzzer",
            "beep",
            "ringtone",
        )
    ):
        return "alarm"
    if any(
        k in text
        for k in (
            "crash",
            "bang",
            "slam",
            "thump",
            "knock",
            "impact",
            "explosion",
            "glass",
            "smash",
            "thunder",
            "gunshot",
        )
    ):
        return "impact"
    if any(k in text for k in ("noise", "static", "hum", "white noise", "background")):
        return "noise"
    return "unknown"


class EnvSoundAnalysisModel:
    ENV_LABELS = list(ENV_SOUND_LABELS)

    def __init__(self, model_path):
        self.model_path = model_path
        self.hf_model_id = os.getenv("M3_HF_MODEL_ID", DEFAULT_HF_MODEL_ID)
        self.sample_rate = int(os.getenv("M3_SAMPLE_RATE", str(DEFAULT_SAMPLE_RATE)))

        self.extractor = None
        self.model = None
        self.id2label: dict[int, str] = {}
        self.device = None
        self.backend = "heuristic"

        self._init_hf_model()

    def _resolve_hf_source(self) -> str:
        if os.path.isdir(self.model_path):
            if os.path.exists(os.path.join(self.model_path, "config.json")):
                return self.model_path
        if os.path.isdir(os.path.join(self.model_path, "ast_hf")):
            nested = os.path.join(self.model_path, "ast_hf")
            if os.path.exists(os.path.join(nested, "config.json")):
                return nested
        return self.hf_model_id

    def _init_hf_model(self) -> None:
        try:
            import torch
            from transformers import ASTForAudioClassification, AutoFeatureExtractor
        except ImportError:
            return

        source = self._resolve_hf_source()
        try:
            self.extractor = AutoFeatureExtractor.from_pretrained(source)
            self.model = ASTForAudioClassification.from_pretrained(source)
            self.model.eval()
            self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
            self.model.to(self.device)
            raw_id2label = getattr(self.model.config, "id2label", {}) or {}
            self.id2label = {int(k): str(v) for k, v in raw_id2label.items()}
            self.backend = "hf-ast"
        except Exception:
            self.extractor = None
            self.model = None
            self.id2label = {}
            self.backend = "heuristic"

    def _extract_waveform(self, input_data) -> np.ndarray | None:
        if input_data is None:
            return None
        if isinstance(input_data, dict):
            for key in ("waveform", "samples", "audio", "pcm"):
                value = input_data.get(key)
                if value is not None:
                    array = np.asarray(value, dtype=np.float32).reshape(-1)
                    return array if array.size else None
            return None
        array = np.asarray(input_data, dtype=np.float32).reshape(-1)
        return array if array.size else None

    def _heuristic_label(self, waveform: np.ndarray) -> tuple[str, float]:
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
        if zcr < 0.05 and energy > 0.25:
            return "noise", 0.62
        if energy > 0.6:
            return "impact", 0.7
        return "noise", 0.62

    def _aggregate_env_scores(self, probs: np.ndarray) -> tuple[str, float, str, float]:
        env_scores = {label: 0.0 for label in self.ENV_LABELS}
        for idx, prob in enumerate(probs.reshape(-1)):
            audioset_label = self.id2label.get(idx, f"class_{idx}")
            env_label = _map_audioset_label(audioset_label)
            env_scores[env_label] = max(env_scores[env_label], float(prob))

        best_label = max(env_scores, key=env_scores.get)
        best_conf = float(env_scores[best_label])
        top_idx = int(np.argmax(probs))
        top_audioset = self.id2label.get(top_idx, f"class_{top_idx}")
        top_conf = float(probs.reshape(-1)[top_idx])
        return best_label, best_conf, top_audioset, top_conf

    def _classify_hf(self, waveform: np.ndarray) -> dict | None:
        if self.model is None or self.extractor is None:
            return None

        import torch

        inputs = self.extractor(
            waveform,
            sampling_rate=self.sample_rate,
            return_tensors="pt",
        )
        inputs = {k: v.to(self.device) for k, v in inputs.items()}

        with torch.no_grad():
            logits = self.model(**inputs).logits

        probs = torch.softmax(logits, dim=-1).cpu().numpy()
        env_label, env_conf, top_audioset, top_conf = self._aggregate_env_scores(probs[0])

        return {
            "env_sound_label": env_label,
            "env_sound_confidence": env_conf,
            "env_sound_source": "hf-ast",
            "ast_top_class": top_audioset,
            "ast_top_confidence": top_conf,
        }

    def infer(self, input_data):
        waveform = self._extract_waveform(input_data)
        if waveform is None:
            return {
                "env_sound_label": "silence",
                "env_sound_confidence": 0.0,
                "env_sound_source": "no-audio",
                "activity": "silence",
                "activity_confidence": 0.0,
            }

        waveform = _normalize_quiet_waveform(waveform)

        hf_result = None
        if self.backend == "hf-ast":
            try:
                hf_result = self._classify_hf(waveform)
            except Exception:
                hf_result = None

        if hf_result is not None:
            label = hf_result["env_sound_label"]
            confidence = float(hf_result["env_sound_confidence"])
            result = {
                "env_sound_label": label,
                "env_sound_confidence": confidence,
                "env_sound_source": hf_result["env_sound_source"],
                "activity": label,
                "activity_confidence": confidence,
                "ast_top_class": hf_result["ast_top_class"],
                "ast_top_confidence": hf_result["ast_top_confidence"],
            }
            return result

        label, confidence = self._heuristic_label(waveform)
        return {
            "env_sound_label": label,
            "env_sound_confidence": confidence,
            "env_sound_source": "heuristic",
            "activity": label,
            "activity_confidence": confidence,
        }


class ActivityClassificationModel(EnvSoundAnalysisModel):
    """기존 코드 호환용 별칭 클래스."""
