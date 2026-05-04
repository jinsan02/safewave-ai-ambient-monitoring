"""
sensing/main.py
ESP32-S3 노드 1~6 으로부터 UDP 패킷을 수신하고
전처리 후 Redis Stream(csi:raw)에 XADD 합니다.
"""

import os
import socket
import struct
import time

import numpy as np
import redis

from filters import preprocess_csi

# ── 환경 변수 ────────────────────────────────────────────────
UDP_IP       = "0.0.0.0"
UDP_PORT     = int(os.getenv("UDP_PORT", 5005))
REDIS_HOST   = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT   = int(os.getenv("REDIS_PORT", 6379))
STREAM_NAME  = "csi:raw"
STREAM_MAXLEN = 36_000          # ~1시간 @ 10 Hz, approximate trim
FS           = float(os.getenv("CSI_FS", 100.0))   # 샘플링 주파수


# ── Redis 연결 (재시도) ──────────────────────────────────────
def connect_redis() -> redis.Redis:
    while True:
        try:
            r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, socket_connect_timeout=3)
            r.ping()
            print(f"[sensing] Redis connected: {REDIS_HOST}:{REDIS_PORT}", flush=True)
            return r
        except redis.exceptions.ConnectionError as exc:
            print(f"[sensing] Redis not ready ({exc}), retry in 2s…", flush=True)
            time.sleep(2)


# ── 패킷 파싱 ────────────────────────────────────────────────
def parse_packet(raw_bytes: bytes):
    """
    헤더(8 bytes): node_id(uint8) + reserved(1) + n_samples(uint16) + timestamp_ms(uint32)
    페이로드: n_samples × float32
    """
    if len(raw_bytes) < 8:
        return None, None, None

    node_id   = raw_bytes[0]
    n_samples = struct.unpack_from("<H", raw_bytes, 2)[0]
    ts_ms     = struct.unpack_from("<I", raw_bytes, 4)[0]
    payload   = raw_bytes[8:]

    expected = n_samples * 4
    if len(payload) < expected:
        return None, None, None

    samples = np.frombuffer(payload[:expected], dtype=np.float32).copy()
    return node_id, ts_ms, samples


# ── 수신 + 적재 루프 ─────────────────────────────────────────
def receive_loop(sock: socket.socket, r: redis.Redis):
    stats = {"rx": 0, "err": 0}
    last_log = time.time()

    while True:
        try:
            data, addr = sock.recvfrom(4096)
            node_id, ts_ms, samples = parse_packet(data)

            if samples is None:
                # 헤더 없는 레거시 패킷: 전체를 float32 배열로 처리
                samples = np.frombuffer(data, dtype=np.float32).copy()
                node_id = 0
                ts_ms   = int(time.time() * 1000) & 0xFFFFFFFF

            processed = preprocess_csi(samples, fs=FS)

            r.xadd(
                STREAM_NAME,
                {
                    "node":  node_id,
                    "ts_ms": ts_ms,
                    "data":  processed.tobytes(),
                },
                maxlen=STREAM_MAXLEN,
                approximate=True,
            )
            stats["rx"] += 1

            # 노드 생존 신고 — /nodes/health 에서 온라인 판정에 사용
            if node_id > 0:
                r.set(f"node:{node_id}:last_seen", time.time(), ex=30)

        except redis.exceptions.ConnectionError as exc:
            print(f"[sensing] Redis error: {exc}, reconnecting…", flush=True)
            r = connect_redis()

        except Exception as exc:
            stats["err"] += 1
            print(f"[sensing] packet error: {exc}", flush=True)

        # 10초마다 수신 통계 출력
        now = time.time()
        if now - last_log >= 10:
            print(f"[sensing] rx={stats['rx']} err={stats['err']} "
                  f"({stats['rx'] / 10:.1f} pkt/s)", flush=True)
            stats["rx"] = stats["err"] = 0
            last_log = now


def main():
    r = connect_redis()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)  # 1 MB 버퍼
    sock.bind((UDP_IP, UDP_PORT))
    print(f"[sensing] UDP listening on {UDP_IP}:{UDP_PORT}", flush=True)

    receive_loop(sock, r)


if __name__ == "__main__":
    main()
