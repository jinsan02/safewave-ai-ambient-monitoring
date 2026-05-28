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
        self.max_new_tokens = int(os.getenv("QWEN_MAX_NEW_TOKENS", "24"))
        self.max_new_tokens = max(16, min(32, self.max_new_tokens))
        self.hourly_window_ms = int(os.getenv("SLM_HOURLY_WINDOW_MS", "3600000"))
        self.hourly_result_scan_limit = int(os.getenv("SLM_HOURLY_RESULT_SCAN_LIMIT", "1800"))
        self.hourly_emergency_scan_limit = int(os.getenv("SLM_HOURLY_EMERGENCY_SCAN_LIMIT", "600"))
        self.hourly_speech_sample_limit = int(os.getenv("SLM_HOURLY_SPEECH_SAMPLE_LIMIT", "8"))
        self.hourly_event_sample_limit = int(os.getenv("SLM_HOURLY_EVENT_SAMPLE_LIMIT", "8"))
        self.hourly_cache_ms = int(os.getenv("SLM_HOURLY_CACHE_MS", "3000"))
        self.redis_client = None
        self._hourly_cache_at_ms = 0
        self._hourly_cache_data = None
        self._onnx_file = None
        self._model_dir = None
        self._load_attempted = False
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
            # ONNX Runtime 세션 생성
            available = ort.get_available_providers()
            providers = []
            if "CUDAExecutionProvider" in available:
                providers.append("CUDAExecutionProvider")
            providers.append("CPUExecutionProvider")
            session_opts = ort.SessionOptions()
            session_opts.intra_op_num_threads = 4
            session_opts.inter_op_num_threads = 2
            
            self.session = ort.InferenceSession(
                onnx_path,
                providers=providers,
                sess_options=session_opts
            )
            _LOGGER.info("qwen_model_loaded path=%s", onnx_path)

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
        """
        M1-M4 결과를 Qwen이 이해하는 분석 프롬프트로 변환
        """
        fall = expert_results.get("fall", {})
        vital = expert_results.get("vital", {})
        env_sound = expert_results.get("env_sound", {})
        speech_ko = expert_results.get("speech_ko", {})
        transcript = speech_ko.get("transcript_ko", "")

        findings = []
        if fall.get("fall_detected", False):
            findings.append("낙상 감지됨")
        hr = float(vital.get("heart_rate", 0.0) or 0.0)
        rr = float(vital.get("breathing_rate", 0.0) or 0.0)
        if hr and (hr < 60 or hr > 100):
            findings.append(f"심박 이상 가능(hr={hr:.0f})")
        if rr and (rr < 12 or rr > 25):
            findings.append(f"호흡 이상 가능(rr={rr:.0f})")
        if env_sound.get("env_sound_label") in {"impact", "alarm"}:
            findings.append(f"고위험 환경음({env_sound.get('env_sound_label')})")
        if transcript and any(kw in transcript for kw in ["살려", "도와", "응급", "위험", "119", "불", "화재"]):
            findings.append("긴급 키워드 음성 감지")

        context_text = ""
        if context_window:
            critical_count = int(context_window.get("recent_critical_count", 0))
            warning_count = int(context_window.get("recent_warning_count", 0))
            context_text = f"최근 맥락: critical={critical_count}, warning={warning_count}"

        hourly_text = ""
        if hourly_context:
            speech_samples = hourly_context.get("speech_samples", [])
            events = hourly_context.get("important_events", [])
            hourly_text = (
                f"지난 {hourly_context.get('window_minutes', 60)}분: "
                f"warning={hourly_context.get('warning_count', 0)}, "
                f"critical={hourly_context.get('critical_count', 0)}, "
                f"{hourly_context.get('heart_rate_trend', '심박 추세 없음')}, "
                f"{hourly_context.get('breathing_rate_trend', '호흡 추세 없음')}, "
                f"음성샘플={speech_samples if speech_samples else '(없음)'}, "
                f"중요이벤트={events if events else '(없음)'}"
            )
        
        # 타임스탐프
        timestamp = ""
        if context_window and context_window.get("current_time"):
            timestamp = f"[{context_window['current_time']}] "
        
        # 프롬프트 구성
        prompt = f"""{timestamp}센서 데이터 분석 요청:

[현재 상황 정보]
- 낙상 감지: {fall.get('fall_detected', False)} (신뢰도: {fall.get('fall_score', 0):.1%})
- 심박수: {vital.get('heart_rate', 0):.0f} bpm (정상: 60-100)
- 호흡수: {vital.get('breathing_rate', 0):.0f} bpm (정상: 12-20)
- 환경음 분석: {env_sound.get('env_sound_label', 'unknown')} (신뢰도: {env_sound.get('env_sound_confidence', 0):.1%})
- 한국어 음성인식: {transcript if transcript else '(없음)'}
- 음성 감지 점수: {speech_ko.get('stt_confidence', 0):.1%}
- 핵심 이상 소견: {', '.join(findings) if findings else '(특이사항 없음)'}
- {context_text if context_text else '최근 맥락 정보 없음'}
- {hourly_text if hourly_text else '최근 1시간 시계열 정보 없음'}

[질문]
현재 신호가 실제 응급 상황인지, 일시적 이상치(outlier)인지 판단해줘.
반드시 JSON만 출력해:
{{
    "risk_score": 0.0,
    "risk_level": "normal|warning|critical",
    "is_outlier": false,
    "correlated_with_history": false,
    "reason": "한 줄 근거"
}}"""
        
        return prompt
    
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
    
    def _evaluate_with_qwen(self, prompt_text):
        """
        Qwen ONNX 모델을 사용한 응답 생성 (간단한 버전)
        
        참고: 실제 LLM 추론은 복잡하므로, 여기서는 규칙 기반 폴백 사용
        """
        if not self.session or not self.tokenizer:
            return None
        
        try:
            # 토크나이징
            inputs = self.tokenizer(
                prompt_text,
                return_tensors="np",
                truncation=True,
                max_length=512
            )

            input_ids = inputs["input_ids"].astype(np.int64)
            attention_mask = inputs.get("attention_mask")
            if attention_mask is None:
                attention_mask = np.ones_like(input_ids, dtype=np.int64)
            else:
                attention_mask = attention_mask.astype(np.int64)

            # 짧은 greedy 디코딩으로 실제 SLLM 응답을 생성
            generated_tokens = []
            max_new_tokens = self.max_new_tokens

            for _ in range(max_new_tokens):
                seq_len = input_ids.shape[1]
                position_ids = np.arange(seq_len, dtype=np.int64).reshape(1, -1)
                if position_ids.shape[0] != input_ids.shape[0]:
                    position_ids = np.repeat(position_ids, input_ids.shape[0], axis=0)

                feed = {}
                for inp in self.session.get_inputs():
                    if inp.name == "input_ids":
                        feed[inp.name] = input_ids
                    elif inp.name == "attention_mask":
                        feed[inp.name] = attention_mask
                    elif inp.name == "position_ids":
                        feed[inp.name] = position_ids

                outputs = self.session.run(None, feed)
                logits = outputs[0]
                next_token_id = int(np.argmax(logits[0, -1, :]))
                generated_tokens.append(next_token_id)

                next_token_arr = np.array([[next_token_id]], dtype=np.int64)
                input_ids = np.concatenate([input_ids, next_token_arr], axis=1)
                attention_mask = np.concatenate(
                    [attention_mask, np.ones((attention_mask.shape[0], 1), dtype=np.int64)],
                    axis=1,
                )

                if self.tokenizer.eos_token_id is not None and next_token_id == int(self.tokenizer.eos_token_id):
                    break

            response = self.tokenizer.decode(generated_tokens, skip_special_tokens=True).strip()
            return response if response else None
            
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
