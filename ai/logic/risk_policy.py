"""M5 backend들이 공유하는 위험 점수 후처리 정책.

모델 로딩·프롬프트 생성과 분리된 순수 함수만 둔다. 임계값과 가중치는 기존
qwen_05b/qwen_15b 동작을 그대로 보존한다.
"""

from __future__ import annotations

import json
from typing import Any

from logic.emergency_score import _HR_CRIT_HI, _HR_CRIT_LO, _RR_CRIT_HI, _RR_CRIT_LO
from utils import safe_float


WARNING_THRESHOLD = 0.6
CRITICAL_THRESHOLD = 0.85
EMERGENCY_KEYWORDS = ("살려", "도와", "응급", "위험", "119", "불", "화재")
HAZARD_SOUNDS = ("alarm", "impact")
# 판정표 하한을 걸 때 올리는 점수(등급 경계보다 조금 위)
RUBRIC_FLOOR_SCORE = {"warning": 0.65, "critical": 0.9}


def clamp_score(value: Any) -> float:
    return max(0.0, min(1.0, safe_float(value, default=0.0)))


def classify_score(value: Any) -> tuple[float, str, bool]:
    score = clamp_score(value)
    if score >= CRITICAL_THRESHOLD:
        level = "critical"
    elif score >= WARNING_THRESHOLD:
        level = "warning"
    else:
        level = "normal"
    return score, level, score >= WARNING_THRESHOLD


def normalize_result(result: dict) -> dict:
    """risk_score를 권위값으로 score/level/emergency를 일관되게 맞춘다."""
    score, level, emergency = classify_score(result.get("risk_score", 0.0))
    result["risk_score"] = round(score, 4)
    result["risk_level"] = level
    result["emergency"] = emergency
    return result


def rule_alert_reason(breakdown: dict | None, expert_results: dict | None) -> str | None:
    """M5를 거치지 않고 1차 경보를 낼 확정 규칙의 사유. 해당 없으면 None.

    compute_emergency_score가 표시한 확정 규칙만 사용한다. M1 단일 창 양성이나
    가중합·시계열 floor처럼 판단이 필요한 경우는 M5 경로에 남긴다.
    """
    breakdown = breakdown or {}
    experts = expert_results or {}
    reasons = []
    if breakdown.get("fall_consensus_bypass"):
        fall = experts.get("fall") or {}
        reasons.append(
            f"낙상 확정(M1 {fall.get('fall_votes', '?')}/{fall.get('fall_vote_samples', '?')})"
        )
    if breakdown.get("fall_hazard_bypass"):
        label = (experts.get("env_sound") or {}).get("label") \
            or (experts.get("env_sound") or {}).get("env_sound_label") or "위험음"
        reasons.append(f"낙상 의심과 {label} 동시 감지")
    if breakdown.get("vital_bypass"):
        vital = experts.get("vital") or {}
        reasons.append(
            f"생체신호 위기(HR={vital.get('heart_rate', '?')}, RR={vital.get('breathing_rate', '?')})"
        )
    if breakdown.get("voice_emergency_bypass"):
        reasons.append(voice_emergency_text(experts.get("speech_ko")))
    return " / ".join(reasons) if reasons else None


def voice_emergency_text(speech: dict | None) -> str:
    speech = speech or {}
    heard = str(speech.get("transcript_ko", "") or "").strip()
    return (f"긴급 음성 '{heard}'(≈{speech.get('emergency_phrase', '?')}, "
            f"유사도 {safe_float(speech.get('emergency_phrase_sim'), 0.0):.2f})")


def has_emergency_keyword(speech: dict | None) -> bool:
    speech = speech or {}
    transcript = str(speech.get("transcript_ko", "") or "")
    return any(k in transcript for k in EMERGENCY_KEYWORDS) or \
        any(k in EMERGENCY_KEYWORDS for k in (speech.get("keywords") or []))


