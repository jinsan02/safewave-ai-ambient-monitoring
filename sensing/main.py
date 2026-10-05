"""
sensing/main.py
ESP32-S3 노드 1~6 으로부터 UDP 패킷을 수신하고
전처리 후 Redis Stream(csi:raw)에 XADD 합니다.

같은 포트에서 세 형식을 받는다.
  CSI! (788 B, 100 Hz)  기존 펌웨어 — raw/resp/heart 3블록
  CSI2 (280 B,  50 Hz)  N_pose 펌웨어 원본 CSI — raw 1블록 + fresh·sig_mode·frame_peak → csi:raw
  CSR! ( 32 B,   5 Hz)  N_pose 보드 추론 결과 — 노드 점수 → m1:score (추론 1회 = 1항목)
N_pose 와이어 계약: composedly13/safewave-ai-ambient-monitoring-N_pose firmware/src/packet.h, hub/packets.py
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
# 보드 추론 결과(CSR!). 5노드 × 5 Hz 기준 약 12분. 수신이 멈추면 1시간 뒤 키가 사라진다.
SCORE_STREAM = "m1:score"
SCORE_STREAM_MAXLEN = int(os.getenv("M1_SCORE_STREAM_MAXLEN", "18000"))
SCORE_STREAM_TTL_SEC = 3600
FS           = float(os.getenv("CSI_FS", 100.0))   # 샘플링 주파수


# ── 패킷 파싱 (788B 고정 — 방식B 확정) ─────────────────────────
_PKT_MAGIC   = b"CSI!"
_STRUCT      = struct.Struct("<4sBBHIIhH192f")  # 788B
_STRUCT_SIZE = _STRUCT.size                      # 788


# N_pose 펌웨어(hub/packets.py와 같은 형식)
_CSI2_MAGIC  = b"CSI2"
_CSI2_STRUCT = struct.Struct("<4sBBHIIhH64ff")    # 280B
_CSR_MAGIC   = b"CSR!"
_CSR_STRUCT  = struct.Struct("<4sBBHIIhHfff")     # 32B


def parse_csi2(raw_bytes: bytes):
    """CSI2 → dict(node_id, flags, seq, ts_ms, rssi, raw64, frame_peak) / 형식이 다르면 None."""
    if len(raw_bytes) != _CSI2_STRUCT.size or raw_bytes[:4] != _CSI2_MAGIC:
        return None
    f = _CSI2_STRUCT.unpack(raw_bytes)
    return {"node_id": f[1], "flags": f[2], "seq": f[4], "ts_ms": f[5], "rssi": f[6],
            "raw64": np.asarray(f[8:72], dtype=np.float32), "frame_peak": f[72]}


def parse_csr(raw_bytes: bytes):
    """CSR! → dict(node_id, model_ver, flags, seq, ts_ms, rssi, infer_ms, score, logit, coverage) / None."""
    if len(raw_bytes) != _CSR_STRUCT.size or raw_bytes[:4] != _CSR_MAGIC:
        return None
    r = _CSR_STRUCT.unpack(raw_bytes)
    return {"node_id": r[1], "model_ver": r[2], "flags": r[3], "seq": r[4], "ts_ms": r[5],
            "rssi": r[6], "infer_ms": r[7], "score": r[8], "logit": r[9], "coverage": r[10]}


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
    stats = {"rx": 0, "err": 0, "score": 0}
    last_log = time.time()
    last_redis_write_error_log = 0.0
    node_seq_state: dict[int, int] = {}
    node_loss_state: dict[int, dict] = {}

    while True:
        try:
            data, addr = sock.recvfrom(4096)
            csr = parse_csr(data)
            if csr is not None:
                # 보드 추론 결과는 CSI 손실 통계(seq)와 섞지 않는다. 생존 신고만 갱신.
                pipe = r.pipeline()
                pipe.xadd(SCORE_STREAM, {
                    "node": csr["node_id"], "seq": csr["seq"], "ts_ms": csr["ts_ms"],
                    "score": f"{csr['score']:.6f}", "logit": f"{csr['logit']:.6f}",
                    "coverage": f"{csr['coverage']:.4f}", "infer_ms": csr["infer_ms"],
                    "model_ver": csr["model_ver"], "flags": csr["flags"], "rssi": csr["rssi"],
                }, maxlen=SCORE_STREAM_MAXLEN, approximate=True)
                pipe.expire(SCORE_STREAM, SCORE_STREAM_TTL_SEC)
                if csr["node_id"] > 0:
                    pipe.set(f"node:{csr['node_id']}:last_seen", time.time(), ex=30)
                pipe.execute()
                stats["score"] += 1
                continue

            csi2 = parse_csi2(data)
            fresh = None
            if csi2 is not None:
                node_id, ts_ms, seq, rssi = csi2["node_id"], csi2["ts_ms"], csi2["seq"], csi2["rssi"]
                fresh = bool(csi2["flags"] & 1)
                r.xadd(
                    STREAM_NAME,
                    {
                        "node":       node_id,
                        "ts_ms":      ts_ms,
                        "data_raw":   csi2["raw64"].tobytes(),
                        "seq":        seq,
                        "fresh":      int(fresh),
                        "sig_mode":   (csi2["flags"] >> 2) & 3,
                        "frame_peak": f"{csi2['frame_peak']:.6f}",
                    },
                    maxlen=STREAM_MAXLEN,
                    approximate=True,
                )
            else:
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

                state = node_loss_state.setdefault(node_id, {"rx": 0, "lost": 0, "fresh": 0})
                state["rx"] += 1
                if fresh:
                    state["fresh"] += 1
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
                if csi2 is not None:
                    # N_pose: 새 CSI 프레임 비율(재전송 제외). GO 기준 ≥ 85% (김태연 0단계 벤치)
                    health_map["fresh_rate"] = round(state["fresh"] / max(state["rx"], 1), 4)
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
            print(f"[sensing] rx={stats['rx']} err={stats['err']} score={stats['score']} "
                  f"({stats['rx'] / 10:.1f} pkt/s)", flush=True)
            stats["rx"] = stats["err"] = stats["score"] = 0
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
