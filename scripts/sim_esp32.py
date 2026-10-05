"""ESP32-S3 3노드 + 마이크 부하 시뮬레이터 (기능·부하 확인용, 성능 수치 보고 금지).

- CSI: sensing 컨테이너 UDP 5005로 788B 패킷(`<4sBBHIIhH192f`)을 노드별 100Hz로 보낸다.
  노드마다 독립된 uint32 ms 장치 시계와 seq를 쓰고, 선택적으로 손실·지터를 넣는다.
- 오디오: audio-sensing과 같은 형식(node/ts_ms/data JSON/waveform raw float32)으로
  Redis audio:events에 주기적으로 넣는다. --audio-dir의 16kHz PCM16 WAV를 순환 재생한다.

예) python scripts/sim_esp32.py --seconds 300 --audio-every 15 \
        --audio-dir data/m4_eval_2398/audio/fall_related
- --format npose: N_pose 펌웨어 흉내. CSI2(280B, --hz 50 권장) + 보드 추론 결과 CSR!(32B, 5 Hz).
  평소 점수 0.05~0.3, --fall-at 초부터 3초 동안 --fall-nodes 점수 0.95(window_full·coverage 1.0).
예) python scripts/sim_esp32.py --format npose --hz 50 --nodes 1,2,3 --seconds 60 --fall-at 20 --fall-nodes 1,2
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
CSI2 = struct.Struct("<4sBBHIIhH64ff")    # N_pose 원본 CSI, 280B
CSR = struct.Struct("<4sBBHIIhHfff")      # N_pose 추론 결과, 32B


def build_packet(node_id: int, seq: int, dev_ts: int, t: float, rssi: int) -> bytes:
    k = np.arange(64, dtype=np.float32)
    raw = np.abs(np.sin(k / 6.0 + t * 2.0 + node_id)) + np.random.normal(0, 0.05, 64)
    resp = 0.5 * np.sin(2 * math.pi * 0.25 * t + k / 20.0)
    heart = 0.3 * np.sin(2 * math.pi * 1.2 * t + k / 15.0)
    floats = np.concatenate([np.clip(raw, 0, 1), resp, heart]).astype(np.float32)
    return PACKET.pack(b"CSI!", node_id, 0, 64, seq & 0xFFFFFFFF, dev_ts & 0xFFFFFFFF,
                       rssi, 0, *floats.tolist())


def build_npose_csi(node_id: int, seq: int, t: float, rssi: int, fresh: bool) -> bytes:
    k = np.arange(64, dtype=np.float32)
    raw = np.clip(np.abs(np.sin(k / 6.0 + t * 2.0 + node_id)) + np.random.normal(0, 0.05, 64), 0, None)
    raw[[0, 1, 2, 3, 4, 5, 32, 59, 60, 61, 62, 63]] = 0.0
    peak = float(raw.max()) or 1.0
    ts = int(time.time() * 1000) & 0xFFFFFFFF
    return CSI2.pack(b"CSI2", node_id, 1 if fresh else 0, 64, seq & 0xFFFFFFFF, ts, rssi, 0,
                     *(raw / peak).astype(np.float32).tolist(), peak * 40.0)


def build_npose_result(node_id: int, seq: int, rssi: int, score: float, window_full: bool) -> bytes:
    logit = math.log(score / (1 - score)) if 0 < score < 1 else 0.0
    ts = int(time.time() * 1000) & 0xFFFFFFFF
    return CSR.pack(b"CSR!", node_id, 0, 2 if window_full else 0, seq & 0xFFFFFFFF, ts, rssi,
                    12, score, logit, 1.0 if window_full else 0.5)


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
    ap.add_argument("--format", choices=("legacy", "npose"), default="legacy",
                    help="legacy = 788B CSI!(100Hz) / npose = CSI2 + CSR!(5Hz)")
    ap.add_argument("--fall-at", type=float, default=-1.0, help="npose: 모의 낙상 시작 초(음수=없음)")
    ap.add_argument("--fall-nodes", default="1", help="npose: 낙상 점수를 낼 노드")
    args = ap.parse_args()
    fall_nodes = {int(n) for n in args.fall_nodes.split(",") if n}
    result_every = max(1, round(args.hz / 5))     # npose 추론 5 Hz
    result_seq = {}

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
            if args.format == "npose":
                sock.sendto(build_npose_csi(n, seq[n], elapsed, rssi[n], random.random() > 0.1),
                            (args.host, args.port))
                if seq[n] % result_every == 0:
                    result_seq[n] = result_seq.get(n, 0) + 1
                    falling = n in fall_nodes and 0 <= elapsed - args.fall_at < 3.0
                    score = 0.95 if falling else random.uniform(0.05, 0.3)
                    sock.sendto(build_npose_result(n, result_seq[n], rssi[n], score, elapsed >= 2.0),
                                (args.host, args.port))
            else:
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