def rubric_level(expert_results: dict | None, gate_score: float,
                 gate_breakdown: dict | None = None) -> tuple[str, str]:
    """판정표: 게이트가 M5를 부른 경우 최종 등급의 하한과 그 근거 문장.

    critical ① 위기 생체신호 + (낙상 확정·위험음·긴급키워드)
             ② 낙상 확정 + (위험음·긴급키워드)
             ③ 게이트 점수가 이미 critical
             ④ M4 긴급 음성(환각 필터 통과 + 긴급 문장 유사 매칭) — 음성 확인 절차가 오경보를 거른다
    warning  그 밖에 게이트 >= 0.6 (M5 호출 구간)
    normal   게이트 < 0.6 (운영에서는 M5를 부르지 않는 구간)
    평가 정답 v2(scripts/eval_qwen_accuracy.py)와 노트북 프롬프트 판정표가 이 함수와 같다.
    """
    gate = safe_float(gate_score, default=0.0)
    if gate < WARNING_THRESHOLD:
        return "normal", ""
    er = expert_results or {}
    vital = er.get("vital") or {}
    hr = safe_float(vital.get("heart_rate"), default=0.0)
    rr = safe_float(vital.get("breathing_rate"), default=0.0)
    crisis = []
    if 0 < hr <= _HR_CRIT_LO or hr >= _HR_CRIT_HI:
        crisis.append(f"심박위기(hr={hr:.0f})")
    if 0 < rr <= _RR_CRIT_LO or rr >= _RR_CRIT_HI:
        crisis.append(f"호흡위기(rr={rr:.0f})")
    fall = ["낙상감지"] if (er.get("fall") or {}).get("fall_detected") else []
    sound = er.get("env_sound") or {}
    label = str(sound.get("env_sound_label") or sound.get("label") or "")
    hazard = [f"위험음({label})"] if label in HAZARD_SOUNDS else []
    keyword = ["긴급키워드"] if has_emergency_keyword(er.get("speech_ko")) else []

    if gate >= CRITICAL_THRESHOLD:
        return "critical", f"판정표③ 게이트 점수 {gate:.2f}"
    if crisis and (fall or hazard or keyword):
        return "critical", "판정표① " + "+".join(crisis + fall + hazard + keyword)
    if fall and (hazard or keyword):
        return "critical", "판정표② " + "+".join(fall + hazard + keyword)
    if (er.get("speech_ko") or {}).get("emergency_phrase_detected"):
        return "critical", "판정표④ " + voice_emergency_text(er.get("speech_ko"))
    basis = crisis + fall
    if (gate_breakdown or {}).get("temporal_escalation"):
        basis.append("시계열 악화")
    return "warning", "판정표 warning " + ("+".join(basis) if basis else f"게이트 점수 {gate:.2f}")


def apply_context_window(risk_score: Any, context_window: dict | None) -> float:
    score = clamp_score(risk_score)
    if context_window and int(context_window.get("recent_warning_count", 0)) >= 3:
        score += 0.1
    return clamp_score(score)


def apply_hourly_fallback_weight(
    risk_score: Any,
    hourly_context: dict | None,
    expert_results: dict | None,
) -> float:
    """폴백 경로에만 기존 1시간 이력 가중치를 적용한다."""
    weighted = clamp_score(risk_score)
    if not hourly_context:
        return weighted

    if int(hourly_context.get("warning_count", 0)) >= 3:
        weighted *= 1.2
    if int(hourly_context.get("critical_count", 0)) >= 1:
        weighted *= 1.1

    speech = (expert_results or {}).get("speech_ko", {})
    transcript = str(speech.get("transcript_ko", "")).strip()
    if hourly_context.get("speech_samples") and transcript:
        if any(keyword in transcript for keyword in EMERGENCY_KEYWORDS):
            weighted += 0.08
    return clamp_score(weighted)


def apply_feedback_adjustment(
    risk_score: Any,
    redis_client: Any,
    feedback_key: str,
) -> float:
    score = clamp_score(risk_score)
    if redis_client is None:
        return score

    try:
        raw = redis_client.get(feedback_key)
        if not raw:
            return score
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="ignore")
        payload = json.loads(raw)
    except Exception:
        return score

    feedback = str(payload.get("feedback", "")).lower().strip()
    delta = safe_float(payload.get("delta"), default=0.0)
    if delta == 0.0:
        if feedback in {"up", "missed_alert", "positive"}:
            delta = 0.08
        elif feedback in {"down", "false_alarm", "negative"}:
            delta = -0.08
    return clamp_score(score + delta)
