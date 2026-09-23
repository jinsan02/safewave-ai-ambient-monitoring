#!/usr/bin/env python3
"""경계값 확인 — 임계값 바로 아래/같음/바로 위에서 판정이 코드 의도대로 바뀌는지 본다.

ai-experts 이미지 안에서 실행(운영 코드 /app 그대로 사용):
  docker run --rm -v <repo>:/repo -v <models>:/app/models rp5-ai-experts python /repo/scripts/dev/boundary_check.py
대상: 위험도 0.6/0.85, M1 낙상 0.80, M1 K/N 3/5, M3 무음 게이트 0.005(실제 모델), M3 충격 임계 0.6.
"""
import json
import os
import sys
from collections import deque

sys.path.insert(0, os.getenv("APP_DIR", "/app"))
import numpy as np  # noqa: E402

rows = []


def check(name, value, got, expect):
    rows.append({"item": name, "input": value, "got": got, "expect": expect, "ok": got == expect})


# 1) 위험도 등급 (risk_policy.classify_score, >= 비교)
from logic.risk_policy import classify_score  # noqa: E402
for v, exp in [(0.5999, "normal"), (0.6, "warning"), (0.8499, "warning"), (0.85, "critical")]:
    check("risk_level", v, classify_score(v)[1], exp)

# 2) M1 낙상 임계 0.80 (가짜 세션으로 점수만 주입, 판정은 운영 코드)
from experts import m1_wifi_pose  # noqa: E402


class _Sess:
    def __init__(self, s):
        self.s = s

    def get_inputs(self):
        return [type("I", (), {"name": "csi_data"})()]

    def run(self, _o, _f):
        return [np.array([[self.s]], dtype=np.float32)]


m1 = m1_wifi_pose.WifiPoseModel("/nonexistent")
for v, exp in [(0.7999, False), (0.80, True), (0.8001, True)]:
    m1.session = _Sess(v)
    check("m1_fall_detected", v, bool(m1._infer_onnx(np.zeros((1, 5, 64, 100), np.float32))["fall_detected"]), exp)

# 3) M1 5회 중 3회 투표
from runtime_inputs import aggregate_m1_result  # noqa: E402
for pattern, exp in [([1, 1, 0, 0, 0], False), ([1, 1, 1, 0, 0], True), ([1, 1, 1, 1], False)]:
    votes = deque()
    out = {}
    for p in pattern:
        out = aggregate_m1_result({"fall_detected": bool(p), "fall_score": 0.9 if p else 0.1}, votes)
    check("m1_votes_k3_n5", "".join(map(str, pattern)), bool(out["fall_detected"]), exp)

# 4) M3 무음 게이트 0.005 — 실제 v34 모델, raw_peak 메타로 판정
from experts import m3_ast_base  # noqa: E402
m3 = m3_ast_base.EnvSoundAnalysisModel(os.path.join(os.getenv("MODEL_PATH", "/app/models"), "ast_onnx"))
rng = np.random.default_rng(0)
noise = rng.standard_normal(48000).astype(np.float32)
noise /= np.abs(noise).max()
for peak, exp in [(0.0049, True), (0.005, False), (0.0051, False)]:
    out = m3.infer({"waveform": noise * peak, "sample_rate": 16000, "raw_peak": peak})
    check("m3_silence_gate", peak, bool(out["silence_gated"]), exp)
rows.append({"item": "m3_model", "input": m3.model_name, "got": m3.backend, "expect": "onnx",
             "ok": m3.backend == "onnx"})

# 5) M3 충격 임계 0.6 (probs만 바꿔 _result 판정 확인)
for p, exp in [(0.5999, False), (0.6, True), (0.6001, True)]:
    probs = np.array([0.0, 1 - p, p, 0.0, 0.0, 0.0], dtype=np.float32)
    out = m3._result("impact" if p >= 0.5 else "speech", float(p), "onnx", probs=probs, raw_peak=0.2)
    check("m3_impact_alert", p, bool(out["impact_alert"]), exp)
# 게이트에 걸리면 충격 확률이 높아도 알림 없음
out = m3._result("silence", 1.0, "onnx", probs=np.array([0, 0, 0.99, 0, 0, 0.01], np.float32), raw_peak=0.001, gated=True)
check("m3_gated_blocks_alert", "impact 0.99 + gated", bool(out["impact_alert"]), False)

bad = [r for r in rows if not r["ok"]]
for r in rows:
    print(("OK  " if r["ok"] else "FAIL"), r["item"], r["input"], "->", r["got"], "(expect", r["expect"], ")")
print(json.dumps({"total": len(rows), "failed": len(bad)}))
sys.exit(1 if bad else 0)
