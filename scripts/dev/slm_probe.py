#!/usr/bin/env python3
"""M5(SLM) 직접 검증 — 실제 CSI 결과 + 주입 음성 결과로 운영과 같은 QwenLogic.evaluate()를 호출한다.

파이프라인에서는 M1 규칙 경보 뒤 90초 동안 M5 요청을 막으므로, 빈 방 M1 오경보가 반복되면 M5가 호출되지
않는다. 이 도구는 그와 별개로 M5가 실제 데이터에서 추론·판단하는지 기록한다. ai:emergency에는 쓰지 않는다.
각 회차: (a) 최신 ai:result의 전문가 결과(실제 CSI) 그대로, (b) 거기에 최신 음성 결과(주입 "도와주세요")를 넣은 것.
rp5-ai-qwen 이미지 안에서 실행:
  docker run --rm --network rp5_rp5-network -v C:\\rp5:/repo -v C:\\rp5\\volumes\\models:/app/models \\
    -e REDIS_HOST=db -e QWEN_GGUF_THREADS=8 rp5-ai-qwen python /repo/scripts/dev/slm_probe.py --every 300
멈추기: --out 옆 STOP_SLM 파일.
"""
import argparse
import copy
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.getenv("APP_DIR", "/app"))
import redis  # noqa: E402

from logic.qwen_gguf import QwenLogic  # noqa: E402
from utils import build_context_window  # noqa: E402


def latest_result(r):
    for _sid, f in r.xrevrange("ai:result", count=50):
        d = json.loads(f.get("data", "{}"))
        if d.get("experts"):
            return d
    return None


def latest_voice(r, keyword):
    for _sid, f in r.xrevrange("audio:result", count=100):
        d = json.loads(f.get("data", "{}"))
        s = d.get("speech_ko") or {}
        if keyword in str(s.get("transcript_ko", "")):
            return d
    return None


def run_case(qwen, experts, ctx):
    state = qwen._state_line(experts, ctx)
    t0 = time.perf_counter()
    out = qwen.evaluate(experts, context_window=ctx)
    ms = (time.perf_counter() - t0) * 1000
    return {"state": state, "risk_level": out.get("risk_level"), "risk_score": out.get("risk_score"),
            "reason": out.get("qwen_reason"), "raw": str(out.get("qwen_response") or "")[:300],
            "slm_mode": out.get("slm_mode"), "infer_ms": round(ms, 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--every", type=float, default=300)
    ap.add_argument("--offset", type=float, default=45, help="첫 실행 전 대기(음성 주입 뒤에 돌도록)")
    ap.add_argument("--keyword", default="도와")
    ap.add_argument("--out", default="/repo/reports/laptop/slm_probe.jsonl")
    args = ap.parse_args()

    r = redis.Redis(host=os.getenv("REDIS_HOST", "db"), port=int(os.getenv("REDIS_PORT", "6379")),
                    decode_responses=True)
    models = os.getenv("MODEL_PATH", "/app/models")
    qwen = QwenLogic(os.path.join(models, "qwen_15b_gguf_q5"), tokenizer_dir=os.path.join(models, "qwen_15b"))
    qwen.redis_client = r
    out = Path(args.out)
    stop = out.parent / "STOP_SLM"
    stop.unlink(missing_ok=True)
    time.sleep(args.offset)

    while not stop.exists():
        t0 = time.time()
        rec = {"t": time.strftime("%Y-%m-%d %H:%M:%S")}
        try:
            snap = latest_result(r)
            voice = latest_voice(r, args.keyword)
            ts = int(snap.get("ts_ms", t0 * 1000))
            ctx = build_context_window(r, ts, "ai:emergency", int(os.getenv("CONTEXT_WINDOW_MINUTES", "10")))
            real = copy.deepcopy(snap["experts"])
            rec.update(node_id=snap.get("node_id"), gate_risk=snap.get("risk_score"),
                       m1_score=(real.get("fall") or {}).get("fall_score"),
                       m1_detected=(real.get("fall") or {}).get("fall_detected"))
            rec["a_real_csi"] = run_case(qwen, real, ctx)
            if voice:
                mixed = copy.deepcopy(real)
                mixed["speech_ko"] = voice.get("speech_ko") or {}
                mixed["env_sound"] = voice.get("env_sound") or mixed.get("env_sound", {})
                rec["voice_age_s"] = round(t0 - voice["ts_ms"] / 1000, 1)
                rec["b_real_csi_plus_voice"] = run_case(qwen, mixed, ctx)
            else:
                rec["b_real_csi_plus_voice"] = None
        except Exception as exc:
            rec["error"] = str(exc)
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(json.dumps(rec, ensure_ascii=False), flush=True)
        while time.time() - t0 < args.every and not stop.exists():
            time.sleep(1)


if __name__ == "__main__":
    main()
