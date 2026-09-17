"""ESP32-S3 3노드 + 마이크 부하 시뮬레이터 (기능·부하 확인용, 성능 수치 보고 금지).

- CSI: sensing 컨테이너 UDP 5005로 788B 패킷(`<4sBBHIIhH192f`)을 노드별 100Hz로 보낸다.
  노드마다 독립된 uint32 ms 장치 시계와 seq를 쓰고, 선택적으로 손실·지터를 넣는다.
- 오디오: audio-sensing과 같은 형식(node/ts_ms/data JSON/waveform raw float32)으로
  Redis audio:events에 주기적으로 넣는다. --audio-dir의 16kHz PCM16 WAV를 순환 재생한다.

예) python scripts/sim_esp32.py --seconds 300 --audio-every 15 \
        --audio-dir data/m4_eval_2398/audio/fall_related
"""

import argparse
import glob
import json
import math
import random
import socket
import struct
import time
import wave

import numpy as np

PACKET = struct.Struct("<4sBBHIIhH192f")


def build_packet(node_id: int, seq: int, dev_ts: int, t: float, rssi: int) -> bytes:
    k = np.arange(64, dtype=np.float32)
    raw = np.abs(np.sin(k / 6.0 + t * 2.0 + node_id)) + np.random.normal(0, 0.05, 64)
    resp = 0.5 * np.sin(2 * math.pi * 0.25 * t + k / 20.0)
    heart = 0.3 * np.sin(2 * math.pi * 1.2 * t + k / 15.0)
    floats = np.concatenate([np.clip(raw, 0, 1), resp, heart]).astype(np.float32)
    return PACKET.pack(b"CSI!", node_id, 0, 64, seq & 0xFFFFFFFF, dev_ts & 0xFFFFFFFF,
                       rssi, 0, *floats.tolist())


def load_wavs(audio_dir: str | None, limit: int = 50) -> list[np.ndarray]:
    clips = []
    for path in sorted(glob.glob(f"{audio_dir}/*.wav"))[:limit] if audio_dir else []:
        with wave.open(path) as w:
            if w.getframerate() != 16000 or w.getsampwidth() != 2 or w.getnchannels() != 1:
                continue
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        clips.append((pcm.astype(np.float32) / 32768.0)[: 16000 * 6])
    return clips


def push_audio(r, clip: np.ndarray, node_id: int) -> None:
    peak_db = 20 * math.log10(float(np.abs(clip).max()) + 1e-9)
    meta = {"sample_rate": 16000, "channels": 1,
            "duration_ms": int(len(clip) * 1000 / 16000), "peak_db": round(peak_db, 2)}
    r.xadd("audio:events",
           {"node": node_id, "ts_ms": int(time.time() * 1000),
            "data": json.dumps(meta), "waveform": clip.astype(np.float32).tobytes()},
           maxlen=120, approximate=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5005)
    ap.add_argument("--nodes", default="1,2,3")
    ap.add_argument("--hz", type=float, default=100.0)
    ap.add_argument("--seconds", type=float, default=120.0)
    ap.add_argument("--loss", type=float, default=0.01, help="패킷 손실 비율")
    ap.add_argument("--jitter-ms", type=float, default=2.0, help="노드별 송신 지터 상한")
    ap.add_argument("--audio-every", type=float, default=0.0, help="오디오 주입 간격(초), 0=끔")
    ap.add_argument("--audio-dir", default=None)
    ap.add_argument("--audio-node", type=int, default=1)
    ap.add_argument("--redis-port", type=int, default=6379)
    args = ap.parse_args()

    nodes = [int(n) for n in args.nodes.split(",") if n]
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    clock0 = {n: random.randint(0, 2**31) for n in nodes}
    seq = {n: 0 for n in nodes}
    rssi = {n: -30 - 4 * i for i, n in enumerate(nodes)}

    r, clips = None, []
    if args.audio_every > 0:
        import redis
        r = redis.Redis(host=args.host, port=args.redis_port)
        clips = load_wavs(args.audio_dir)
        if not clips:
            rng = np.random.default_rng(0)
            clips = [(0.2 * np.sin(2 * math.pi * 220 * np.arange(32000) / 16000)
                      + rng.normal(0, 0.02, 32000)).astype(np.float32)]
        print(f"[sim] audio clips={len(clips)} every {args.audio_every}s", flush=True)

    period = 1.0 / args.hz
    start = time.perf_counter()
    next_tick = start
    sent = lost = audio_sent = 0
    last_audio = start - args.audio_every if args.audio_every > 0 else float("inf")
    last_report = start
    while True:
        now = time.perf_counter()
        elapsed = now - start
        if elapsed >= args.seconds:
            break
        if now < next_tick:
            time.sleep(next_tick - now)
            continue
        for n in random.sample(nodes, len(nodes)):
            seq[n] += 1
            if random.random() < args.loss:
                lost += 1
                continue
            dev_ts = clock0[n] + int(elapsed * 1000 + random.uniform(0, args.jitter_ms))
            sock.sendto(build_packet(n, seq[n], dev_ts, elapsed, rssi[n]), (args.host, args.port))
            sent += 1
        next_tick += period
        if r is not None and now - last_audio >= args.audio_every:
            push_audio(r, clips[audio_sent % len(clips)], args.audio_node)
            audio_sent += 1
            last_audio = now
        if now - last_report >= 10:
            print(f"[sim] t={elapsed:5.0f}s sent={sent} lost={lost} audio={audio_sent} "
                  f"rate={sent / elapsed:.0f}pkt/s", flush=True)
            last_report = now
    print(f"[sim] done sent={sent} lost={lost} audio={audio_sent}", flush=True)


if __name__ == "__main__":
    main()
