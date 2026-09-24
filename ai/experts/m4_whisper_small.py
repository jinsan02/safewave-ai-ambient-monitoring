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

# ── 긴급 문장 유사 매칭 ────────────────────────────────────────────────────
# 오인식("넘어졌어요" → "나마졌어요")도 잡도록 한글을 자모로 풀어 편집거리로 비교한다.
# 문장 목록은 M4 평가셋(data/m4_eval_2398) 라벨의 긴급 키워드 묶음에서 가져왔다.
# 값 = 기존 키워드 줄기(emergency_score·M5가 쓰는 목록과 같은 이름).
EMERGENCY_PHRASES = {
    "도와주세요": "도와", "도와줘": "도와", "살려주세요": "살려", "살려줘": "살려", "사람살려": "살려",
    "신고해주세요": "119", "119불러줘": "119", "119불러주세요": "119",
    "구급차불러줘": "응급", "구급차불러주세요": "응급",
    "넘어졌어요": "넘어", "미끄러졌어요": "넘어", "쓰러졌어요": "넘어", "못일어나겠어요": "넘어",
    "불이야": "화재", "숨을못쉬겠어요": "응급",
}
# 오인식 흔적이 남은 전사도 잡되, 무관한 말은 걸리지 않을 유사도(09-24 평가셋·생활 소음 4시간으로 정함)
PHRASE_MIN_SIM = float(os.getenv("M4_PHRASE_MIN_SIM", "0.8"))
# 환각 판정 — 말이 아닌 것으로 보고 전사·키워드를 버린다(원문은 transcript_raw):
#   '말 없음' 확률 > NO_SPEECH_MAX, 또는 (확률 > SURE_SPEECH_MAX 이면서 토큰 평균 로그확률 < MIN_AVG_LOGPROB)
# 실제 발화는 '말 없음' 확률이 0.005 이하라, 오인식으로 확신도가 낮아도 버리지 않는다.
# 09-24 측정(생활 소음 4시간 VAD 이벤트 175개, 평가 발화 550개)으로 정함: 소음 163/175 버림,
# 실제 발화 버림 1/550(규칙 '0.1 또는 -0.5'는 11/550).
NO_SPEECH_MAX = float(os.getenv("M4_NO_SPEECH_MAX", "0.05"))
SURE_SPEECH_MAX = float(os.getenv("M4_SURE_SPEECH_MAX", "0.005"))
MIN_AVG_LOGPROB = float(os.getenv("M4_MIN_AVG_LOGPROB", "-0.5"))

_CHO = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"
_JUNG = "ㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣ"
_JONG = " ㄱㄲㄳㄴㄵㄶㄷㄹㄺㄻㄼㄽㄾㄿㅀㅁㅂㅄㅅㅆㅇㅈㅊㅋㅌㅍㅎ"


# 받침은 대표음으로(겹받침 포함) — 발음 기준 비교
_JONG_SOUND = {"ㄲ": "ㄱ", "ㄳ": "ㄱ", "ㄵ": "ㄴ", "ㄶ": "ㄴ", "ㄺ": "ㄱ", "ㄻ": "ㅁ", "ㄼ": "ㄹ", "ㄽ": "ㄹ",
               "ㄾ": "ㄹ", "ㄿ": "ㅂ", "ㅀ": "ㄹ", "ㅄ": "ㅂ", "ㅅ": "ㄷ", "ㅆ": "ㄷ", "ㅈ": "ㄷ", "ㅊ": "ㄷ",
               "ㅋ": "ㄱ", "ㅌ": "ㄷ", "ㅍ": "ㅂ", "ㅎ": "ㄷ"}
# 음성 인식이 잘 헷갈리는 비슷한 소리 — 서로 바뀌면 편집 비용 0.5
_NEAR = [set("ㅏㅓㅑㅕ"), set("ㅗㅜㅛㅠ"), set("ㅐㅔㅒㅖ"), set("ㅚㅙㅞㅟ"), set("ㅘㅝ"), set("ㅡㅣㅢ"),
         set("ㄱㄲㅋ"), set("ㄷㄸㅌ"), set("ㅂㅃㅍ"), set("ㅅㅆ"), set("ㅈㅉㅊ")]


