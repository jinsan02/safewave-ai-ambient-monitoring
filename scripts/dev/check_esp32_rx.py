#!/usr/bin/env python3
"""ESP32 CSI 수신 확인 — sensing이 Redis에 남기는 node:N:health를 주기적으로 읽어 노드별 수신 상태를 보여준다.

각 줄: 노드, 구간 수신 Hz(rx 증가량/시간), 구간 손실률(last_seq 증가량 대비 rx 증가량), RSSI, 마지막 수신 뒤 경과 초.
누적 loss_rate 대신 두 시점의 rx·last_seq 증가량을 비교한다(측정 원칙). 시뮬레이터(sim_esp32.py)를 끄고 실행해야
실제 ESP32 수신만 보인다. 사용: python scripts/dev/check_esp32_rx.py [--interval 5] [--seconds 0]
"""
import argparse
import time

import redis


def snapshot(r):
    out = {}
    for key in r.scan_iter("node:*:health"):
        h = r.hgetall(key)
        try:
            out[int(key.split(":")[1])] = {k: float(v) for k, v in h.items()}
        except ValueError:
            continue
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=6379)
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--seconds", type=float, default=0, help="0이면 Ctrl+C까지")
    args = ap.parse_args()

    r = redis.Redis(host=args.host, port=args.port, decode_responses=True)
    r.ping()
    print(f"[check] Redis {args.host}:{args.port} 연결. UDP 5005 → sensing → node:N:health 를 {args.interval}s마다 확인")
    prev, t_prev, t0 = snapshot(r), time.time(), time.time()
    while not args.seconds or time.time() - t0 < args.seconds:
        time.sleep(args.interval)
        cur, now = snapshot(r), time.time()
        dt = now - t_prev
        rows = []
        for node in sorted(cur):
            c, p = cur[node], prev.get(node, {})
            age = now - c.get("last_seen", 0)
            d_rx = c.get("rx", 0) - p.get("rx", 0)
            d_seq = c.get("last_seq", 0) - p.get("last_seq", 0)
            if not p or d_rx <= 0:
                rows.append(f"  node {node}: 새 수신 없음 (마지막 수신 {age:.0f}s 전)")
                continue
            loss = max(0.0, 1 - d_rx / d_seq) if d_seq > 0 else float("nan")
            rows.append(f"  node {node}: {d_rx / dt:6.1f} Hz  손실 {loss:6.2%}  RSSI {c.get('rssi', 0):.0f} dBm"
                        f"  마지막 {age:.1f}s 전")
        print(time.strftime("%H:%M:%S"), "노드 없음 - 수신 기록 없음" if not rows else "")
        for row in rows:
            print(row)
        prev, t_prev = cur, now


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
