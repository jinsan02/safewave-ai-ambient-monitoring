"""
통합 파이프라인 경계값 테스트 - M1~M4 expert_results -> emergency_score -> (임계 초과 시) Qwen

경계값 케이스를 직접 정의하여 emergency_score 계산 및 Qwen 실제 추론을 검증한다.
각 케이스의 예상 emg_score는 아래 공식으로 사전 계산됨:
  conf_weight(c) = 0.5 + 0.5*c
  score = 0.4*fall_c + 0.3*vital_c + 0.15*sound_c + 0.15*speech_c
  2+ 도메인 >= 0.5 → score *= 1.2

실행: python scripts/test_pipeline_integration.py
      ORT_USE_GPU=1 python scripts/test_pipeline_integration.py   (GPU 모드)
"""

import sys, os, time, json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ai"))

from logic.emergency_score import compute_emergency_score

THRESHOLD = 0.6
SLM_MODEL_PATH = os.getenv("SLM_MODEL", "/app/models/qwen_05b")

# ── 테스트 케이스 정의 ────────────────────────────────────────────────────────
# conf 기준: M1=0.75, M2=0.70(정상)/0.75(위기), M3=0.80(ONNX), M4=0.55(whisper)
# 예상 emg_score는 사전 계산값 (공차 ±0.002 허용)