def to_jamo(text):
    """한글 → 발음에 가까운 자모열. 첫소리 ㅇ(소리 없음)은 빼고, 받침은 대표음으로 바꿔
    다음 음절 첫소리와 같은 기호로 둔다(연음: 넘어 → ㄴㅓㅁㅓ = 너머). 공백·문장부호는 뺀다."""
    out = []
    for ch in str(text):
        code = ord(ch) - 0xAC00
        if 0 <= code < 11172:
            cho = _CHO[code // 588]
            if cho != "ㅇ":
                out.append(cho)
            out.append(_JUNG[(code % 588) // 28])
            if code % 28:
                jong = _JONG[code % 28]
                out.append(_JONG_SOUND.get(jong, jong))
        elif ch.isalnum():
            out.append(ch)
    return out


def _sub_cost(a, b):
    if a == b:
        return 0.0
    return 0.5 if any(a in g and b in g for g in _NEAR) else 1.0


def phrase_similarity(phrase, text):
    """text 안 어느 부분과 phrase가 가장 가까운지 — 1 - (최소 편집 비용 / phrase 자모 수)."""
    p, t = to_jamo(phrase), to_jamo(text)
    if not p or not t:
        return 0.0
    prev = [0.0] * (len(t) + 1)               # 부분 문자열 매칭: text 어디서든 시작 가능
    for i, pc in enumerate(p, 1):
        cur = [float(i)] + [0.0] * len(t)
        for j, tc in enumerate(t, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + _sub_cost(pc, tc))
        prev = cur
    sim = max(0.0, 1.0 - min(prev) / len(p))
    # 짧은 문장(자모 8개 미만, 예: 도와줘·살려줘)은 한 글자만 달라도 다른 말이 된다 → 거의 그대로여야 인정
    return sim if len(p) >= 8 or sim >= 0.95 else 0.0


def match_emergency_phrase(text):
    """가장 가까운 긴급 문장과 유사도. 없으면 ("", 0.0)."""
    best, best_sim = "", 0.0
    for phrase in EMERGENCY_PHRASES:
        sim = phrase_similarity(phrase, text)
        if sim > best_sim:
            best, best_sim = phrase, sim
    return best, round(best_sim, 3)


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

            first = providers[0][0] if isinstance(providers[0], tuple) else providers[0]
            if first != "CPUExecutionProvider":
                kwargs["provider"] = first

            # encoder/decoder/decoder_with_past 3세션 전부에 스레드·스핀 설정 적용
            # 세션별 스핀을 제한하기 위한 설계. RPi5의 post-change 절감량은 별도 측정 필요.
            kwargs["session_options"] = get_session_opts(int(os.getenv("M4_ORT_THREADS", "2")))

            model = ORTModelForSpeechSeq2Seq.from_pretrained(self.model_path, **kwargs)
            self.asr_pipe = pipeline(
                task="automatic-speech-recognition",
                model=model,
                tokenizer=processor.tokenizer,
                feature_extractor=processor.feature_extractor,
                # 지정하지 않으면 pipeline이 모델을 CPU로 옮겨 CUDA EP가 꺼진다. CPU 모드에서는 cpu 그대로.
                device=model.device,
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
        """Whisper 전사 + 환각 판정용 지표. 반환 (text, conf, source), 지표는 self._last_stt_meta.

        pipeline 대신 encoder 1회 → ① 첫 위치 '말 없음'(<|nocaptions|>) 확률(decoder 1스텝)
        ② 같은 encoder 출력으로 generate(전사는 pipeline과 동일) ③ 토큰 로그확률 평균.
        Whisper는 무음·잡음에도 문장을 만든다("MBC 뉴스 ○○○입니다", 무음 → "도와주셨습니다").
        09-24 재현: 실제 음성 no_speech ≤ 0.0012, 무음·잡음 0.003~0.78(중앙값 0.37~0.78).
        """
        self._last_stt_meta = {}
        if not self._asr_init_attempted:
            self._init_asr_pipeline()
        if self.asr_pipe is None or waveform is None or waveform.size == 0:
            return None, None, None
        try:
            import torch
            model = self.asr_pipe.model
            fe, tok = self.asr_pipe.feature_extractor, self.asr_pipe.tokenizer
            feats = fe(waveform.astype(np.float32), sampling_rate=16000,
                       return_tensors="pt").input_features.to(model.device)
            with torch.no_grad():
                enc = model.encoder(input_features=feats, attention_mask=None)
                sot = tok.convert_tokens_to_ids("<|startoftranscript|>")
                first = model.decoder(input_ids=torch.tensor([[sot]], device=model.device),
                                      encoder_hidden_states=enc.last_hidden_state)
                no_speech = float(torch.softmax(first.logits[0, -1].float(), -1)[self._nospeech_id(tok)])
                gen = model.generate(feats, encoder_outputs=enc, language="ko", task="transcribe",
                                     num_beams=1, max_new_tokens=int(os.getenv("M4_MAX_NEW_TOKENS", "48")),
                                     return_dict_in_generate=True, output_scores=True)
                lp = model.compute_transition_scores(gen.sequences, gen.scores, normalize_logits=True)[0]
                lp = lp[torch.isfinite(lp)]
                avg_logprob = float(lp.mean()) if lp.numel() else -10.0
            text = tok.batch_decode(gen.sequences, skip_special_tokens=True)[0].strip()
            # 실제 확신도 = (1 - 말 없음 확률) × 토큰 평균 확률 (기존 0.75 고정값 대체)
            conf = float(np.clip((1.0 - no_speech) * np.exp(avg_logprob), 0.0, 1.0))
            self._last_stt_meta = {"no_speech_prob": round(no_speech, 4),
                                   "avg_logprob": round(avg_logprob, 3), "raw_text": text}
            return text, conf, "whisper-stt"
        except Exception:
            return None, None, None

    @staticmethod
    def _nospeech_id(tok):
        for t in ("<|nocaptions|>", "<|nospeech|>"):
            i = tok.convert_tokens_to_ids(t)
            if i is not None and i != tok.unk_token_id:
                return i
        raise KeyError("no-speech token")

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

        meta = (getattr(self, "_last_stt_meta", None) or {}) if direct_text is None else {}
        filtered = bool(meta) and (meta["no_speech_prob"] > NO_SPEECH_MAX
                                   or (meta["no_speech_prob"] > SURE_SPEECH_MAX
                                       and meta["avg_logprob"] < MIN_AVG_LOGPROB))
        if filtered:
            text = ""          # 말이 아닌 구간(환각) — 전사·키워드를 버린다

        speech_detected = bool(text) or occupancy_score >= 0.2
        keywords = self._extract_keywords(text)
        phrase, phrase_sim = match_emergency_phrase(text) if text else ("", 0.0)
        phrase_hit = phrase_sim >= PHRASE_MIN_SIM
        if phrase_hit and EMERGENCY_PHRASES[phrase] not in keywords:
            keywords.append(EMERGENCY_PHRASES[phrase])
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
            # 긴급 문장 유사 매칭(자모 편집거리). emergency_phrase_detected면 규칙 게이트가 긴급 음성으로 본다
            "emergency_phrase": phrase,
            "emergency_phrase_sim": phrase_sim,
            "emergency_phrase_detected": phrase_hit,
            # 환각 판정 지표와 버리기 전 원문
            "no_speech_prob": meta.get("no_speech_prob"),
            "avg_logprob": meta.get("avg_logprob"),
            "hallucination_filtered": filtered,
            "transcript_raw": meta.get("raw_text", text),
            "infer_confidence": round(infer_conf, 3),
            # 하위 호환 키 (기존 occupancy UI/로직 유지)
            "occupied": speech_detected,
            "occupancy_score": float(np.clip(occupancy_score, 0.0, 1.0)),
        }
