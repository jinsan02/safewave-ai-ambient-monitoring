import logging
import os
import re
import time
import json
import numpy as np
import onnxruntime as ort

from logic.emergency_score import compute_emergency_score
from logic.risk_policy import (
    RUBRIC_FLOOR_SCORE,
    WARNING_THRESHOLD,
    apply_context_window,
    apply_feedback_adjustment,
    apply_hourly_fallback_weight,
    classify_score,
    has_emergency_keyword,
    normalize_result,
    rubric_level,
)
from utils import safe_float as _safe_float, stream_id_ts_ms as _stream_id_ts_ms

_LOGGER = logging.getLogger("rp5.ai.qwen15b")


class QwenLogic:
    """
    M5: Qwen2.5-1.5B를 사용한 고급 위험도 평가 엔진 (qwen_05b.py에서 분기한 1.5B 전용).

    0.5B 대비 수정점:
    - few-shot를 raw completion이 아닌 multi-turn 대화(user/assistant 턴)로 제공
      → instruct 모델이 마지막 [현재] 턴만 답하게 하여 예시 echo 방지
    - 멀티 eos 정지(im_end 151645 + endoftext 151643)
    - 첫 완결 JSON에서 early-stop → JSON 뒤 잡음 생성 차단

    Qwen2.5-1.5B-Instruct ONNX 모델을 활용하여 M1-M4(낙상, 생체신호, 환경음, 한국어 STT)
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
        # 기본 64: 출력 토큰 분석 p99≈56(cap)에서 truncation 발생 → MAX=p99+여유=64 적용
        self.max_new_tokens = int(os.getenv("QWEN_MAX_NEW_TOKENS", "64"))
        self.max_new_tokens = max(40, min(80, self.max_new_tokens))
        self.hourly_window_ms = int(os.getenv("SLM_HOURLY_WINDOW_MS", "3600000"))
        self.hourly_emergency_scan_limit = int(os.getenv("SLM_HOURLY_EMERGENCY_SCAN_LIMIT", "300"))
        self.hourly_event_sample_limit = int(os.getenv("SLM_HOURLY_EVENT_SAMPLE_LIMIT", "8"))
        self.hourly_event_dedup_ms = int(os.getenv("SLM_HOURLY_EVENT_DEDUP_MS", "90000"))
        self.hourly_cache_ms = int(os.getenv("SLM_HOURLY_CACHE_MS", "60000"))
        self.redis_client = None  # qwen_service.py가 외부에서 주입
        self._hourly_cache_at_ms = 0
        self._hourly_cache_data = None
        self._onnx_file = None
        self._model_dir = None
        self._load_attempted = False
        self.session_with_past = None
        self._is_merged_kv = False  # optimum 2.x single-file merged KV format
        self._stop_ids = None  # 멀티 eos 정지 토큰 집합 (lazy)
        self._last_prompt_tokens = None  # 직전 추론 입력 토큰 수
        self._last_output_tokens = None  # 직전 추론 출력 토큰 수
        self.feedback_topic_key = os.getenv("MQTT_FEEDBACK_REDIS_KEY", "mqtt:feedback:last")

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

            # optimum 2.x: single merged model with past_key_values inputs
            in_names = {inp.name for inp in self.session.get_inputs()}
            if "past_key_values.0.key" in in_names:
                self._is_merged_kv = True
                _LOGGER.info("qwen_merged_kv_detected — using _generate_merged_kv path")
            elif model_dir:
                # optimum 1.x: separate model_with_past.onnx
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
            # 분 단위 vital 추세는 agg:minute:*에서 별도로 읽는다. 여기서는 실제 M5 사건만 본다.
            emergency_entries = self.redis_client.xrevrange(
                "ai:emergency", count=self.hourly_emergency_scan_limit
            )
        except Exception:
            return context

        last_event_ts_by_node: dict[int, int] = {}
        event_summaries = set()
        for msg_id, fields in emergency_entries:
            ts_ms = _stream_id_ts_ms(msg_id)
            if ts_ms < since_ts_ms:
                break

            payload_raw = fields.get(b"data", b"")
            if isinstance(payload_raw, bytes):
                payload_raw = payload_raw.decode("utf-8", errors="ignore")
            try:
                payload = json.loads(payload_raw) if payload_raw else {}
            except Exception:
                continue

            try:
                node_id = int(payload.get("node_id", 0) or 0)
            except (TypeError, ValueError):
                node_id = 0
            newer_ts = last_event_ts_by_node.get(node_id)
            if newer_ts is not None and newer_ts - ts_ms < self.hourly_event_dedup_ms:
                continue
            last_event_ts_by_node[node_id] = ts_ms

            risk_level = str(payload.get("risk_level", "normal"))
            if risk_level == "critical":
                context["critical_count"] += 1
            elif risk_level == "warning":
                context["warning_count"] += 1

            summary = str(payload.get("summary", "")).strip()
            if summary and summary not in event_summaries \
                    and len(context["important_events"]) < self.hourly_event_sample_limit:
                event_summaries.add(summary)
                context["important_events"].append(summary[:96])

        self._hourly_cache_at_ms = now_ts_ms
        self._hourly_cache_data = dict(context)
        return context

    # rp5와 동일 — 분 집계 소스(heart_sum/heart_count/breathing_sum/breathing_count, TTL 60분)
    _MINUTE_AGG_PREFIX = "agg:minute:"

    def _fetch_time_series(self, now_ts_ms=None, window_min=60):
        """rp5 agg:minute(분 집계, TTL 60분) 이력 → [{m,hr,rr}] 시계열.

        운영(rp5) wiring: ai:result는 36초 롤링 버퍼라 추세 소스로 부적합. 1h 추세는 rp5가
        100Hz 인라인으로 누적하는 agg:minute:{epoch분}(heart_sum/heart_count …)에서 읽는다.
        평가 하니스는 명시 time_series를 주입하므로 이 경로 안 탐(이식 무손실).
        Redis 없거나 집계 없으면 None(스냅샷 전용 동작)."""
        if self.redis_client is None:
            return None
        now_ts_ms = int(now_ts_ms or (time.time() * 1000))
        cur_min = now_ts_ms // 60000  # epoch 분 (rp5 minute_key = ts_ms // 60000)

        def _bf(bucket, name):
            v = bucket.get(name.encode()) if name.encode() in bucket else bucket.get(name)
            return _safe_float(v.decode() if isinstance(v, bytes) else v, default=0.0)

        # 60회 순차 HGETALL → pipeline 1왕복 (SLM 호출마다 실행되므로 RTT 절감)
        try:
            pipe = self.redis_client.pipeline()
            for k in range(window_min - 1, -1, -1):   # 오래된(-59) → 최근(0)
                pipe.hgetall(f"{self._MINUTE_AGG_PREFIX}{cur_min - k}")
            buckets = pipe.execute()
        except Exception:
            return None

        rows = []
        for k, bucket in zip(range(window_min - 1, -1, -1), buckets):
            if not bucket:
                continue
            hc = _bf(bucket, "heart_count")
            bc = _bf(bucket, "breathing_count")
            hr = _bf(bucket, "heart_sum") / hc if hc > 0 else 0.0
            rr = _bf(bucket, "breathing_sum") / bc if bc > 0 else 0.0
            if hr > 0 or rr > 0:
                rows.append({"m": -k, "hr": int(round(hr)), "rr": int(round(rr))})
        return rows[-60:] if rows else None

    def _state_line(self, expert_results, context_window=None, hourly_context=None):
        """expert 출력 → '낙상:..,심박:..,호흡:..,환경:..,소견:..' 한 줄 + 소견 문자열."""
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
        elif fall_score >= 0.5:
            findings.append(f"낙상위험({fall_score:.0%})")
        # 위기(≤40/≥130, ≤5/≥35)는 '위기'로, 경고는 '이상'으로 구분 — few-shot 어휘와 정렬(salience).
        # 위기 vital을 경고 vital보다 앞에 배치 — 소형 모델의 primacy 편향상 먼저 나온 항목을
        # 인용하므로, crisis RR이 warn HR 뒤에 묻혀 누락되던 문제를 해소한다.
        _vit = []
        if hr and (hr <= 40 or hr >= 130):
            _vit.append((0, f"심박위기(hr={hr:.0f})"))
        elif hr and (hr < 60 or hr > 100):
            _vit.append((1, f"심박이상(hr={hr:.0f})"))
        if rr and (rr <= 5 or rr >= 35):
            _vit.append((0, f"호흡위기(rr={rr:.0f})"))
        elif rr and (rr < 12 or rr > 25):
            _vit.append((1, f"호흡이상(rr={rr:.0f})"))
        _vit.sort(key=lambda x: x[0])   # 위기(0) → 경고(1)
        findings.extend(f for _, f in _vit)
        if env_label in {"impact", "alarm"}:
            findings.append(f"위험음({env_label})")
        _kw_list = list(speech_ko.get("keywords") or [])
        _ALERT_KWS = frozenset(["살려", "도와", "응급", "위험", "119", "불", "화재"])
        if (transcript and any(kw in transcript for kw in _ALERT_KWS)) or \
                any(kw in _ALERT_KWS for kw in _kw_list):
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

        # M2가 꺼지면 vital은 {} → 0을 그대로 적으면 모델이 심정지(hr=0)로 읽어 critical을 낸다.
        hr_s = f"{hr:.0f}" if hr > 0 else "미측정"
        rr_s = f"{rr:.0f}" if rr > 0 else "미측정"
        line = (f"낙상:{fall_detected}({fall_score:.0%}),심박:{hr_s},호흡:{rr_s},"
                f"환경:{env_label},소견:{findings_str}{ctx_note}")
        return line

    # 시계열 정상 범위(소견·게이트와 정합: HR 55~100, RR 10~22)
    _TS_HR_LO, _TS_HR_HI = 55.0, 100.0
    _TS_RR_LO, _TS_RR_HI = 10.0, 22.0

    def _series_prompt(self, time_series, k=4):
        """≤60행 시계열 → 압축요약 한 줄(추세 시작→최근 + 경고누적, ~30토큰).
        추세의 '시작→최근'이 최근값을 담으므로 분당 raw행은 생략 — 모델이 raw행을
        reason에 복사(echo)해 JSON이 잘리던 문제·토큰 낭비 방지.
        신호가 없으면(경고0 & 추세 안정) 빈 문자열 — 정상·안정 시계열은 미노출."""
        if not time_series or len(time_series) < 3:
            return ""
        rows = list(time_series)
        hrs = [float(r.get("hr", 0) or 0) for r in rows if float(r.get("hr", 0) or 0) > 0]
        rrs = [float(r.get("rr", 0) or 0) for r in rows if float(r.get("rr", 0) or 0) > 0]

        def _trend(vals):
            if len(vals) < 3:
                return None
            q = max(1, len(vals) // 4)
            s0 = sum(vals[:q]) / q
            s1 = sum(vals[-q:]) / q          # 방향 판정용 분위수 평균
            last = vals[-1]                  # 표시 끝값 = 실제 현재값(=스냅샷). 분위수 평균을
            d = s1 - s0                      #   쓰면 스냅샷 위기값과 어긋나 모델이 추세값을 인용함.
            arrow = "상승" if d > 2 else "하강" if d < -2 else "안정"
            return s0, last, sum(vals) / len(vals), arrow

        ht = _trend(hrs)
        rt = _trend(rrs)
        warn = sum(1 for r in rows
                   if (float(r.get("hr", 0) or 0) > 0 and not (self._TS_HR_LO < float(r["hr"]) < self._TS_HR_HI))
                   or (float(r.get("rr", 0) or 0) > 0 and not (self._TS_RR_LO < float(r["rr"]) < self._TS_RR_HI)))

        # 신호 없음(경고0 & HR/RR 추세 모두 안정) → 시계열 라인 생략
        hr_moving = ht and ht[3] != "안정"
        rr_moving = rt and rt[3] != "안정"
        if warn == 0 and not hr_moving and not rr_moving:
            return ""

        parts = []
        if ht:
            parts.append(f"HR {ht[0]:.0f}→{ht[1]:.0f}({ht[3]},μ{ht[2]:.0f})")
        if rt:
            parts.append(f"RR {rt[0]:.0f}→{rt[1]:.0f}({rt[3]})")
        parts.append(f"경고{warn}/{len(rows)}분")
        return f"[1h추세] {' '.join(parts)}"

    # instruct 모델용 multi-turn few-shot (각 예시를 user/assistant 턴으로)
    # C(최적화): 규칙 슬림 — HR/RR 위기 한 줄 병합, 군더더기 제거(토큰 절감, Track B 무손실 검증).
    _SYSTEM = (
        "너는 독거노인 안전 모니터링 AI다. 센서 상태를 보고 위험도를 평가해 JSON 한 줄만 출력한다. "
        "예시를 반복하지 말고 입력으로 새로 판단한다.\n"
        "규칙:\n"
        "- 심박 ≤40/≥130 또는 호흡 ≤5/≥35 → 위기: warning 이상, reason에 수치 명시\n"
        "- 낙상감지 또는 긴급키워드(살려/도와/119) → warning 이상\n"
        "- [1h추세] 지속 경고 누적·악화 추세 → 한 단계 상향\n"
        "- 위기 없고 활력징후 정상 → normal\n"
        '형식: {"risk_score":0~1,"risk_level":"normal|warning|critical","reason":"근거"}'
    )
    _SHOTS = [
        ("낙상:False(3%),심박:72,호흡:15,환경:silence,소견:정상",
         '{"risk_score":0.1,"risk_level":"normal","reason":"정상"}'),
        ("낙상:False(0%),심박:130,호흡:16,환경:silence,소견:심박위기(hr=130)",
         '{"risk_score":0.7,"risk_level":"warning","reason":"심박위기(hr=130)"}'),
        ("낙상:False(0%),심박:72,호흡:4,환경:silence,소견:호흡위기(rr=4)",
         '{"risk_score":0.7,"risk_level":"warning","reason":"호흡위기(rr=4)"}'),
        # [A/B 제거 후보] 4-domain critical (85토큰, 최대) — 제거해 토큰 절감 테스트 중
        # ("낙상:True(91%),심박:33,호흡:5,환경:alarm,소견:낙상감지,심박이상(hr=33),위험음(alarm)",
        #  '{"risk_score":0.95,"risk_level":"critical","reason":"낙상+심박위기(hr=33)+호흡위기(rr=5)+알람"}'),
        ("낙상:False(95%),심박:68,호흡:14,환경:speech,소견:낙상위험(95%),긴급키워드",
         '{"risk_score":0.9,"risk_level":"critical","reason":"낙상위험+긴급키워드"}'),
        # 시계열 예시(악화 추세): [1h추세] 줄을 복사하지 말고 간결한 근거+추세 태그로 요약
        ("낙상:False(0%),심박:108,호흡:16,환경:silence,소견:심박이상(hr=108)\n"
         "[1h추세] HR 75→108(상승,μ90) 경고12/30분",
         '{"risk_score":0.8,"risk_level":"warning","reason":"심박이상(hr=108)+악화추세"}'),
        # 시계열 예시(지속 경고): 추세는 안정이어도 경고가 오래 누적되면 warning
        ("낙상:False(0%),심박:66,호흡:10,환경:silence,소견:정상\n"
         "[1h추세] HR 66→66(안정,μ66) RR 10→10(안정) 경고33/40분",
         '{"risk_score":0.7,"risk_level":"warning","reason":"지속경고 누적(rr=10,40분)"}'),
    ]

    # ── 노트북 프로필(SLM_PROMPT_PROFILE=laptop) ─────────────────────────────
    # 프롬프트가 길어도 되는 장비용. 게이트 발동 규칙을 입력에 넣고, critical 판정표를
    # 명시하고, 등급이 고르게 섞인 예시를 준다. 고정 prefix라 2회차부터 KV 캐시로 재사용된다.
    # 출력은 reason을 먼저 쓰게 해 근거를 정한 뒤 등급을 고르게 한다.
    _SYSTEM_LAPTOP = (
        "너는 독거노인 안전 모니터링 AI다. 센서 규칙 게이트가 먼저 점수를 매긴 뒤 너에게 판단을 맡긴다. "
        "현재 상태를 보고 최종 위험 등급을 정해 JSON 한 줄만 출력한다. 예시를 베끼지 말고 입력으로 새로 판단한다.\n"
        "입력 읽는 법:\n"
        "- 낙상:True = 낙상 확정(여러 번 연속 판정). 낙상:False(xx%) = 단일 판정 확률일 뿐 확정이 아니다\n"
        "- 심박·호흡 '미측정'은 판단에서 뺀다. '이상'은 경고 수준, '위기'는 심박 ≤40/≥130 또는 호흡 ≤5/≥35\n"
        "- 위험음 = 환경 alarm 또는 impact. 긴급키워드 = 소견에 '긴급키워드'가 있을 때만\n"
        "- 게이트규칙 = 센서 규칙 게이트가 발동한 규칙(등급이 아니다). 게이트critical = 규칙 점수가 이미 critical\n"
        "등급 판정표:\n"
        "- critical: ① 위기 생체신호 + (낙상감지·위험음·긴급키워드 중 하나 이상) "
        "② 낙상감지 + (위험음 또는 긴급키워드) ③ 게이트critical. "
        "조건을 만족하면 소견이 더 붙어도 critical이다(신호가 많을수록 더 위험)\n"
        "- warning: critical이 아니면서 낙상감지 단독(이상 수준 활력징후가 붙어도), 위기 생체신호 단독"
        "(심박·호흡 동시 위기 포함), 또는 게이트규칙이 있을 때\n"
        "- normal: 게이트규칙이 없고 낙상감지·위기 생체신호·긴급키워드가 모두 없을 때. "
        "이상 수준 활력징후, 낙상 확률, 위험음은 하나씩만으로는 normal\n"
        "reason에는 등급 근거가 된 소견만 짧게 쓰고 입력에 없는 소견을 지어내지 않는다.\n"
        '형식: {"reason":"근거","risk_level":"normal|warning|critical","risk_score":점수} '
        "(normal 0.1~0.5, warning 0.6~0.8, critical 0.9~1.0)"
    )
    # (심박, 호흡, 낙상확률, 낙상확정, 환경, 환경신뢰도, 키워드, 시계열, 정답 JSON)
    # 평가셋(eval_qwen_accuracy.py) 케이스와 겹치지 않는 값으로 만든다.
    _SHOT_DEFS_LAPTOP = [
        (70, 14, 0.05, False, "silence", 0.9, [], None,
         '{"reason":"정상","risk_level":"normal","risk_score":0.1}'),
        (104, 16, 0.10, False, "silence", 0.9, [], None,
         '{"reason":"심박이상(hr=104) 단독","risk_level":"normal","risk_score":0.3}'),
        (72, 15, 0.93, False, "silence", 0.9, [], None,
         '{"reason":"낙상위험(93%)은 단일 판정, 확정 아님","risk_level":"normal","risk_score":0.4}'),
        (112, 15, 0.05, False, "alarm", 0.9, [], None,
         '{"reason":"심박이상(hr=112)+위험음(alarm), 위기·낙상 없음","risk_level":"normal","risk_score":0.4}'),
        (74, 14, 0.88, True, "silence", 0.9, [], None,
         '{"reason":"낙상감지 단독","risk_level":"warning","risk_score":0.7}'),
        (38, 14, 0.03, False, "silence", 0.9, [], None,
         '{"reason":"심박위기(hr=38) 단독","risk_level":"warning","risk_score":0.7}'),
        (150, 16, 0.03, False, "silence", 0.9, [], None,
         '{"reason":"심박위기(hr=150) 단독","risk_level":"warning","risk_score":0.75}'),
        (30, 4, 0.03, False, "silence", 0.9, [], None,
         '{"reason":"심박위기(hr=30)+호흡위기(rr=4), 다른 영역 없음","risk_level":"warning","risk_score":0.8}'),
        (68, 10, 0.03, False, "silence", 0.9, [], [{"hr": 68, "rr": 10}] * 40,
         '{"reason":"지속경고 누적(rr=10,40분)","risk_level":"warning","risk_score":0.65}'),
        (82, 29, 0.91, True, "noise", 0.5, [], None,
         '{"reason":"낙상감지+호흡이상(rr=29), 위험음·키워드 없음","risk_level":"warning","risk_score":0.7}'),
        (76, 38, 0.05, False, "speech", 0.8, ["도와"], None,
         '{"reason":"호흡위기(rr=38)+긴급키워드","risk_level":"critical","risk_score":0.9}'),
        (27, 16, 0.10, False, "speech", 0.8, ["도와"], None,
         '{"reason":"심박위기(hr=27)+긴급키워드","risk_level":"critical","risk_score":0.9}'),
        (36, 15, 0.82, True, "silence", 0.9, [], None,
         '{"reason":"낙상감지+심박위기(hr=36)","risk_level":"critical","risk_score":0.95}'),
        (74, 15, 0.86, True, "impact", 0.82, [], None,
         '{"reason":"낙상감지+위험음(impact)","risk_level":"critical","risk_score":0.9}'),
        (37, 16, 0.10, False, "alarm", 0.88, [], None,
         '{"reason":"심박위기(hr=37)+위험음(alarm)","risk_level":"critical","risk_score":0.9}'),
        (70, 16, 0.93, True, "speech", 0.8, ["살려"], None,
         '{"reason":"낙상감지+긴급키워드","risk_level":"critical","risk_score":0.9}'),
        (78, 15, 0.84, True, "alarm", 0.86, ["응급"], None,
         '{"reason":"낙상감지+위험음(alarm)+긴급키워드","risk_level":"critical","risk_score":0.95}'),
        (72, 26, 0.90, True, "impact", 0.9, [], None,
         '{"reason":"낙상감지+호흡이상(rr=26)+위험음(impact)","risk_level":"critical","risk_score":0.9}'),
        (132, 16, 0.87, True, "speech", 0.8, ["살려"], None,
         '{"reason":"낙상감지+심박위기(hr=132)+긴급키워드","risk_level":"critical","risk_score":0.95}'),
        (145, 3, 0.05, False, "impact", 0.8, [], None,
         '{"reason":"심박위기(hr=145)+호흡위기(rr=3)+위험음(impact)","risk_level":"critical","risk_score":0.95}'),
        (142, 36, 0.9, True, "alarm", 0.9, ["119"], None,
         '{"reason":"낙상감지+심박위기(hr=142)+호흡위기(rr=36)+위험음(alarm)+긴급키워드","risk_level":"critical","risk_score":1.0}'),
    ]
    # M5 호출 임계(0.6)로 끌어올리는 규칙만 — 키워드+낙상의심 보너스(+0.15)는 normal에서도 붙어 뺀다.
    _GATE_RULE_NAMES = (
        ("fall_consensus_bypass", "낙상확정"),
        ("fall_hazard_bypass", "낙상+위험음"),
        ("vital_bypass", "생체위기"),
        ("temporal_escalation", "시계열악화"),
    )

    @staticmethod
    def _laptop_profile():
        return os.getenv("SLM_PROMPT_PROFILE", "rpi5").strip().lower() == "laptop"

    @staticmethod
    def _gate(expert_results, time_series=None):
        """ai/main.py와 같은 규칙 게이트 점수 — M5 입력(노트북)과 안전 하한에 쓴다."""
        return compute_emergency_score(expert_results or {}, time_series=time_series)

    def _gate_note(self, gate):
        # 게이트 등급(warning)을 그대로 적으면 1.5B가 그 등급을 복사한다(held-out 09-24: 위기+위험음·
        # 키워드 critical 21건을 warning으로). 발동 규칙만 적고, 등급은 critical일 때만 알린다.
        score, bd = gate
        _, level, _ = classify_score(score)
        rules = [name for key, name in self._GATE_RULE_NAMES if bd.get(key)]
        if not rules and score >= WARNING_THRESHOLD:
            rules = ["점수초과"]   # 규칙 없이 가중합만으로 0.6 이상
        note = "게이트규칙:" + (",".join(rules) if rules else "없음")
        return note + (" 게이트critical" if level == "critical" else "")

    @staticmethod
    def _shot_experts(hr, rr, fall, fall_det, env, env_conf, kws):
        return {
            "fall": {"fall_score": fall, "fall_detected": fall_det, "infer_confidence": 0.75},
            "vital": {"heart_rate": hr, "breathing_rate": rr, "infer_confidence": 0.7},
            "env_sound": {"label": env, "env_sound_label": env, "confidence": env_conf,
                          "env_sound_confidence": env_conf, "infer_confidence": 0.8},
            "speech_ko": {"transcript_ko": " ".join(kws), "speech_detected": bool(kws),
                          "stt_confidence": 0.55 if kws else 0.0, "keywords": list(kws),
                          "infer_confidence": 0.55},
        }

    def _laptop_user(self, expert_results, context_window=None, hourly_context=None,
                     time_series=None, gate=None):
        cur = self._state_line(expert_results, context_window, hourly_context)
        cur += " | " + self._gate_note(gate or self._gate(expert_results, time_series))
        series_line = self._series_prompt(time_series) if time_series else ""
        return cur + ("\n" + series_line if series_line else "")

    # ── 노트북 유사 예시(retrieval few-shot) ─────────────────────────────────
    # 고정 예시 21개 뒤에 입력과 가장 비슷한 판정표 라벨 예시 3개를 붙인다. 고정 prefix는 그대로라
    # 캐시가 유지되고, 붙는 예시(약 450토큰)만 매번 새로 읽는다. 09-24 held-out에서 모델만 정확도
    # 74/92 → 88/92. 같이 시험한 판정표 체크리스트 출력(84/92, 대부분 하한이 채움)·등급 logit 보정
    # (다른 세트로 일반화 안 됨)은 뺐다.
    @staticmethod
    def _signature(er):
        er = er or {}
        v = er.get("vital") or {}
        hr = _safe_float(v.get("heart_rate"), 0.0)
        rr = _safe_float(v.get("breathing_rate"), 0.0)

        def cat(x, crit_lo, crit_hi, warn_lo, warn_hi):
            if x <= 0:
                return "none"
            if x <= crit_lo or x >= crit_hi:
                return "crisis"
            return "warn" if (x < warn_lo or x > warn_hi) else "ok"

        f = er.get("fall") or {}
        fs = _safe_float(f.get("fall_score"), 0.0)
        snd = er.get("env_sound") or {}
        label = str(snd.get("env_sound_label") or snd.get("label") or "")
        return {
            "hr": cat(hr, 40, 130, 60, 100),
            "rr": cat(rr, 5, 35, 12, 25),
            "fall": "det" if f.get("fall_detected") else ("prob" if fs >= 0.5 else "none"),
            "hazard": label in ("alarm", "impact"),
            "kw": has_emergency_keyword(er.get("speech_ko")),
        }

    _KNN_POOL = None

    def _knn_pool(self):
        """판정표 라벨 예시 풀(고정 격자 288개). 라벨·근거는 rubric_level로 만든다."""
        if QwenLogic._KNN_POOL is None:
            pool = []
            for hr in (72, 108, 34, 150):
                for rr in (15, 9, 3, 38):
                    for fall, det in ((0.1, False), (0.8, False), (0.9, True)):
                        for env, conf in (("silence", 0.9), ("alarm", 0.9), ("impact", 0.85)):
                            for kws in ([], ["도와"]):
                                er = self._shot_experts(hr, rr, fall, det, env, conf, kws)
                                score, bd = compute_emergency_score(er)
                                level, _ = rubric_level(er, score, bd)
                                # 근거 = 상태 문장의 소견(고정 예시와 같은 모양). warning은 critical이 아닌
                                # 이유를 붙인다 — 없으면 1.5B가 소견이 여럿이면 critical로 올렸다(09-24 held-out).
                                reason = self._state_line(er).split("소견:", 1)[1].replace(", ", "+")
                                if level == "warning":
                                    sig = self._signature(er)
                                    crisis = "crisis" in (sig["hr"], sig["rr"])
                                    if sig["fall"] == "det":
                                        reason += ", 위험음·키워드·위기 없음"
                                    elif crisis:
                                        reason += ", 낙상확정·위험음·키워드 없음"
                                    else:
                                        reason += ", 낙상 확정 아님·위기 없음"
                                ans = json.dumps({"reason": reason, "risk_level": level,
                                                  "risk_score": {"normal": 0.3, "warning": 0.7,
                                                                 "critical": 0.9}[level]},
                                                 ensure_ascii=False, separators=(",", ":"))
                                pool.append((self._signature(er), er, ans))
            QwenLogic._KNN_POOL = pool
        return QwenLogic._KNN_POOL

    def _knn_shots(self, expert_results, k=3):
        sig = self._signature(expert_results)
        ranked = sorted(self._knn_pool(),
                        key=lambda item: -sum(item[0][f] == sig[f] for f in sig))
        # 가장 비슷한 예시를 입력 바로 앞(마지막)에 둔다
        return [(er, ans) for _, er, ans in reversed(ranked[:k])]

    def _build_messages(self, expert_results, context_window=None, hourly_context=None,
                        time_series=None, gate=None):
        """system + few-shot(user/assistant 턴) + 현재 상태(+시계열)(user)로 messages 구성."""
        if self._laptop_profile():
            messages = [{"role": "system", "content": self._SYSTEM_LAPTOP}]
            for *args, ts, answer in self._SHOT_DEFS_LAPTOP:
                shot = self._shot_experts(*args)
                messages.append({"role": "user", "content": self._laptop_user(shot, time_series=ts)})
                messages.append({"role": "assistant", "content": answer})
            for er, answer in self._knn_shots(expert_results):
                messages.append({"role": "user", "content": self._laptop_user(er)})
                messages.append({"role": "assistant", "content": answer})
            messages.append({"role": "user", "content": self._laptop_user(
                expert_results, context_window, hourly_context, time_series, gate)})
            return messages
        cur = self._state_line(expert_results, context_window, hourly_context)
        if time_series:
            series_line = self._series_prompt(time_series)
            if series_line:
                cur = cur + "\n" + series_line
        messages = [{"role": "system", "content": self._SYSTEM}]
        for u, a in self._SHOTS:
            messages.append({"role": "user", "content": u})
            messages.append({"role": "assistant", "content": a})
        messages.append({"role": "user", "content": cur})
        return messages

    def _extract_risk_score(self, response_text):
        """응답에서 위험도 점수 추출"""
        # 첫 번째: "risk_score" 값, 없으면 독립된 0.x/1.0 소수.
        # 다른 숫자(hr=118, 119)의 일부를 점수로 읽지 않도록 앞뒤가 숫자·점이 아닌 것만 받는다.
        match = re.search(r'"?risk_score"?\s*[:=]\s*([01](?:\.\d+)?)', response_text) \
            or re.search(r'(?<![\d.])(0\.\d+|1\.0+)(?![\d.])', response_text)
        if match:
            try:
                score = float(match.group(1))
                return float(np.clip(score, 0.0, 1.0))
            except ValueError:
                pass

        # 두 번째: 텍스트 기반 휴리스틱
        text_lower = response_text.lower()
        if "긴급" in text_lower or "응급" in text_lower or "즉시" in text_lower:
            return 0.85
        elif "경고" in text_lower or "주의" in text_lower or "주의필요" in text_lower:
            return 0.65
        elif "정상" in text_lower or "안전" in text_lower or "이상없" in text_lower:
            return 0.2

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
            score = _safe_float(obj.get("risk_score"), default=-1.0)
            if score < 0.0:
                return None
            score = float(np.clip(score, 0.0, 1.0))
            level = str(obj.get("risk_level", "")).strip().lower()
            if level not in {"normal", "warning", "critical"}:
                level = "critical" if score >= 0.85 else "warning" if score >= 0.6 else "normal"
            return {
                "risk_score": score,
                "risk_level": level,
                "is_outlier": False,
                "correlated_with_history": False,
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

    def _get_kv_config(self):
        """config.json에서 num_layers, num_kv_heads, head_dim 읽기."""
        if self._model_dir:
            cfg_path = os.path.join(self._model_dir, "config.json")
            if os.path.exists(cfg_path):
                try:
                    with open(cfg_path) as f:
                        cfg = json.load(f)
                    num_layers = int(cfg.get("num_hidden_layers", 24))
                    num_heads = int(cfg.get("num_attention_heads", 14))
                    num_kv_heads = int(cfg.get("num_key_value_heads", num_heads))
                    hidden_size = int(cfg.get("hidden_size", 896))
                    head_dim = hidden_size // num_heads
                    return num_layers, num_kv_heads, head_dim
                except Exception:
                    pass
        return 24, 2, 64  # Qwen2-0.5B defaults

    def _generate_merged_kv(self, input_ids, attention_mask):
        """optimum 2.x merged 형식: prefill + decode를 단일 session으로 처리.

        prefill: past_key_values = empty [1, kv_heads, 0, head_dim]
        decode: past_key_values = 직전 present 출력
        """
        num_layers, num_kv_heads, head_dim = self._get_kv_config()
        in_names = {inp.name for inp in self.session.get_inputs()}

        # 빈 past KV (prefill용)
        past_kv = {
            f"past_key_values.{i}.{t}": np.zeros((1, num_kv_heads, 0, head_dim), dtype=np.float32)
            for i in range(num_layers)
            for t in ("key", "value")
        }

        cur_ids = input_ids  # [1, seq_len]
        past_len = 0
        generated = []

        for _ in range(self.max_new_tokens):
            cur_len = cur_ids.shape[1]
            pos_ids = np.arange(past_len, past_len + cur_len, dtype=np.int64).reshape(1, -1)
            cur_attn = np.ones((1, past_len + cur_len), dtype=np.int64)

            feed = {}
            if "input_ids" in in_names:
                feed["input_ids"] = cur_ids
            if "attention_mask" in in_names:
                feed["attention_mask"] = cur_attn
            if "position_ids" in in_names:
                feed["position_ids"] = pos_ids
            feed.update(past_kv)

            outputs = self.session.run(None, feed)
            out_names = [o.name for o in self.session.get_outputs()]
            out_dict = {name: outputs[i] for i, name in enumerate(out_names)}

            next_token = int(np.argmax(out_dict["logits"][0, -1, :]))
            generated.append(next_token)

            # present.i.key/value → past_key_values.i.key/value
            past_kv = {
                k.replace("present.", "past_key_values."): v
                for k, v in out_dict.items() if k != "logits"
            }
            past_len += cur_len
            cur_ids = np.array([[next_token]], dtype=np.int64)

            # 멀티 eos 정지 (im_end 151645 + endoftext 151643 등)
            if next_token in self._get_stop_ids():
                break
            # 첫 완결 JSON에서 early-stop (강제 prefix '{'로 depth 1 시작)
            if self._json_complete(generated):
                break

        return self.tokenizer.decode(generated, skip_special_tokens=True).strip() or None

    def _get_stop_ids(self):
        """tokenizer.eos + generation_config.json의 eos_token_id(리스트 가능) 합집합."""
        if self._stop_ids is not None:
            return self._stop_ids
        ids = set()
        if self.tokenizer is not None and self.tokenizer.eos_token_id is not None:
            ids.add(int(self.tokenizer.eos_token_id))
        if self._model_dir:
            p = os.path.join(self._model_dir, "generation_config.json")
            if os.path.exists(p):
                try:
                    with open(p) as f:
                        gc = json.load(f)
                    e = gc.get("eos_token_id")
                    if isinstance(e, list):
                        ids.update(int(x) for x in e)
                    elif e is not None:
                        ids.add(int(e))
                except Exception:
                    pass
        self._stop_ids = ids or {151645}
        return self._stop_ids

    def _json_complete(self, generated):
        """생성 토큰을 디코드해 강제 '{' 기준 첫 완결 JSON이 닫혔는지 검사."""
        text = self.tokenizer.decode(generated, skip_special_tokens=True)
        depth = 1  # _evaluate_with_qwen이 prompt 끝에 '{'를 강제했으므로
        for ch in text:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return True
        return False

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

    def _evaluate_with_qwen(self, messages):
        if not self.session or not self.tokenizer:
            return None
        try:
            formatted = self.tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True
            )

            # JSON prefix forcing: { 를 입력에 추가해 모델이 JSON으로 시작하도록 강제
            formatted += "{"

            inputs = self.tokenizer(
                formatted, return_tensors="np", truncation=True,
                # 노트북 프로필 프롬프트는 약 2,730토큰 — 1024에서 자르면 현재 상태가 잘린다.
                max_length=int(os.getenv("QWEN_ONNX_MAX_PROMPT",
                                         "4096" if self._laptop_profile() else "1024")),
            )
            input_ids = inputs["input_ids"].astype(np.int64)
            attention_mask = inputs.get("attention_mask")
            if attention_mask is None:
                attention_mask = np.ones_like(input_ids, dtype=np.int64)
            else:
                attention_mask = attention_mask.astype(np.int64)

            # 입력 프롬프트 토큰 수 (강제 '{' 포함)
            self._last_prompt_tokens = int(input_ids.shape[1])

            if self._is_merged_kv:
                raw = self._generate_merged_kv(input_ids, attention_mask)
            elif self.session_with_past is not None:
                raw = self._generate_with_past(input_ids, attention_mask)
            else:
                raw = self._generate_full_seq(input_ids, attention_mask)

            if not raw:
                self._last_output_tokens = 0
                return None
            # 출력 토큰 수 (생성분 재토크나이즈 — 분포/상한 산정용)
            self._last_output_tokens = len(
                self.tokenizer(raw, add_special_tokens=False)["input_ids"]
            )
            # 모델이 { 를 중복 생성했을 경우 정규화
            return "{" + raw.lstrip("{")
        except Exception as e:
            _LOGGER.error("qwen_infer_failed error=%s", e)
            return None

    def _evaluate_fallback(self, expert_results):
        """Qwen 모델이 없을 때 사용할 규칙 기반 평가"""
        fall = expert_results.get("fall", {})
        vital = expert_results.get("vital", {})
        env_sound = expert_results.get("env_sound", {})
        speech_ko = expert_results.get("speech_ko", {})

        risk = 0.0

        if fall.get("fall_detected", False):
            risk = max(risk, 0.9)
        else:
            risk += _safe_float(fall.get("fall_score", 0.0), 0.0) * 0.3

        hr = _safe_float(vital.get("heart_rate", 70.0), 70.0)
        rr = _safe_float(vital.get("breathing_rate", 16.0), 16.0)

        if hr < 50 or hr > 120 or rr < 10 or rr > 30:
            risk = max(risk, 0.75)
        elif hr < 60 or hr > 100 or rr < 12 or rr > 25:
            risk = max(risk, 0.55)

        env_label = env_sound.get("env_sound_label", "unknown")
        if env_label in {"impact", "alarm"}:
            risk = max(risk, 0.7)

        transcript = str(speech_ko.get("transcript_ko", ""))
        keywords = ["살려", "도와", "응급", "위험", "119", "불", "화재"]
        if any(kw in transcript for kw in keywords):
            risk = max(risk, 0.85)

        return float(np.clip(risk, 0.0, 1.0))

    def _apply_context_window(self, risk_score, context_window):
        return apply_context_window(risk_score, context_window)

    def _apply_hourly_fallback_weight(self, risk_score, hourly_context, expert_results):
        """Qwen 폴백 경로에서 1시간 시계열 맥락을 더 강하게 반영한다."""
        return apply_hourly_fallback_weight(risk_score, hourly_context, expert_results)

    def _apply_feedback_adjustment(self, risk_score):
        return apply_feedback_adjustment(
            risk_score, self.redis_client, self.feedback_topic_key
        )

    def evaluate(self, expert_results, context_window=None, time_series=None):
        """
        최종 위험도 평가

        Args:
            expert_results: M1-M4 전문가 모델의 결과
            context_window: 시간 시리즈 맥락 (최근 경고/긴급 카운트 등)
            time_series: 분당 vital 시계열(≤60행) — 주어지면 프롬프트에 압축 반영(Redis 스캔 대체)

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
        # 운영 wiring: 명시 time_series 없고 Redis 연결됐으면 agg:minute(분 집계)에서 자동 생성.
        # (평가 하니스는 time_series를 명시 주입하므로 이 분기 안 탐 — 이식 무손실)
        if time_series is None and self.redis_client is not None:
            time_series = self._fetch_time_series()

        qwen_infer_ms = None
        parsed_response = None
        used_fallback = False
        gate = self._gate(expert_results, time_series)
        if self.session and self.tokenizer:
            messages = self._build_messages(expert_results, context_window, hourly_context, time_series, gate)
            qwen_started = time.perf_counter()
            qwen_response = self._evaluate_with_qwen(messages)
            qwen_infer_ms = (time.perf_counter() - qwen_started) * 1000.0

            if qwen_response:
                parsed_response = self._parse_qwen_json_response(qwen_response)
                if parsed_response is not None:
                    if self._laptop_profile():
                        # 노트북: 등급이 점수 구간과 어긋나면(예: warning인데 0.4) 등급을 믿고 점수를 구간에 맞춘다.
                        # 점수가 권위값이라 0.4면 normal로 떨어지던 문제(09-24 held-out 5건).
                        s, lv = parsed_response["risk_score"], parsed_response["risk_level"]
                        if classify_score(s)[1] != lv:
                            parsed_response["risk_score"] = {"normal": 0.3, "warning": 0.7, "critical": 0.9}[lv]
                    risk_score = parsed_response["risk_score"]
                else:
                    risk_score = self._extract_risk_score(qwen_response)
            else:
                risk_score = self._evaluate_fallback(expert_results)
                qwen_response = None
                used_fallback = True
        else:
            risk_score = self._evaluate_fallback(expert_results)
            qwen_response = None
            used_fallback = True

        if used_fallback:
            risk_score = self._apply_hourly_fallback_weight(risk_score, hourly_context, expert_results)

        risk_score = self._apply_context_window(risk_score, context_window)
        risk_score = self._apply_feedback_adjustment(risk_score)

        risk_score, level, emergency = classify_score(risk_score)

        result = {
            "emergency": emergency,
            "risk_level": level,
            "risk_score": round(risk_score, 4),
            "experts": expert_results,
            "context_used": bool(context_window),
            "hourly_context": hourly_context,
            "qwen_infer_ms": round(float(qwen_infer_ms), 2) if qwen_infer_ms is not None else None,
            "slm_mode": "fallback" if used_fallback else "qwen",
            "prompt_tokens": self._last_prompt_tokens,
            "output_tokens": self._last_output_tokens,
        }

        if parsed_response is not None:
            result["is_outlier"] = parsed_response["is_outlier"]
            result["correlated_with_history"] = parsed_response["correlated_with_history"]
            if parsed_response.get("reason"):
                result["qwen_reason"] = parsed_response["reason"]
            result["risk_level"] = parsed_response["risk_level"]

        if qwen_response:
            result["qwen_response"] = qwen_response

        # env_label이 alarm/impact가 아닌데 reason에 "알람" 포함 시 제거 (0.5B 할루시네이션 방지)
        _env_so = (expert_results or {}).get("env_sound") or {}
        _env_l = str(_env_so.get("env_sound_label") or _env_so.get("label") or "")
        if _env_l not in {"alarm", "impact"} and result.get("qwen_reason"):
            result["qwen_reason"] = re.sub(r"\+?알람", "", result["qwen_reason"]).strip("+").strip()

        # vital 극한값(HR<=40 or >=130 / RR<=5 or >=35)일 때 Qwen "normal" 다운그레이드 방지
        # emergency_score crit_lo(D1, 40/5)와 동일 경계. vital_bypass 에스컬레이션을 되돌리지 않도록 함
        _vital = (expert_results or {}).get("vital") or {}
        _hr = float(_vital.get("heart_rate", 0) or 0)
        _rr = float(_vital.get("breathing_rate", 0) or 0)
        _vital_crisis = (0 < _hr <= 40) or _hr >= 130 or (0 < _rr <= 5) or _rr >= 35
        if _vital_crisis:
            # ① 점수 하한 교정: 모델 level 문자열과 무관하게 최소 warning을 보장한다.
            current_score = _safe_float(result.get("risk_score"), 0.0)
            if current_score < 0.65:
                result["risk_score"] = 0.65
                result["vital_override"] = True

            # ② reason 교정: vital 수치가 누락됐으면 항상 보정
            _vr_parts = []
            if 0 < _hr <= 40 or _hr >= 130:
                _vr_parts.append(f"심박위기(hr={_hr:.0f})")
            if 0 < _rr <= 5 or _rr >= 35:
                _vr_parts.append(f"호흡위기(rr={_rr:.0f})")
            _cur_reason = result.get("qwen_reason", "").strip()
            if _vr_parts:
                # reason이 "정상"/빈값이면 교체, 수치가 없으면 append
                if _cur_reason in ("정상", "", "normal"):
                    result["qwen_reason"] = "+".join(_vr_parts)
                    result["vital_override"] = True
                elif not any(p.split("(")[1].rstrip(")") in _cur_reason
                             for p in _vr_parts if "(" in p):
                    result["qwen_reason"] = _cur_reason + "+" + "+".join(_vr_parts)
                    result["vital_override"] = True

        # 판정표 하한: 모델이 판정표 등급보다 낮게 내면 판정표 등급으로 올리고, 어느 규칙 때문인지
        # reason에 남긴다(알림 문구 qwen_reason으로 나간다). 모델이 더 높게 낸 경우는 그대로 둔다.
        # 1.5B는 판정표를 일관되게 적용하지 못해 critical을 warning으로 내리는 일이 있었다(09-24 held-out).
        gate_score, gate_bd = gate
        rub_level, rub_reason = rubric_level(expert_results, gate_score, gate_bd)
        floor = RUBRIC_FLOOR_SCORE.get(rub_level)
        if floor and _safe_float(result.get("risk_score"), 0.0) < floor:
            result["risk_score"] = floor
            result["rubric_floor"] = rub_level
            model_reason = str(result.get("qwen_reason") or "").strip()
            note = f"{rub_reason} → 최소 {rub_level}"
            result["qwen_reason"] = note if model_reason in ("", "정상", "normal") \
                else f"{model_reason} / {note}"

        # 모든 보정이 끝난 뒤 점수·단계·응급 플래그를 한 번에 정규화한다.
        # 모델 JSON의 level과 후처리 score가 서로 다른 상태로 ai:emergency에 나가는 것을 막는다.
        return normalize_result(result)
