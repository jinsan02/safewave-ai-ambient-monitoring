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
from redis_client import connect_redis

# ── 환경 변수 ────────────────────────────────────────────────
UDP_IP       = "0.0.0.0"
UDP_PORT     = int(os.getenv("UDP_PORT", 5005))
REDIS_HOST   = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT   = int(os.getenv("REDIS_PORT", 6379))
STREAM_NAME  = "csi:raw"
# RPi5 8GB 운영 기본값: 5노드 100Hz에서 약 72초, 3노드에서 약 120초.
# 장기 추세는 agg:minute:*로 보존하므로 raw CSI를 1시간 유지하지 않는다.
STREAM_MAXLEN = int(os.getenv("CSI_STREAM_MAXLEN", "36_000"))
FS           = float(os.getenv("CSI_FS", 100.0))   # 샘플링 주파수


# ── 패킷 파싱 (788B 고정 — 방식B 확정) ─────────────────────────
_PKT_MAGIC   = b"CSI!"
_STRUCT      = struct.Struct("<4sBBHIIhH192f")  # 788B
_STRUCT_SIZE = _STRUCT.size                      # 788


def parse_packet(raw_bytes: bytes):
    """
    788B 고정 패킷: magic"CSI!" + 20B 헤더 + 192 float32 (3블록×64).
    반환: (node_id, ts_ms, raw64, resp64, heart64, seq, rssi)
    magic 불일치 또는 크기 미달 → 전부 None (폐기)
    """
    if len(raw_bytes) < _STRUCT_SIZE or raw_bytes[:4] != _PKT_MAGIC:
        return None, None, None, None, None, None, None
    _, node_id, _rsv, _n, seq, ts_ms, rssi, _rsv2, *floats = _STRUCT.unpack(raw_bytes[:_STRUCT_SIZE])
    arr = np.asarray(floats, dtype=np.float32)
    return node_id, ts_ms, arr[:64], arr[64:128], arr[128:], seq, rssi


# ── 수신 + 적재 루프 ─────────────────────────────────────────
def receive_loop(sock: socket.socket, r: redis.Redis):
    stats = {"rx": 0, "err": 0}
    last_log = time.time()
    last_redis_write_error_log = 0.0
    node_seq_state: dict[int, int] = {}
    node_loss_state: dict[int, dict] = {}

    while True:
        try:
            data, addr = sock.recvfrom(4096)
            node_id, ts_ms, raw64, resp64, heart64, seq, rssi = parse_packet(data)

            if raw64 is None:
                stats["err"] += 1
                continue

            r.xadd(
                STREAM_NAME,
                {
                    "node":       node_id,
                    "ts_ms":      ts_ms,
                    "data_raw":   raw64.tobytes(),
                    "data_resp":  resp64.tobytes(),
                    "data_heart": heart64.tobytes(),
                },
                maxlen=STREAM_MAXLEN,
                approximate=True,
            )
            stats["rx"] += 1

            # 노드 생존 신고 — /nodes/health 에서 온라인 판정에 사용
            if node_id > 0:
                r.set(f"node:{node_id}:last_seen", time.time(), ex=30)

                state = node_loss_state.setdefault(node_id, {"rx": 0, "lost": 0})
                state["rx"] += 1
                if seq is not None:
                    seq = int(seq)
                    prev = node_seq_state.get(node_id)
                    advance = True
                    if prev is not None and seq != prev:
                        step = (seq - prev) % (1 << 32)  # seq_num uint32 (struct I)
                        if step >= (1 << 31):
                            # 역행: 소폭(≤1000)이면 늦게 도착·중복 → 기준 유지, 크게 역행하면 재부팅 → 기준 재설정
                            advance = (1 << 32) - step > 1000
                        elif 1 < step <= 10000:
                            # step > 10000: 100초치 초과 → 재부팅 추정, 카운터 오염 방지
                            state["lost"] += (step - 1)
                    if advance:
                        node_seq_state[node_id] = seq

                denom = state["rx"] + state["lost"]
                loss_rate = (state["lost"] / denom) if denom > 0 else 0.0
                health_map = {
                    "last_seen": time.time(),
                    "last_seq": int(seq) if seq is not None else -1,
                    "rx": state["rx"],
                    "lost": state["lost"],
                    "loss_rate": round(loss_rate, 6),
                }
                health_map["rssi"] = int(rssi)
                pipe = r.pipeline()
                pipe.hset(f"node:{node_id}:health", mapping=health_map)
                pipe.expire(f"node:{node_id}:health", 3600)
                pipe.execute()

        except redis.exceptions.ConnectionError as exc:
            print(f"[sensing] Redis error: {exc}, reconnecting…", flush=True)
            r = connect_redis(REDIS_HOST, REDIS_PORT, "sensing")

        except redis.exceptions.ResponseError as exc:
            # maxmemory/noeviction 등 데이터 경계 위반 시 300 pkt/s 로그 폭주를 막는다.
            stats["err"] += 1
            now = time.time()
            if now - last_redis_write_error_log >= 5:
                print(f"[sensing] Redis write rejected: {exc}", flush=True)
                last_redis_write_error_log = now

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
    r = connect_redis(REDIS_HOST, REDIS_PORT, "sensing")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)  # 1 MB 버퍼
    sock.bind((UDP_IP, UDP_PORT))
    print(f"[sensing] UDP listening on {UDP_IP}:{UDP_PORT}", flush=True)

    receive_loop(sock, r)


if __name__ == "__main__":
    main()
