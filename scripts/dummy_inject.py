"""
더미 CSI + 오디오 이벤트를 Redis에 주입해 AI 파이프라인 시뮬레이션.
60초간 실행: CSI 10Hz, 오디오 이벤트 0.5Hz
"""
import json
import math
import os
import random
import sys
import time

import numpy as np
import redis

REDIS_HOST = os.getenv("REDIS_HOST", "db")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
CSI_HZ = 10
AUDIO_HZ = 0.5
RUN_SECONDS = int(os.getenv("INJECT_SECONDS", "60"))
NODE_IDS = [1]

SAMPLE_RATE = 16000
AUDIO_BLOCK_SAMPLES = SAMPLE_RATE * 2  # 2초 분량


def make_csi_frame(node_id: int, ts_ms: int, frame_idx: int) -> bytes:
    n = 512
    t = np.linspace(0.0, 8.0 * math.pi, n) + frame_idx * 0.1
    noise = np.random.normal(0, 0.05, n).astype(np.float32)
    wave = (np.sin(t) + noise).astype(np.float32)
    return wave.tobytes()


def make_audio_event(node_id: int, ts_ms: int) -> dict:
    n = AUDIO_BLOCK_SAMPLES
    t = np.linspace(0.0, 2.0 * math.pi * 440 * 2, n)
    noise = np.random.normal(0, 0.02, n).astype(np.float32)
    waveform = (np.sin(t) * 0.3 + noise).astype(np.float32)
    peak_db = float(20 * np.log10(np.abs(waveform).max() + 1e-9))
    return {
        "sample_rate": SAMPLE_RATE,
        "channels": 1,
        "duration_ms": int(n * 1000 / SAMPLE_RATE),
        "peak_db": round(peak_db, 2),
        "waveform": waveform.tolist(),
        "ts_ms": ts_ms,
    }


def main():
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=False)
    try:
        r.ping()
    except redis.exceptions.ConnectionError as e:
        print(f"Redis 연결 실패: {e}", flush=True)
        sys.exit(1)

    print(f"[inject] Redis 연결 OK ({REDIS_HOST}:{REDIS_PORT})", flush=True)
    print(f"[inject] {RUN_SECONDS}초간 CSI {CSI_HZ}Hz + 오디오 {AUDIO_HZ}Hz 주입 시작", flush=True)

    csi_interval = 1.0 / CSI_HZ
    audio_interval = 1.0 / AUDIO_HZ

    start = time.time()
    last_audio = start - audio_interval  # 즉시 첫 오디오 주입
    frame_idx = 0
    csi_count = 0
    audio_count = 0

    while True:
        now = time.time()
        elapsed = now - start
        if elapsed >= RUN_SECONDS:
            break

        ts_ms = int(now * 1000)
        node_id = NODE_IDS[frame_idx % len(NODE_IDS)]

        # CSI 주입
        csi_data = make_csi_frame(node_id, ts_ms, frame_idx)
        r.xadd("csi:raw", {"node": node_id, "ts_ms": ts_ms, "data": csi_data},
               maxlen=36000, approximate=True)
        # 노드 헬스 키 갱신 (API는 초 단위 last_seen 비교)
        r.set(f"node:{node_id}:last_seen", now, ex=30)
        r.hset(f"node:{node_id}:health", mapping={
            "last_seen": now, "rx": csi_count, "lost": 0, "loss_rate": 0.0,
        })
        r.expire(f"node:{node_id}:health", 3600)
        csi_count += 1
        frame_idx += 1

        # 오디오 주입 (0.5Hz)
        if (now - last_audio) >= audio_interval:
            event = make_audio_event(node_id, ts_ms)
            r.xadd("audio:events",
                   {"node": node_id, "ts_ms": ts_ms,
                    "data": json.dumps(event, ensure_ascii=False).encode()},
                   maxlen=3600, approximate=True)
            audio_count += 1
            last_audio = now
            print(f"[inject] t={elapsed:.1f}s | csi={csi_count} audio={audio_count} "
                  f"node={node_id} peak_db={event['peak_db']:.1f}", flush=True)

        time.sleep(max(0.0, csi_interval - (time.time() - now)))

    ai_result_len = r.xlen("ai:result")
    print(f"\n[inject] 완료: csi={csi_count} audio={audio_count} "
          f"ai:result={ai_result_len}", flush=True)


if __name__ == "__main__":
    main()
