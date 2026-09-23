#!/usr/bin/env python3
"""일정 간격으로 같은 음성을 API(/audio/events)에 넣고 M3·M4 인식 결과를 기록한다 — 무인 실험용.

기록(jsonl): 주입 시각, 이벤트→결과 지연, M4 전사·키워드, M3 라벨·확률, 주입 뒤 ai:emergency 건수.
멈추기: Ctrl+C 또는 --out 옆 STOP_VOICE 파일 생성.
사용: python scripts/dev/voice_probe.py --wav reports/laptop/voice.wav --every 300 --out reports/laptop/voice_probe.jsonl
"""
import argparse
import json
import time
import urllib.request
import wave
from pathlib import Path

import numpy as np
import redis


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav", required=True, help="16 kHz mono 16-bit WAV")
    ap.add_argument("--every", type=float, default=300)
    ap.add_argument("--node", type=int, default=1)
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    ap.add_argument("--out", required=True)
    ap.add_argument("--expect", default="", help="기대 문장(기록에 함께 남김)")
    args = ap.parse_args()

    with wave.open(args.wav, "rb") as w:
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    body = json.dumps({"node_id": args.node, "waveform": [round(float(v), 5) for v in x],
                       "sample_rate": 16000, "trigger_ai": False}).encode()
    r = redis.Redis(decode_responses=True)
    out = Path(args.out)
    stop = out.parent / "STOP_VOICE"
    stop.unlink(missing_ok=True)

    while not stop.exists():
        t0 = time.time()
        rec = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "expect": args.expect}
        try:
            req = urllib.request.Request(args.api + "/audio/events", data=body,
                                         headers={"Content-Type": "application/json"})
            eid = json.loads(urllib.request.urlopen(req, timeout=15).read())["audio_event_id"]
            ev_ms = int(eid.split("-")[0])
            rec["event_id"] = eid
            for _ in range(120):
                hit = None
                for sid, f in r.xrevrange("audio:result", count=30):
                    d = json.loads(f["data"])
                    if d.get("ts_ms") == ev_ms:
                        hit = (sid, d)
                        break
                if hit:
                    sid, d = hit
                    s, e = d.get("speech_ko") or {}, d.get("env_sound") or {}
                    rec.update(latency_ms=int(sid.split("-")[0]) - ev_ms, transcript=s.get("transcript_ko"),
                               keywords=s.get("keywords"), m3_label=e.get("env_sound_label"),
                               m3_conf=round(float(e.get("env_sound_confidence") or 0), 3),
                               m3_impact=e.get("impact_prob"))
                    break
                time.sleep(0.5)
            else:
                rec["error"] = "no audio:result within 60s"
            time.sleep(20)  # 규칙·M5 반응을 잠깐 기다린다
            emg = [json.loads(f.get("data", "{}")) for _i, f in r.xrange("ai:emergency", min=f"{ev_ms}-0", max="+")]
            rec["emergency_after_20s"] = len(emg)
            # slm_mode=qwen이면 M5가 이 사건을 실제로 추론한 것, rule이면 규칙 경보
            rec["slm_modes"] = [e.get("slm_mode") for e in emg]
            rec["slm_summaries"] = [str(e.get("summary") or "")[:40] for e in emg if e.get("slm_mode") == "qwen"]
        except Exception as exc:
            rec["error"] = str(exc)
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(json.dumps(rec, ensure_ascii=False), flush=True)
        while time.time() - t0 < args.every and not stop.exists():
            time.sleep(1)


if __name__ == "__main__":
    main()
