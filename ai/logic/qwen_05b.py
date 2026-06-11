import logging
import os
import re
import time
import json
import numpy as np
import onnxruntime as ort

try:
    import redis
except Exception:
    redis = None

_LOGGER = logging.getLogger("rp5.ai.qwen")


class QwenLogic:
    """
    M5: Qwen-0.5B를 사용한 고급 위험도 평가 엔진

    Qwen2-0.5B-Instruct ONNX 모델을 활용하여 M1-M4(낙상, 생체신호, 환경음, 한국어 STT)
    의 결과를 분석하고 상황에 맞는 위험도 점수를 생성합니다.
    
    역할:
    - M1-M4 전문가 모델의 출력을 통합 분석
    - 시간 시리즈 맥락 반영
    - 응급 상황 감지 및 위험도 평가
    """
    
    def __init__(self, model_path):
        """
        Args:
            model_path: Qwen ONNX 모델 경로
                       - 폴더면: model.onnx, config.json, tokenizer.json 포함
                       - 파일면: ONNX 모델 파일 경로
        """
        self.model_path = model_path
        self.session = None
        self.tokenizer = None
        self.max_new_tokens = int(os.getenv("QWEN_MAX_NEW_TOKENS", "128"))
        self.max_new_tokens = max(96, min(192, self.max_new_tokens))
        self.hourly_window_ms = int(os.getenv("SLM_HOURLY_WINDOW_MS", "3600000"))
        self.hourly_result_scan_limit = int(os.getenv("SLM_HOURLY_RESULT_SCAN_LIMIT", "1800"))
        self.hourly_emergency_scan_limit = int(os.getenv("SLM_HOURLY_EMERGENCY_SCAN_LIMIT", "600"))
        self.hourly_speech_sample_limit = int(os.getenv("SLM_HOURLY_SPEECH_SAMPLE_LIMIT", "8"))
        self.hourly_event_sample_limit = int(os.getenv("SLM_HOURLY_EVENT_SAMPLE_LIMIT", "8"))
        self.hourly_cache_ms = int(os.getenv("SLM_HOURLY_CACHE_MS", "10000"))
        self.redis_client = None
        self._hourly_cache_at_ms = 0
        self._hourly_cache_data = None
        self._onnx_file = None
        self._model_dir = None
        self._load_attempted = False
        self.session_with_past = None
        self.feedback_topic_key = os.getenv("MQTT_FEEDBACK_REDIS_KEY", "mqtt:feedback:last")

        if redis is not None:
            try:
                self.redis_client = redis.Redis(
                    host=os.getenv("REDIS_HOST", "db"),
                    port=int(os.getenv("REDIS_PORT", "6379")),
                    decode_responses=False,
                    socket_connect_timeout=1,
                    socket_timeout=1,
                )
            except Exception:
                self.redis_client = None
        
        # 폴더인지 파일인지 확인
        if os.path.isdir(self.model_path):
            self._onnx_file = os.path.join(self.model_path, "model.onnx")
            self._model_dir = self.model_path
        else:
            self._onnx_file = self.model_path
            self._model_dir = None

    def _ensure_model_loaded(self):
        if self._load_attempted:
            return
        self._load_attempted = True
        if self._onnx_file and os.path.exists(self._onnx_file):
            self._load_model(self._onnx_file, self._model_dir)
    
    def _load_model(self, onnx_path, model_dir=None):
        """ONNX 모델 및 토크나이저 로드"""
        try:
            from utils import get_ort_providers
            providers = get_ort_providers()
            session_opts = ort.SessionOptions()
            session_opts.intra_op_num_threads = 4
            session_opts.inter_op_num_threads = 2
            
            self.session = ort.InferenceSession(
                onnx_path,
                providers=providers,
                sess_options=session_opts
            )
            _LOGGER.info("qwen_model_loaded path=%s", onnx_path)

            # decoder_with_past 모델 로드 (있으면 사용, 없으면 full-seq 폴백)
            if model_dir:
                with_past_path = os.path.join(model_dir, "model_with_past.onnx")
                if os.path.exists(with_past_path):
                    self.session_with_past = ort.InferenceSession(
                        with_past_path,
                        providers=providers,
                        sess_options=session_opts,
                    )
                    _LOGGER.info("qwen_with_past_loaded path=%s", with_past_path)

            # 토크나이저 로드
            if model_dir and os.path.exists(os.path.join(model_dir, "tokenizer.json")):
                try:
                    from transformers import AutoTokenizer
                    self.tokenizer = AutoTokenizer.from_pretrained(
                        model_dir,
                        trust_remote_code=True
                    )
                    _LOGGER.info("qwen_tokenizer_loaded path=%s", model_dir)
                except Exception as e:
                    _LOGGER.warning("qwen_tokenizer_failed error=%s", e)
                    self.tokenizer = None

        except Exception as e:
            _LOGGER.error("qwen_model_load_failed error=%s", e)
            self.session = None

    def _safe_float(self, value, default=0.0):
        try:
            if isinstance(value, dict):
                for key in ("value", "score", "mean", "avg"):
                    if key in value:
                        return float(value[key])
                return default
            if isinstance(value, (list, tuple)):
                if not value:
                    return default
                return float(value[0])
            return float(value)
        except Exception:
            return default

    def _stream_id_ts_ms(self, stream_id):
        if isinstance(stream_id, bytes):
            stream_id = stream_id.decode("utf-8", errors="ignore")
        try:
            return int(str(stream_id).split("-")[0])
        except Exception:
            return int(time.time() * 1000)

    def _series_trend_summary(self, series, label, unit):
        if len(series) < 3:
            return f"{label}: 데이터 부족"
        head_n = max(1, len(series) // 4)
        tail_n = max(1, len(series) // 4)
        start_mean = float(np.mean(series[:head_n]))
        end_mean = float(np.mean(series[-tail_n:]))
        full_mean = float(np.mean(series))
        delta = end_mean - start_mean
        direction = "상승" if delta > 1.0 else "하강" if delta < -1.0 else "안정"
        return (
            f"{label}: 시작 {start_mean:.1f}{unit}, 최근 {end_mean:.1f}{unit}, "
            f"평균 {full_mean:.1f}{unit}, 추세 {direction}"
        )

    def _fetch_hourly_context(self, now_ts_ms=None):
        now_ts_ms = int(now_ts_ms or (time.time() * 1000))
        if (
            self._hourly_cache_data is not None
            and self.hourly_cache_ms > 0
            and (now_ts_ms - self._hourly_cache_at_ms) <= self.hourly_cache_ms
        ):
            return dict(self._hourly_cache_data)

        since_ts_ms = now_ts_ms - self.hourly_window_ms

        context = {
            "window_minutes": int(self.hourly_window_ms / 60000),
            "warning_count": 0,
            "critical_count": 0,
            "heart_rate_trend": "심박 추세 데이터 없음",
            "breathing_rate_trend": "호흡 추세 데이터 없음",
            "speech_samples": [],
            "important_events": [],
            "sampled_result_points": 0,
        }

        if self.redis_client is None:
            return context

        try:
            result_entries = self.redis_client.xrevrange("ai:result", count=self.hourly_result_scan_limit)
            emergency_entries = self.redis_client.xrevrange("ai:emergency", count=self.hourly_emergency_scan_limit)
        except Exception:
            return context

        heart_rates = []
        breathing_rates = []
        speech_seen = set()

        for msg_id, fields in result_entries:
            ts_ms = self._stream_id_ts_ms(msg_id)
            if ts_ms < since_ts_ms:
                break

            payload_raw = fields.get(b"data", b"")
            if isinstance(payload_raw, bytes):
                payload_raw = payload_raw.decode("utf-8", errors="ignore")
            try:
                payload = json.loads(payload_raw) if payload_raw else {}
            except Exception:
                continue

            risk_level = str(payload.get("risk_level", "normal"))
            if risk_level == "critical":
                context["critical_count"] += 1
            elif risk_level == "warning":
                context["warning_count"] += 1

            experts = payload.get("experts", {})
            vital = experts.get("vital", {}) if isinstance(experts, dict) else {}
            hr = self._safe_float(vital.get("heart_rate"), default=-1.0)
            rr = self._safe_float(vital.get("breathing_rate"), default=-1.0)
            if hr >= 0.0:
                heart_rates.append(hr)
            if rr >= 0.0:
                breathing_rates.append(rr)

            speech = experts.get("speech_ko", {}) if isinstance(experts, dict) else {}
            transcript = str(speech.get("transcript_ko", "")).strip()
            if transcript and transcript not in speech_seen and len(context["speech_samples"]) < self.hourly_speech_sample_limit:
                speech_seen.add(transcript)
                context["speech_samples"].append(transcript[:64])

            context["sampled_result_points"] += 1

        for msg_id, fields in emergency_entries:
            ts_ms = self._stream_id_ts_ms(msg_id)
            if ts_ms < since_ts_ms:
                break

            payload_raw = fields.get(b"data", b"")
            if isinstance(payload_raw, bytes):
                payload_raw = payload_raw.decode("utf-8", errors="ignore")
            try:
                payload = json.loads(payload_raw) if payload_raw else {}
            except Exception:
                continue

            summary = str(payload.get("summary", "")).strip()
            if summary and len(context["important_events"]) < self.hourly_event_sample_limit:
                context["important_events"].append(summary[:96])

        context["heart_rate_trend"] = self._series_trend_summary(heart_rates, "심박", "bpm")
        context["breathing_rate_trend"] = self._series_trend_summary(breathing_rates, "호흡", "bpm")
        self._hourly_cache_at_ms = now_ts_ms
        self._hourly_cache_data = dict(context)
        return context
    
    def _build_analysis_prompt(self, expert_results, context_window=None, hourly_context=None):
        """few-shot 예시로 Qwen-0.5B가 실제 값을 생성하도록 유도."""
        fall = expert_results.get("fall", {})
        vital = expert_results.get("vital", {})
        env_sound = expert_results.get("env_sound", {})
        speech_ko = expert_results.get("speech_ko", {})

        hr = float(vital.get("heart_rate", 0.0) or 0.0)
        rr = float(vital.get("breathing_rate", 0.0) or 0.0)
        fall_score = float(fall.get("fall_score", 0.0) or 0.0)
        fall_detected = bool(fall.get("fall_detected", False))
        env_label = str(env_sound.get("env_sound_label", "unknown"))
        transcript = str(speech_ko.get("transcript_ko", "")).strip()

        findings = []
        if fall_detected:
            findings.append("낙상감지")
        if hr and (hr < 60 or hr > 100):
            findings.append(f"심박이상(hr={hr:.0f})")
        if rr and (rr < 12 or rr > 25):
            findings.append(f"호흡이상(rr={rr:.0f})")
        if env_label in {"impact", "alarm"}:
            findings.append(f"위험음({env_label})")
        if transcript and any(kw in transcript for kw in ["살려", "도와", "응급", "위험", "119", "불", "화재"]):
            findings.append("긴급키워드")

        findings_str = ", ".join(findings) if findings else "정상"

        ctx_note = ""
        if context_window:
            cc = int(context_window.get("recent_critical_count", 0))
            wc = int(context_window.get("recent_warning_count", 0))
            if cc or wc:
                ctx_note = f", 최근이력:critical={cc},warning={wc}"
        if hourly_context:
            hc = int(hourly_context.get("critical_count", 0))
            hw = int(hourly_context.get("warning_count", 0))
            if hc or hw:
                ctx_note += f", 1h:c={hc},w={hw}"

        return (
            "[예시]\n"
            "낙상:False(2%), 심박:72, 호흡:15, 환경음:silence, 이상소견:정상\n"
            '-> {"risk_score":0.1,"risk_level":"normal","is_outlier":false,'
            '"correlated_with_history":false,"reason":"정상범위"}\n\n'
            "낙상:True(91%), 심박:45, 호흡:8, 환경음:impact, 이상소견:낙상감지,심박이상,위험음\n"
            '-> {"risk_score":0.95,"risk_level":"critical","is_outlier":false,'
            '"correlated_with_history":false,"reason":"낙상+심박이상+위험음"}\n\n'
            f"[현재] 낙상:{fall_detected}({fall_score:.0%}), 심박:{hr:.0f}, 호흡:{rr:.0f}, "
            f"환경음:{env_label}, 이상소견:{findings_str}{ctx_note}\n"
            "->"
        )
    
    def _extract_risk_score(self, response_text):
        """
        응답에서 위험도 점수 추출
        """
        # 첫 번째: 0~1 사이의 소수 찾기
        match = re.search(r'0\.\d+|1\.0|1', response_text.strip())
        if match:
            try:
                score = float(match.group())
                return float(np.clip(score, 0.0, 1.0))
            except:
                pass
        
        # 두 번째: 텍스트 기반 휴리스틱
        text_lower = response_text.lower()
        if "긴급" in text_lower or "응급" in text_lower or "즉시" in text_lower:
            return 0.85
        elif "경고" in text_lower or "주의" in text_lower or "주의필요" in text_lower:
            return 0.65
        elif "정상" in text_lower or "안전" in text_lower or "이상없" in text_lower:
            return 0.2
        
        # 기본값
        return 0.5

    def _parse_qwen_json_response(self, response_text):
        if not response_text:
            return None
        try:
            start = response_text.find("{")
            end = response_text.rfind("}")
            if start < 0 or end < start:
                return None
            obj = json.loads(response_text[start:end + 1])
            score = self._safe_float(obj.get("risk_score"), default=-1.0)
            if score < 0.0:
                return None
            score = float(np.clip(score, 0.0, 1.0))
            level = str(obj.get("risk_level", "")).strip().lower()
            if level not in {"normal", "warning", "critical"}:
                level = "critical" if score >= 0.85 else "warning" if score >= 0.6 else "normal"
            return {
                "risk_score": score,
                "risk_level": level,
                "is_outlier": bool(obj.get("is_outlier", False)),
                "correlated_with_history": bool(obj.get("correlated_with_history", False)),
                "reason": str(obj.get("reason", "")).strip(),
            }
        except Exception:
            return None
    
    def _build_prefill_feed(self, input_ids, attention_mask):
        seq_len = input_ids.shape[1]
        position_ids = np.arange(seq_len, dtype=np.int64).reshape(1, -1)
        valid = {inp.name for inp in self.session.get_inputs()}
        feed = {}
        if "input_ids" in valid:
            feed["input_ids"] = input_ids
        if "attention_mask" in valid:
            feed["attention_mask"] = attention_mask
        if "position_ids" in valid:
            feed["position_ids"] = position_ids
        return feed

    def _generate_full_seq(self, input_ids, attention_mask):
        """KV 캐시 없이 매 스텝 전체 시퀀스 재계산 (model_with_past 없을 때 폴백)."""
        generated = []
        for _ in range(self.max_new_tokens):
            feed = self._build_prefill_feed(input_ids, attention_mask)
            outputs = self.session.run(None, feed)
            next_token_id = int(np.argmax(outputs[0][0, -1, :]))
            generated.append(next_token_id)
            input_ids = np.concatenate(
                [input_ids, np.array([[next_token_id]], dtype=np.int64)], axis=1
            )
            attention_mask = np.concatenate(
                [attention_mask, np.ones((1, 1), dtype=np.int64)], axis=1
            )
            if self.tokenizer.eos_token_id is not None and next_token_id == self.tokenizer.eos_token_id:
                break
        response = self.tokenizer.decode(generated, skip_special_tokens=True).strip()
        return response if response else None

    def _generate_with_past(self, input_ids, attention_mask):
        """decoder_with_past KV 캐시 방식: prefill 1회 + 스텝마다 단일 토큰 추론."""
        # Prefill: 전체 프롬프트 → logits + present KV
        feed = self._build_prefill_feed(input_ids, attention_mask)
        prefill_out = self.session.run(None, feed)
        out_names = [o.name for o in self.session.get_outputs()]

        next_token_id = int(np.argmax(prefill_out[0][0, -1, :]))
        generated = [next_token_id]

        # present.X.key/value 딕셔너리
        present_kv = {name: prefill_out[i] for i, name in enumerate(out_names) if name != "logits"}

        if self.tokenizer.eos_token_id and next_token_id == self.tokenizer.eos_token_id:
            return self.tokenizer.decode(generated, skip_special_tokens=True).strip() or None

        with_past_in_names = {inp.name for inp in self.session_with_past.get_inputs()}
        with_past_out_names = [o.name for o in self.session_with_past.get_outputs()]
        past_seq_len = input_ids.shape[1]

        for _ in range(self.max_new_tokens - 1):
            total_len = past_seq_len + len(generated)
            step_feed = {}
            if "input_ids" in with_past_in_names:
                step_feed["input_ids"] = np.array([[next_token_id]], dtype=np.int64)
            if "attention_mask" in with_past_in_names:
                step_feed["attention_mask"] = np.ones((1, total_len), dtype=np.int64)
            if "position_ids" in with_past_in_names:
                step_feed["position_ids"] = np.array([[total_len - 1]], dtype=np.int64)
            # present.X.key → past_key_values.X.key 매핑
            for inp_name in with_past_in_names:
                if inp_name in step_feed:
                    continue
                present_name = inp_name.replace("past_key_values", "present")
                if present_name in present_kv:
                    step_feed[inp_name] = present_kv[present_name]

            step_out = self.session_with_past.run(None, step_feed)
            next_token_id = int(np.argmax(step_out[0][0, -1, :]))
            generated.append(next_token_id)

            # present KV 갱신
            present_kv = {name: step_out[i] for i, name in enumerate(with_past_out_names) if name != "logits"}

            if self.tokenizer.eos_token_id and next_token_id == self.tokenizer.eos_token_id:
                break

        response = self.tokenizer.decode(generated, skip_special_tokens=True).strip()
        return response if response else None

    def _evaluate_with_qwen(self, prompt_text):
        if not self.session or not self.tokenizer:
            return None
        try:
            system_content = (
                "독거인 안전 모니터링 AI. 센서 데이터 분석 후 JSON만 출력.\n"
                '스키마: {"risk_score":float,"risk_level":"normal|warning|critical",'
                '"is_outlier":bool,"correlated_with_history":bool,"reason":"str"}\n'
                "normal(<0.6), warning(0.6~0.85), critical(≥0.85)"
            )

            if hasattr(self.tokenizer, "apply_chat_template"):
                messages = [
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": prompt_text},
                ]
                formatted = self.tokenizer.apply_chat_template(
                    messages, tokenize=False, add_generation_prompt=True
                )
            else:
                # apply_chat_template 없을 때 Qwen-Instruct 포맷 직접 구성
                formatted = (
                    f"<|im_start|>system\n{system_content}<|im_end|>\n"
                    f"<|im_start|>user\n{prompt_text}<|im_end|>\n"
                    f"<|im_start|>assistant\n"
                )

            # JSON prefix forcing: { 를 입력에 추가해 모델이 JSON으로 시작하도록 강제
            formatted += "{"

            inputs = self.tokenizer(
                formatted, return_tensors="np", truncation=True, max_length=640
            )
            input_ids = inputs["input_ids"].astype(np.int64)
            attention_mask = inputs.get("attention_mask")
            if attention_mask is None:
                attention_mask = np.ones_like(input_ids, dtype=np.int64)
            else:
                attention_mask = attention_mask.astype(np.int64)

            if self.session_with_past is not None:
                raw = self._generate_with_past(input_ids, attention_mask)
            else:
                raw = self._generate_full_seq(input_ids, attention_mask)

            if not raw:
                return None
            # 모델이 { 를 중복 생성했을 경우 정규화
            return "{" + raw.lstrip("{")
        except Exception as e:
            _LOGGER.error("qwen_infer_failed error=%s", e)
            return None
    
    def _evaluate_fallback(self, expert_results):
        """
        Qwen 모델이 없을 때 사용할 규칙 기반 평가
        """
        fall = expert_results.get("fall", {})
        vital = expert_results.get("vital", {})
        env_sound = expert_results.get("env_sound", {})
        speech_ko = expert_results.get("speech_ko", {})
        
        risk = 0.0

        def _as_float(value, default=0.0):
            try:
                if isinstance(value, dict):
                    for key in ("value", "score", "mean", "avg"):
                        if key in value:
                            return float(value[key])
                    return default
                if isinstance(value, (list, tuple)):
                    if not value:
                        return default
                    return float(value[0])
                return float(value)
            except Exception:
                return default
        
        # 낙상 감지: 매우 높은 위험
        if fall.get("fall_detected", False):
            risk = max(risk, 0.9)
        else:
            risk += _as_float(fall.get("fall_score", 0.0), 0.0) * 0.3
        
        # 생체신호 이상: 중간~높은 위험
        hr = _as_float(vital.get("heart_rate", 70.0), 70.0)
        rr = _as_float(vital.get("breathing_rate", 16.0), 16.0)
        
        if hr < 50 or hr > 120 or rr < 10 or rr > 30:
            risk = max(risk, 0.75)
        elif hr < 60 or hr > 100 or rr < 12 or rr > 25:
            risk = max(risk, 0.55)
        
        # 환경음/음성 기반 보조 지표
        env_label = env_sound.get("env_sound_label", "unknown")
        if env_label in {"impact", "alarm"}:
            risk = max(risk, 0.7)

        transcript = str(speech_ko.get("transcript_ko", ""))
        keywords = ["살려", "도와", "응급", "위험", "119", "불", "화재"]
        if any(kw in transcript for kw in keywords):
            risk = max(risk, 0.85)
        
        return float(np.clip(risk, 0.0, 1.0))
    
    def _apply_context_window(self, risk_score, context_window):
        """
        시간 시리즈 맥락 적용
        
        최근 연속 경고/긴급 상태를 고려하여 위험도 조정
        """
        if not context_window:
            return risk_score
        
        critical_count = int(context_window.get("recent_critical_count", 0))
        warning_count = int(context_window.get("recent_warning_count", 0))
        
        # 최근 긴급 상태가 연속이면 높음
        if critical_count > 1:
            risk_score = max(risk_score, 0.9)
        elif critical_count > 0:
            risk_score = max(risk_score, 0.85)
        
        # 최근 경고가 3번 이상 연속이면 위험도 상향
        if warning_count >= 3:
            risk_score = min(1.0, risk_score + 0.1)
        
        return float(np.clip(risk_score, 0.0, 1.0))

    def _apply_hourly_fallback_weight(self, risk_score, hourly_context, expert_results):
        """Qwen 폴백 경로에서 1시간 시계열 맥락을 더 강하게 반영한다."""
        if not hourly_context:
            return float(np.clip(risk_score, 0.0, 1.0))

        warning_count = int(hourly_context.get("warning_count", 0))
        critical_count = int(hourly_context.get("critical_count", 0))
        speech_samples = hourly_context.get("speech_samples", [])

        weighted = float(risk_score)
        if warning_count >= 3:
            weighted *= 1.2
        if critical_count >= 1:
            weighted *= 1.1

        # 과거 발화 이력이 있고 현재도 음성 위험 키워드가 있으면 추가 가중
        speech = expert_results.get("speech_ko", {}) if isinstance(expert_results, dict) else {}
        transcript = str(speech.get("transcript_ko", "")).strip()
        if speech_samples and transcript:
            keywords = ("살려", "도와", "응급", "위험", "119", "불", "화재")
            if any(k in transcript for k in keywords):
                weighted += 0.08

        return float(np.clip(weighted, 0.0, 1.0))

    def _apply_feedback_adjustment(self, risk_score):
        if self.redis_client is None:
            return float(np.clip(risk_score, 0.0, 1.0))

        try:
            raw = self.redis_client.get(self.feedback_topic_key)
            if not raw:
                return float(np.clip(risk_score, 0.0, 1.0))
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="ignore")
            payload = json.loads(raw)
        except Exception:
            return float(np.clip(risk_score, 0.0, 1.0))

        feedback = str(payload.get("feedback", "")).lower().strip()
        delta = self._safe_float(payload.get("delta"), default=0.0)
        if delta == 0.0:
            if feedback in {"up", "missed_alert", "positive"}:
                delta = 0.08
            elif feedback in {"down", "false_alarm", "negative"}:
                delta = -0.08
        adjusted = float(np.clip(risk_score + delta, 0.0, 1.0))
        return adjusted
    
    def evaluate(self, expert_results, context_window=None):
        """
        최종 위험도 평가
        
        Args:
            expert_results: M1-M4 전문가 모델의 결과
            context_window: 시간 시리즈 맥락 (최근 경고/긴급 카운트 등)
        
        Returns:
            {
                "emergency": bool,
                "risk_level": "normal" | "warning" | "critical",
                "risk_score": float (0-1),
                "experts": dict,
                "context_used": bool,
                "qwen_response": str (optional)
            }
        """
        
        self._ensure_model_loaded()
        hourly_context = self._fetch_hourly_context()

        # Qwen 또는 폴백 규칙으로 위험도 계산
        qwen_infer_ms = None
        parsed_response = None
        used_fallback = False
        if self.session and self.tokenizer:
            # Qwen 모델 사용
            prompt = self._build_analysis_prompt(expert_results, context_window, hourly_context)
            qwen_started = time.perf_counter()
            qwen_response = self._evaluate_with_qwen(prompt)
            qwen_infer_ms = (time.perf_counter() - qwen_started) * 1000.0
            
            if qwen_response:
                parsed_response = self._parse_qwen_json_response(qwen_response)
                if parsed_response is not None:
                    risk_score = parsed_response["risk_score"]
                else:
                    risk_score = self._extract_risk_score(qwen_response)
            else:
                # Qwen 추론 실패시 폴백
                risk_score = self._evaluate_fallback(expert_results)
                qwen_response = None
                used_fallback = True
        else:
            # Qwen 없으면 규칙 기반 평가
            risk_score = self._evaluate_fallback(expert_results)
            qwen_response = None
            used_fallback = True

        if used_fallback:
            risk_score = self._apply_hourly_fallback_weight(risk_score, hourly_context, expert_results)
        
        # 시간 맥락 적용
        risk_score = self._apply_context_window(risk_score, context_window)
        risk_score = self._apply_feedback_adjustment(risk_score)
        
        # 위험 레벨 분류
        if risk_score >= 0.85:
            level = "critical"
        elif risk_score >= 0.6:
            level = "warning"
        else:
            level = "normal"
        
        # 응답 구성
        result = {
            "emergency": risk_score >= 0.6,
            "risk_level": level,
            "risk_score": round(risk_score, 4),
            "experts": expert_results,
            "context_used": bool(context_window),
            "hourly_context": hourly_context,
            "qwen_infer_ms": round(float(qwen_infer_ms), 2) if qwen_infer_ms is not None else None,
            "slm_mode": "fallback" if used_fallback else "qwen",
        }

        if parsed_response is not None:
            result["is_outlier"] = parsed_response["is_outlier"]
            result["correlated_with_history"] = parsed_response["correlated_with_history"]
            if parsed_response.get("reason"):
                result["qwen_reason"] = parsed_response["reason"]
            result["risk_level"] = parsed_response["risk_level"]
        
        if qwen_response:
            result["qwen_response"] = qwen_response
        
        return result
