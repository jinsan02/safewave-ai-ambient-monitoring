#!/usr/bin/env python3
"""Phase 2(TTS 안부 질문 → 음성 응답 → 의도 분류) 종단 확인 — 노트북 실험용.

시나리오마다 ai:emergency에 노드 critical을 쓰고(slm_mode=phase2_test), TTS가 끝날 즈음
응답 음성을 /audio/events로 넣은 뒤 api 로그의 phase2_result 의도를 확인한다.
시나리오 사이에는 Phase 2 락(90초)이 풀릴 때까지 기다린다.
사용: python scripts/dev/phase2_test.py --node 1 --out reports/laptop/phase2_cpu.jsonl
"""
import argparse
import json
import subprocess
import time
import urllib.request
import wave

import numpy as np
import redis

SCENARIOS = [
    ("괜찮아요", "reports/laptop/reply_gwaenchanayo.wav", "cancel_alarm"),
    ("도와주세요", "reports/laptop/voice_260923_202328.wav", "call_emergency"),
    ("무응답", None, "call_emergency"),
]


def wav_body(path, node):
    with wave.open(path, "rb") as w:
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    return json.dumps({"node_id": node, "waveform": [round(float(v), 5) for v in x],
                       "sample_rate": 16000, "trigger_ai": False}).encode()


def tts_spoke_count(path, node):
    """호스트 tts_worker 로그(PowerShell Tee → UTF-16일 수 있음)에서 해당 노드 재생 완료 줄 수."""
    try:
        raw = open(path, "rb").read()
    except OSError:
        return 0
    text = raw.decode("utf-16", errors="ignore") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else \
        raw.decode("utf-8", errors="ignore")
    return sum(1 for line in text.splitlines() if "spoke" in line and line.rstrip().endswith(f"node={node}"))


def api_events(since_s, names):
    out = subprocess.run(["docker", "logs", "--since", str(int(since_s)), "rp5-api"], capture_output=True,
                         text=True, encoding="utf-8", errors="replace")
    evs = []
    for line in (out.stdout + out.stderr).splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("event") in names:
            evs.append(d)
    return evs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--node", type=int, default=1)
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    ap.add_argument("--reply-delay", type=float, default=9.0, help="경보 후 응답 주입까지(초): TTS 합성+재생")
    ap.add_argument("--tts-log", default="reports/laptop/tts_worker.log")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    r = redis.Redis(decode_responses=True)
    lock = f"phase2:active:{args.node}"

    for name, path, expect in SCENARIOS:
        while r.exists(lock):
            time.sleep(2)
        t0 = time.time()
        ts = int(t0 * 1000)
        payload = {"ts_ms": ts, "node_id": args.node, "risk_score": 0.9, "risk_level": "critical",
                   "emergency": True, "slm_invoked": False, "slm_mode": "phase2_test",
                   "summary": f"Phase2 시험({name})"}
        r.xadd("ai:emergency", {"data": json.dumps(payload, ensure_ascii=False)}, maxlen=3600, approximate=True)
        injected = None
        if path:
            # TTS 재생이 끝난 뒤 응답해야 한다(API는 재생 완료 이후 녹음만 응답으로 인정).
            # tts_worker 로그에 이 노드의 "spoke" 줄이 새로 생기면 재생 완료로 본다. 없으면 reply_delay 후 주입.
            spoken_before = tts_spoke_count(args.tts_log, args.node)
            while time.time() - t0 < 40 and tts_spoke_count(args.tts_log, args.node) <= spoken_before:
                time.sleep(0.5)
            if time.time() - t0 >= 40:
                time.sleep(max(0.0, args.reply_delay - (time.time() - t0)))
            time.sleep(1.0)
            req = urllib.request.Request(args.api + "/audio/events", data=wav_body(path, args.node),
                                         headers={"Content-Type": "application/json"})
            injected = json.loads(urllib.request.urlopen(req, timeout=15).read()).get("audio_event_id")
        result = None
        while time.time() - t0 < 60 and result is None:
            time.sleep(2)
            evs = api_events(t0 - 1, {"phase2_result"})
            result = next((e for e in evs if e.get("node_id") == args.node), None)
        evs = api_events(t0 - 1, {"tts_queued", "tts_signal_timeout", "fcm_send_failed", "fcm_unavailable",
                                  "phase2_result", "alert_sent"})
        rec = {"scenario": name, "expect": expect, "got": (result or {}).get("intent"),
               "ok": (result or {}).get("intent") == expect, "transcript_len": (result or {}).get("transcript_len"),
               "injected_event": injected, "elapsed_s": round(time.time() - t0, 1),
               "api_events": sorted({e["event"] for e in evs})}
        print(json.dumps(rec, ensure_ascii=False), flush=True)
        with open(args.out, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
