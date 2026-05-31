"""
M1-M4 전문가 출력 기반 응급지수 계산.

SLM(M5) 호출 여부를 결정하는 경량 알고리즘.
SLM은 임계값(threshold) 초과 시에만 호출된다.
"""

import numpy as np

# M2 생체신호 정상 범위
_HR_WARN_LO, _HR_WARN_HI = 55.0, 100.0    # BPM
_HR_CRIT_LO, _HR_CRIT_HI = 35.0, 130.0
_RR_WARN_LO, _RR_WARN_HI = 10.0, 22.0    # 분당 호흡수
_RR_CRIT_LO, _RR_CRIT_HI =  4.0,  35.0

# M3 환경음 → 위험 가중치
_SOUND_WEIGHTS = {
    "alarm":   0.90,
    "impact":  0.65,
    "noise":   0.20,
    "speech":  0.10,
    "music":   0.05,
    "silence": 0.00,
    "unknown": 0.10,
}

# M4 응급 키워드 목록
_CRITICAL_KEYWORDS = frozenset(["살려", "도와", "아파", "응급", "위험", "넘어", "불", "화재", "119"])

# 도메인 가중치 (합 = 1.0)
_DOMAIN_WEIGHTS = {"fall": 0.40, "vital": 0.30, "sound": 0.15, "speech": 0.15}


def _vital_component(val: float, crit_lo: float, warn_lo: float, warn_hi: float, crit_hi: float) -> float:
    """단일 생체신호 값의 이상 점수를 반환한다."""
    if val <= 0.0:
        return 0.0
    if val <= crit_lo or val >= crit_hi:
        return 1.0
    if val <= warn_lo or val >= warn_hi:
        return 0.55
    return 0.0


def compute_emergency_score(expert_results: dict) -> tuple[float, dict]:
    """
    M1-M4 출력에서 응급지수(0.0-1.0)를 계산한다.

    도메인 가중치: fall 40% + vital 30% + sound 15% + speech 15%
    2개 이상 도메인이 0.5 이상이면 복합 위험 보정(×1.2, 상한 1.0)을 적용한다.

    Args:
        expert_results: ai/main.py process_experts() 반환값

    Returns:
        (score, breakdown)
          - score: float 0.0~1.0 응급지수
          - breakdown: {"fall", "vital", "sound", "speech"} 도메인별 점수
    """
    fall_out   = expert_results.get("fall")      or {}
    vital_out  = expert_results.get("vital")     or {}
    sound_out  = expert_results.get("env_sound") or {}
    speech_out = expert_results.get("speech_ko") or {}

    # ── M1: 낙상 점수 ────────────────────────────────────────────
    fall_c = float(np.clip(fall_out.get("fall_score", 0.0), 0.0, 1.0))

    # ── M2: 생체신호 이상 점수 ────────────────────────────────────
    hr = float(vital_out.get("heart_rate", 0.0))
    rr = float(vital_out.get("breathing_rate", 0.0))
    vital_c = max(
        _vital_component(hr, _HR_CRIT_LO, _HR_WARN_LO, _HR_WARN_HI, _HR_CRIT_HI),
        _vital_component(rr, _RR_CRIT_LO, _RR_WARN_LO, _RR_WARN_HI, _RR_CRIT_HI),
    )

    # ── M3: 환경음 위험 점수 ──────────────────────────────────────
    label = str(sound_out.get("label") or sound_out.get("env_sound_label") or "unknown")
    conf  = float(sound_out.get("confidence") or sound_out.get("env_sound_confidence") or 0.0)
    sound_c = float(np.clip(_SOUND_WEIGHTS.get(label, 0.10) * conf, 0.0, 1.0))

    # ── M4: 음성 응급 키워드 점수 ────────────────────────────────
    keywords = list(speech_out.get("keywords") or [])
    stt_conf = float(speech_out.get("stt_confidence", 0.0))
    kw_hits  = len(_CRITICAL_KEYWORDS.intersection(keywords))
    if kw_hits > 0:
        # 키워드 1개당 +0.2, STT 신뢰도 낮아도 최소 0.3으로 보정
        speech_c = float(np.clip(0.5 + kw_hits * 0.2, 0.0, 0.9)) * max(stt_conf, 0.3)
    elif speech_out.get("speech_detected"):
        speech_c = 0.15
    else:
        speech_c = 0.0

    breakdown = {
        "fall":   round(fall_c,   4),
        "vital":  round(vital_c,  4),
        "sound":  round(sound_c,  4),
        "speech": round(float(speech_c), 4),
    }

    score = sum(_DOMAIN_WEIGHTS[k] * breakdown[k] for k in _DOMAIN_WEIGHTS)

    # 복합 위험 보정: 2개 이상 도메인이 경계값(0.5) 이상이면 동시 이상 패턴으로 가산
    if sum(1 for v in breakdown.values() if v >= 0.5) >= 2:
        score = min(1.0, score * 1.2)

    return float(np.clip(score, 0.0, 1.0)), breakdown