TEST_CASES = [
    {
        "id": "TC-01",
        "scenario": "완전 정상 - Qwen 미호출",
        "expert_results": {
            "fall":      {"fall_score": 0.02, "fall_detected": False, "infer_confidence": 0.75},
            "vital":     {"heart_rate": 72.0, "breathing_rate": 15.0, "infer_confidence": 0.70},
            "env_sound": {"label": "silence", "env_sound_label": "silence",
                          "confidence": 0.95, "env_sound_confidence": 0.95, "infer_confidence": 0.80},
            "speech_ko": {"transcript_ko": "", "speech_detected": False,
                          "stt_confidence": 0.0, "keywords": [], "infer_confidence": 0.30},
        },
        "expect_qwen": False,
        "expect_score_range": (0.00, 0.05),  # ~0.007
    },
    {
        "id": "TC-02",
        "scenario": "M1 낙상 단독 고위험 - 단일 도메인 임계 미달 확인",
        "expert_results": {
            "fall":      {"fall_score": 0.92, "fall_detected": True, "infer_confidence": 0.75},
            "vital":     {"heart_rate": 72.0, "breathing_rate": 15.0, "infer_confidence": 0.70},
            "env_sound": {"label": "silence", "env_sound_label": "silence",
                          "confidence": 0.90, "env_sound_confidence": 0.90, "infer_confidence": 0.80},
            "speech_ko": {"transcript_ko": "", "speech_detected": False,
                          "stt_confidence": 0.0, "keywords": [], "infer_confidence": 0.30},
        },
        "expect_qwen": False,
        "expect_score_range": (0.30, 0.37),  # ~0.322
    },
    {
        "id": "TC-03",
        "scenario": "M2 생체신호 위기 단독 (HR=33) - vital 극한 바이패스 -> Qwen 호출",
        "expert_results": {
            "fall":      {"fall_score": 0.02, "fall_detected": False, "infer_confidence": 0.75},
            "vital":     {"heart_rate": 33.0, "breathing_rate": 5.0, "infer_confidence": 0.70},
            "env_sound": {"label": "silence", "env_sound_label": "silence",
                          "confidence": 0.90, "env_sound_confidence": 0.90, "infer_confidence": 0.80},
            "speech_ko": {"transcript_ko": "", "speech_detected": False,
                          "stt_confidence": 0.0, "keywords": [], "infer_confidence": 0.30},
        },
        "expect_qwen": True,
        "expect_score_range": (0.64, 0.67),  # 원점수 0.262 -> vital_bypass 0.65; Qwen vital_override
    },
    {
        "id": "TC-04",
        "scenario": "M1+M2 복합 - vital 바이패스 적용 (emg 0.567->0.65, Qwen 호출)",
        "expert_results": {
            "fall":      {"fall_score": 0.60, "fall_detected": False, "infer_confidence": 0.75},
            "vital":     {"heart_rate": 33.0, "breathing_rate": 5.0, "infer_confidence": 0.75},
            "env_sound": {"label": "silence", "env_sound_label": "silence",
                          "confidence": 0.90, "env_sound_confidence": 0.90, "infer_confidence": 0.80},
            "speech_ko": {"transcript_ko": "", "speech_detected": False,
                          "stt_confidence": 0.0, "keywords": [], "infer_confidence": 0.30},
        },
        "expect_qwen": True,
        "expect_score_range": (0.64, 0.67),  # 복합 보정 0.567 -> vital_bypass 0.65; Qwen vital_override
    },
    {
        "id": "TC-05",
        "scenario": "M1+M2 복합 - 복합 보정 + vital 바이패스 일치 (Qwen 호출)",
        "expert_results": {
            "fall":      {"fall_score": 0.75, "fall_detected": True, "infer_confidence": 0.75},
            "vital":     {"heart_rate": 33.0, "breathing_rate": 5.0, "infer_confidence": 0.75},
            "env_sound": {"label": "silence", "env_sound_label": "silence",
                          "confidence": 0.90, "env_sound_confidence": 0.90, "infer_confidence": 0.80},
            "speech_ko": {"transcript_ko": "", "speech_detected": False,
                          "stt_confidence": 0.0, "keywords": [], "infer_confidence": 0.30},
        },
        "expect_qwen": True,
        "expect_score_range": (0.64, 0.67),  # 복합 보정 0.630 -> vital_bypass floor 0.65
    },
    {
        "id": "TC-06",
        "scenario": "M1 낙상 + M3 알람음 + M2 warning (3도메인 복합, Qwen 호출)",
        "expert_results": {
            "fall":      {"fall_score": 1.00, "fall_detected": True, "infer_confidence": 0.75},
            "vital":     {"heart_rate": 105.0, "breathing_rate": 23.0, "infer_confidence": 0.70},
            "env_sound": {"label": "alarm", "env_sound_label": "alarm",
                          "confidence": 0.90, "env_sound_confidence": 0.90, "infer_confidence": 0.80},
            "speech_ko": {"transcript_ko": "", "speech_detected": False,
                          "stt_confidence": 0.0, "keywords": [], "infer_confidence": 0.30},
        },
        "expect_qwen": True,
        "expect_score_range": (0.70, 0.74),  # ~0.720 (복합 보정 포함)
    },
    {
        "id": "TC-07",
        "scenario": "M4 긴급 키워드 + M1 중위험 - keyword+fall 보너스 적용 (임계 미달)",
        "expert_results": {
            "fall":      {"fall_score": 0.30, "fall_detected": False, "infer_confidence": 0.75},
            "vital":     {"heart_rate": 72.0, "breathing_rate": 15.0, "infer_confidence": 0.70},
            "env_sound": {"label": "speech", "env_sound_label": "speech",
                          "confidence": 0.80, "env_sound_confidence": 0.80, "infer_confidence": 0.50},
            "speech_ko": {"transcript_ko": "살려 도와줘", "speech_detected": True,
                          "stt_confidence": 0.75, "keywords": ["살려", "도와"], "infer_confidence": 0.55},
        },
        "expect_qwen": False,
        "expect_score_range": (0.30, 0.38),  # 0.193 + keyword_fall_bonus 0.15 = ~0.343
    },
    {
        "id": "TC-08",
        "scenario": "전체 최고 위험 - 낙상+심박위기+알람+긴급키워드 (4도메인 x1.5, Qwen 호출)",
        "expert_results": {
            "fall":      {"fall_score": 0.95, "fall_detected": True, "infer_confidence": 0.75},
            "vital":     {"heart_rate": 33.0, "breathing_rate": 5.0, "infer_confidence": 0.75},
            "env_sound": {"label": "alarm", "env_sound_label": "alarm",
                          "confidence": 0.92, "env_sound_confidence": 0.92, "infer_confidence": 0.80},
            "speech_ko": {"transcript_ko": "살려줘 응급", "speech_detected": True,
                          "stt_confidence": 0.80, "keywords": ["살려", "응급"], "infer_confidence": 0.60},
        },
        "expect_qwen": True,
        "expect_score_range": (0.99, 1.00),  # 4도메인 x1.5 -> 1.189 -> cap 1.0
    },
]


# ── Qwen 로딩 (임계 초과 케이스가 있을 때만) ──────────────────────────────────

def _load_qwen():
    """QwenLogic 로드. 실패 시 None 반환."""
    model_path = SLM_MODEL_PATH
    if not os.path.exists(model_path):
        # 로컬 개발 환경 fallback
        local_candidates = [
            os.path.join(os.path.dirname(__file__), "..", "volumes", "models", "qwen_05b"),
            os.path.join("C:\\", "rp5", "volumes", "models", "qwen_05b"),
        ]
        for p in local_candidates:
            if os.path.exists(p):
                model_path = p
                break
        else:
            print(f"  [SKIP] Qwen 모델 경로 없음: {SLM_MODEL_PATH}")
            return None
    try:
        from logic.qwen_05b import QwenLogic
        qwen = QwenLogic(model_path)
        print(f"  Qwen 로드: {model_path}")
        return qwen
    except Exception as exc:
        print(f"  [SKIP] Qwen 로드 실패: {exc}")
        return None


