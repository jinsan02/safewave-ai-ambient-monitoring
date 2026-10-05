#!/usr/bin/env python3
"""앱 푸시 종단 시험 — 긴급 음성 주입 → M4 → 규칙 경보(ai:emergency) → API → FCM 발송 → 앱 확인(ack).

1) 등록된 앱 기기 수를 확인한다(/auth/tokens). 0대면 발송 단계는 '대상 없음'으로 끝난다.
2) "도와주세요" 녹음을 audio:events에 넣는다(audio-sensing과 같은 형식, raw_peak 포함).
3) 새 ai:emergency 항목(긴급 음성 규칙 경보)을 기다리고, notify:sent:{msg_id}:{device} 키가 생기는 시각으로
   API 발송 시각을 잰다(키는 기기별 발송 직전에 쓰인다).
4) --ack-wait 초 동안 GET /alerts/{msg_id}로 앱의 확인(acked_by)·음성 확인 결과(voice_ok)를 본다.
결과 JSON: 주입→경보, 경보→발송, 주입→발송(ms), 발송 기기 수, acked_by, voice_ok.

예) python scripts/dev/push_e2e.py --wav data/m4_eval_2398/audio/help_direct/<파일>.wav --out reports/laptop/val/push_e2e.json
"""
import argparse
import json
import math
import time
import urllib.request
import wave

import numpy as np
import redis


def get(api, path):
    with urllib.request.urlopen(api + path, timeout=5) as r:
        return json.loads(r.read())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav", required=True)
    ap.add_argument("--node", type=int, default=1)
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--ack-wait", type=float, default=60.0)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    r = redis.Redis()
    devices = get(a.api, "/auth/tokens").get("devices", [])
    with wave.open(a.wav) as w:
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    peak = float(np.abs(x).max())
    clip = x if peak >= 0.12 else np.clip(x * (0.85 / max(peak, 1e-9)), -1, 1).astype(np.float32)
    last = r.xrevrange("ai:emergency", count=1)
    last_id = last[0][0].decode() if last else "0-0"
    t_inj = int(time.time() * 1000)
    meta = {"sample_rate": 16000, "channels": 1, "duration_ms": int(len(clip) / 16),
            "peak_db": round(20 * math.log10(float(np.abs(clip).max()) + 1e-9), 2), "raw_peak": round(peak, 6)}
    r.xadd("audio:events", {"node": a.node, "ts_ms": t_inj, "data": json.dumps(meta),
                            "waveform": clip.tobytes()}, maxlen=120, approximate=True)
    rep = {"wav": a.wav, "devices_registered": len(devices), "inject_ms": t_inj}
    msg_id, payload = None, None
    while time.time() * 1000 - t_inj < a.timeout * 1000 and msg_id is None:
        for mid, f in r.xrange("ai:emergency", min=f"({last_id}", max="+"):
            d = json.loads(f[b"data"])
            if d.get("slm_mode") == "rule" or d.get("risk_level") == "critical":
                msg_id, payload = mid.decode(), d
                break
        time.sleep(0.05)
    if msg_id is None:
        rep["result"] = "경보 없음(시간 초과)"
    else:
        t_alert = int(msg_id.split("-")[0])
        rep.update(msg_id=msg_id, summary=payload.get("summary"), slm_mode=payload.get("slm_mode"),
                   inject_to_alert_ms=t_alert - t_inj)
        sent = {}
        while time.time() * 1000 - t_alert < a.timeout * 1000 and len(sent) < len(devices):
            for k in r.scan_iter(f"notify:sent:{msg_id}:*"):
                dev = k.decode().rsplit(":", 1)[1]
                sent.setdefault(dev, int(time.time() * 1000))
            time.sleep(0.05)
        rep["devices_notified"] = len(sent)
        if sent:
            first = min(sent.values())
            rep.update(alert_to_send_ms=first - t_alert, inject_to_send_ms=first - t_inj)
        detail = {}
        t_end = time.time() + a.ack_wait
        while time.time() < t_end:
            detail = get(a.api, f"/alerts/{msg_id}")
            if detail.get("acked_by") and detail.get("voice_ok") is not None:
                break
            time.sleep(2)
        rep.update(acked_by=detail.get("acked_by"), voice_ok=detail.get("voice_ok"))
        rep["result"] = "발송" if sent else ("대상 기기 없음" if not devices else "발송 키 없음")
    json.dump(rep, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(rep, ensure_ascii=False))


if __name__ == "__main__":
    main()
