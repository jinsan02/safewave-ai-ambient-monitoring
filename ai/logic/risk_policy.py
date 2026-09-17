"""M5 backend들이 공유하는 위험 점수 후처리 정책.

모델 로딩·프롬프트 생성과 분리된 순수 함수만 둔다. 임계값과 가중치는 기존
qwen_05b/qwen_15b 동작을 그대로 보존한다.
"""

from __future__ import annotations

import json
from typing import Any

from utils import safe_float


WARNING_THRESHOLD = 0.6
CRITICAL_THRESHOLD = 0.85
EMERGENCY_KEYWORDS = ("살려", "도와", "응급", "위험", "119", "불", "화재")


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
    return " / ".join(reasons) if reasons else None


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