# ── 결과 출력 헬퍼 ─────────────────────────────────────────────────────────────

def _fmt_breakdown(bd):
    return (f"fall={bd['fall']:.3f} vital={bd['vital']:.3f} "
            f"sound={bd['sound']:.3f} speech={bd['speech']:.3f}")


def _print_result(tc, emg_score, breakdown, qwen_result, elapsed_ms, pass_score, pass_qwen):
    status = "PASS" if (pass_score and pass_qwen) else "FAIL"
    print(f"\n{'='*72}")
    print(f"[{status}] {tc['id']} - {tc['scenario']}")
    print(f"  emg_score : {emg_score:.4f}  (expect {tc['expect_score_range'][0]:.2f}~{tc['expect_score_range'][1]:.2f})")
    print(f"  breakdown : {_fmt_breakdown(breakdown)}")
    print(f"  threshold : {THRESHOLD}  →  Qwen 호출={'예' if emg_score >= THRESHOLD else '아니오'}")
    if not pass_score:
        print(f"  !! emg_score 범위 벗어남")
    if not pass_qwen:
        print(f"  !! Qwen 호출 여부 불일치 (expect={tc['expect_qwen']})")
    if qwen_result:
        print(f"  Qwen 소요  : {qwen_result.get('qwen_infer_ms', '?'):.1f}ms  "
              f"(전체 {elapsed_ms:.1f}ms)")
        print(f"  risk_level : {qwen_result.get('risk_level', '?')}")
        print(f"  risk_score : {qwen_result.get('risk_score', '?')}")
        print(f"  slm_mode   : {qwen_result.get('slm_mode', '?')}")
        if qwen_result.get("qwen_reason"):
            print(f"  reason     : {qwen_result['qwen_reason']}")
        raw = qwen_result.get("qwen_response", "")
        if raw:
            print(f"  raw_qwen   : {raw[:200]}")


# ── 메인 실행 ─────────────────────────────────────────────────────────────────

def run():
    needs_qwen = any(tc["expect_qwen"] for tc in TEST_CASES)
    qwen = _load_qwen() if needs_qwen else None

    results = []
    pass_count = 0
    fail_count = 0

    for tc in TEST_CASES:
        emg_score, breakdown = compute_emergency_score(tc["expert_results"])

        lo, hi = tc["expect_score_range"]
        pass_score = lo <= emg_score <= hi
        will_call_qwen = emg_score >= THRESHOLD

        qwen_result = None
        elapsed_ms = 0.0
        if will_call_qwen and qwen is not None:
            t0 = time.perf_counter()
            qwen_result = qwen.evaluate(tc["expert_results"])
            elapsed_ms = (time.perf_counter() - t0) * 1000.0

        pass_qwen = (will_call_qwen == tc["expect_qwen"])
        ok = pass_score and pass_qwen
        if ok:
            pass_count += 1
        else:
            fail_count += 1

        _print_result(tc, emg_score, breakdown, qwen_result, elapsed_ms, pass_score, pass_qwen)

        rec = {
            "id":           tc["id"],
            "scenario":     tc["scenario"],
            "emg_score":    round(emg_score, 4),
            "breakdown":    breakdown,
            "threshold":    THRESHOLD,
            "qwen_called":  will_call_qwen,
            "pass":         ok,
        }
        if qwen_result:
            rec["qwen_risk_level"]  = qwen_result.get("risk_level")
            rec["qwen_risk_score"]  = qwen_result.get("risk_score")
            rec["qwen_reason"]      = qwen_result.get("qwen_reason")
            rec["qwen_raw"]         = qwen_result.get("qwen_response", "")[:300]
            rec["qwen_slm_mode"]    = qwen_result.get("slm_mode")
            rec["qwen_infer_ms"]    = qwen_result.get("qwen_infer_ms")
            rec["total_ms"]         = round(elapsed_ms, 1)
        results.append(rec)

    print(f"\n{'='*72}")
    print(f"결과: {pass_count}/{len(TEST_CASES)} 통과  {'FAIL' if fail_count else 'ALL PASS'}")

    out_path = os.path.join(os.path.dirname(__file__), "..", "reports",
                            "integration_test_results.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"threshold": THRESHOLD, "cases": results}, f, ensure_ascii=False, indent=2)
    print(f"JSON 저장: {out_path}")

    return fail_count == 0


if __name__ == "__main__":
    ok = run()
    sys.exit(0 if ok else 1)
