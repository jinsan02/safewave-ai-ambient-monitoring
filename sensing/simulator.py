"""
sensing/simulator.py
실제 ESP32-S3 센서 없이 CSI 패킷을 흉내 내어
sensing 컨테이너(UDP 5005)로 전송하는 테스트 스크립트.

실행:
  python simulator.py [--host 127.0.0.1] [--port 5005] [--rate 10] [--nodes 4]
"""

import argparse
import socket
import struct
import time

import numpy as np


def generate_csi_packet(
    node_id: int,
    n_samples: int = 128,
    scenario: str = "normal",
) -> bytes:
    """
    시나리오별 합성 CSI 시계열 생성 후 패킷으로 직렬화.

    헤더(8 bytes): node_id(u8) + pad(u8) + n_samples(u16) + ts_ms(u32)
    페이로드: n_samples × float32
    """
    t = np.linspace(0.0, n_samples / 100.0, n_samples, dtype=np.float32)
    noise = np.random.normal(0.0, 0.03, n_samples).astype(np.float32)

    if scenario == "fall":
        # 낙상: 고에너지 충격 + 급격한 감쇠
        signal = (
            np.sin(2 * np.pi * 1.5 * t) * np.exp(-t * 2.0)
            + np.random.normal(0.0, 0.3, n_samples)
        ).astype(np.float32)

    elif scenario == "vital":
        # 심박(1.2 Hz) + 호흡(0.3 Hz) 중첩
        signal = (
            0.6 * np.sin(2 * np.pi * 1.2 * t)   # 심박
            + 0.3 * np.sin(2 * np.pi * 0.3 * t)  # 호흡
            + noise
        ).astype(np.float32)

    elif scenario == "activity":
        # 보행: 2 Hz 반복 + 고조파
        signal = (
            np.sin(2 * np.pi * 2.0 * t)
            + 0.4 * np.sin(2 * np.pi * 4.0 * t)
            + noise
        ).astype(np.float32)

    else:  # normal / idle
        signal = (0.1 * np.sin(2 * np.pi * 0.2 * t) + noise).astype(np.float32)

    ts_ms = int(time.time() * 1000) & 0xFFFFFFFF
    header = struct.pack("<BBHI", node_id, 0, n_samples, ts_ms)
    return header + signal.tobytes()


def main():
    parser = argparse.ArgumentParser(description="CSI UDP Simulator")
    parser.add_argument("--host",    default="127.0.0.1")
    parser.add_argument("--port",    type=int, default=5005)
    parser.add_argument("--rate",    type=float, default=10.0, help="packets/sec per node")
    parser.add_argument("--nodes",   type=int, default=4)
    parser.add_argument("--samples", type=int, default=128)
    parser.add_argument("--scenario", default="auto",
                        choices=["normal", "fall", "vital", "activity", "auto"])
    args = parser.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    interval = 1.0 / args.rate
    scenarios = ["normal", "vital", "activity"]
    cycle = 0

    print(f"[simulator] → {args.host}:{args.port}  "
          f"{args.nodes} nodes @ {args.rate} pkt/s  scenario={args.scenario}")

    try:
        while True:
            # 매 50 사이클(5초 @ 10Hz)마다 시나리오 자동 전환
            if args.scenario == "auto":
                if cycle % 200 == 100:
                    current = "fall"
                elif cycle % 50 == 0:
                    current = scenarios[cycle // 50 % len(scenarios)]
                else:
                    current = current if cycle > 0 else "normal"
            else:
                current = args.scenario

            for node_id in range(1, args.nodes + 1):
                pkt = generate_csi_packet(node_id, args.samples, current)
                sock.sendto(pkt, (args.host, args.port))

            cycle += 1
            time.sleep(interval)

    except KeyboardInterrupt:
        print("\n[simulator] stopped.")
    finally:
        sock.close()


if __name__ == "__main__":
    main()
