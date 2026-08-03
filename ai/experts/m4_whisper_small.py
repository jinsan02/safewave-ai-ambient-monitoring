"""M4 Whisper-Small 기반 한국어 음성인식(STT) 전문가 모델.

Whisper ONNX 아티팩트를 이용해 한국어 전사를 시도하고,
전사 텍스트/신뢰도/음성감지 결과를 반환한다.
"""

import os

import numpy as np
import onnxruntime as ort

from utils import get_ort_providers, get_session_opts

try:
    from optimum.onnxruntime import ORTModelForSpeechSeq2Seq
    from transformers import AutoProcessor, pipeline
except Exception:
    ORTModelForSpeechSeq2Seq = None
    AutoProcessor = None
    pipeline = None

class WhisperSmallModel:
    def __init__(self, model_path):
        self.model_path = model_path
        self.effective_model_path = model_path
        self.session = None
        self.asr_pipe = None
        self._asr_init_attempted = False

        if os.path.isdir(self.model_path):
            self.effective_model_path = os.path.join(self.model_path, "encoder_model.onnx")

        if os.path.exists(self.effective_model_path):
            self.session = ort.InferenceSession(
                self.effective_model_path,
                providers=get_ort_providers(),
                # Phase 2 15s 응답 윈도우 보호 — 1스레드 강제 시 STT 수십 초 위험 → 기본 2
                sess_options=get_session_opts(int(os.getenv("M4_ORT_THREADS", "2"))),
            )

        # ASR 파이프라인을 시작 시점에 즉시 초기화 (lazy init 시 1s 타임아웃 초과로 전체 모델 블로킹)
        self._init_asr_pipeline()

    def _init_asr_pipeline(self):
        self._asr_init_attempted = True
        if not os.path.isdir(self.model_path):
            return
        if pipeline is None or AutoProcessor is None or ORTModelForSpeechSeq2Seq is None:
            return
        try:
            processor = AutoProcessor.from_pretrained(self.model_path)
            kwargs = {}
            providers = get_ort_providers()
            dec = os.path.join(self.model_path, "decoder_model.onnx")
            dec_past = os.path.join(self.model_path, "decoder_with_past_model.onnx")
            if os.path.exists(dec):
                kwargs["decoder_file_name"] = "decoder_model.onnx"
            if os.path.exists(dec_past):
                kwargs["decoder_with_past_file_name"] = "decoder_with_past_model.onnx"
            else:
                # decoder_with_past가 없는 2-file 구성: cache 비활성화 + io_binding 비활성화
                kwargs["use_cache"] = False
                kwargs["use_io_binding"] = False

            if providers[0] != "CPUExecutionProvider":
                kwargs["provider"] = providers[0]

            # encoder/decoder/decoder_with_past 3세션 전부에 스레드·스핀 설정 적용
            # (미적용 시 세션당 기본 4워커 스핀 — RPi5 CPU 낭비의 최대 단일 소스)
            kwargs["session_options"] = get_session_opts(int(os.getenv("M4_ORT_THREADS", "2")))

            model = ORTModelForSpeechSeq2Seq.from_pretrained(self.model_path, **kwargs)
            self.asr_pipe = pipeline(
                task="automatic-speech-recognition",
                model=model,
                tokenizer=processor.tokenizer,
                feature_extractor=processor.feature_extractor,
            )
            # asr_pipe가 encoder+decoder를 모두 보유 → 단독 encoder 세션 해제 (337MB 절감)
            self.session = None
        except Exception:
            self.asr_pipe = None

    def _extract_waveform(self, input_data):
        if isinstance(input_data, dict):
            for key in ("transcript", "text", "text_ko"):
                txt = input_data.get(key)
                if isinstance(txt, str) and txt.strip():
                    return None, txt.strip(), 0.99, "upstream-text"
            for key in ("waveform", "samples", "audio", "pcm"):
                if key in input_data and input_data[key] is not None:
                    wav = np.asarray(input_data[key], dtype=np.float32).reshape(-1)
                    return wav, None, None, None
            return None, None, None, None

        wav = np.asarray(input_data, dtype=np.float32).reshape(-1)
        return wav, None, None, None

    def _preprocess(self, input_data):
        data = np.asarray(input_data, dtype=np.float32)
        if self.session is not None:
            input_meta = self.session.get_inputs()[0]
            shape = list(input_meta.shape)
            if len(shape) == 3:
                d1 = int(shape[1]) if isinstance(shape[1], int) and shape[1] > 0 else 80
                d2 = int(shape[2]) if isinstance(shape[2], int) and shape[2] > 0 else 3000
                flat = data.reshape(-1)
                need = d1 * d2
                if flat.size < need:
                    flat = np.pad(flat, (0, need - flat.size), mode="constant")
                elif flat.size > need:
                    flat = flat[:need]
                return flat.reshape(1, d1, d2).astype(np.float32)
        if data.ndim == 1:
            data = data.reshape(1, -1)
        return data

    def _predict_onnx(self, data):
        input_name = self.session.get_inputs()[0].name
        output = np.asarray(self.session.run(None, {input_name: data})[0]).reshape(-1)
        score = float(np.max(output)) if output.size > 0 else 0.0
        score = float(np.clip(score, 0.0, 1.0))
        return score

    def _extract_keywords(self, text):
        if not text:
            return []
        kws = ["살려", "도와", "아파", "응급", "위험", "넘어", "불", "화재", "119"]
        found = [kw for kw in kws if kw in text]
        return found[:5]

    def _predict_stt(self, waveform):
        if not self._asr_init_attempted:
            self._init_asr_pipeline()
        if self.asr_pipe is None or waveform is None or waveform.size == 0:
            return None, None, None
        try:
            result = self.asr_pipe(
                {"array": waveform.astype(np.float32), "sampling_rate": 16000},
                generate_kwargs={"language": "ko", "task": "transcribe",
                                 "num_beams": 1, "max_new_tokens": 128},
            )
            if isinstance(result, dict):
                text = str(result.get("text", "")).strip()
            else:
                text = str(result).strip()
            if not text:
                return "", 0.0, "whisper-stt"
            base_conf = 0.75
            if len(text) <= 2:
                base_conf = 0.55
            return text, base_conf, "whisper-stt"
        except Exception:
            return None, None, None

    def _predict_fallback(self, waveform):
        if waveform is None or waveform.size == 0:
            return "", 0.0, "fallback"
        score = float(np.clip(np.mean(np.abs(waveform)) * 3.0, 0.0, 1.0))
        if score < 0.15:
            return "", score, "fallback"
        return "(음성 감지, 전사 미확정)", min(0.6, score), "fallback"

    def infer(self, input_data):
        waveform, direct_text, direct_conf, direct_source = self._extract_waveform(input_data)

        if direct_text is not None:
            text = direct_text
            stt_conf = float(direct_conf)
            source = direct_source
            occupancy_score = stt_conf
        else:
            occupancy_score = 0.0
            if waveform is not None and self.session is not None:
                try:
                    enc_in = self._preprocess(waveform)
                    occupancy_score = self._predict_onnx(enc_in)
                except Exception:
                    occupancy_score = 0.0

            text, stt_conf, source = self._predict_stt(waveform)
            if text is None:
                text, stt_conf, source = self._predict_fallback(waveform)

        speech_detected = bool(text) or occupancy_score >= 0.2
        keywords = self._extract_keywords(text)
        stt_conf_val = float(np.clip(stt_conf if stt_conf is not None else 0.0, 0.0, 1.0))

        if source == "upstream-text":
            infer_conf = 0.99
        elif source == "whisper-stt":
            infer_conf = max(0.55, stt_conf_val)
        else:  # fallback / None
            infer_conf = min(0.45, stt_conf_val)

        return {
            "transcript_ko": text,
            "speech_detected": speech_detected,
            "stt_confidence": stt_conf_val,
            "stt_source": source,
            "language": "ko",
            "keywords": keywords,
            "infer_confidence": round(infer_conf, 3),
            # 하위 호환 키 (기존 occupancy UI/로직 유지)
            "occupied": speech_detected,
            "occupancy_score": float(np.clip(occupancy_score, 0.0, 1.0)),
        }
