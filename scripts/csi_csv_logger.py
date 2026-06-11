"""
csi:raw Redis Stream을 읽어 1분 단위 CSV로 저장.
sensing 컨테이너가 실행 중인 상태에서 동작 (포트 충돌 없음).

사용:
  python scripts/csi_csv_logger.py                  # data/csi/ 디렉토리에 저장
  python scripts/csi_csv_logger.py -d ./my_data     # 저장 디렉토리 지정
  python scripts/csi_csv_logger.py --host 127.0.0.1 # Redis 호스트 (기본값)

파일명: csi_YYYYMMDD_HHMM.csv (1분마다 새 파일)
Ctrl-C로 중단.
"""

import argparse
import csv
import os
import signal
import struct
import time
from datetime import datetime
from pathlib import Path

import redis

_FLOAT_STRUCT = struct.Struct("<64f")  # 64 × float32 = 256B

_HEADER = (
    ["stream_id", "node_id", "ts_ms"]
    + [f"raw_{i}" for i in range(64)]
    + [f"resp_{i}" for i in range(64)]
    + [f"heart_{i}" for i in range(64)]
)


def _decode_block(b: bytes) -> list:
    if len(b) < 256:
        return [0.0] * 64
    return list(_FLOAT_STRUCT.unpack(b[:256]))


def current_minute_label() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M")


def open_csv(out_dir: Path, label: str):
    path = out_dir / f"csi_{label}.csv"
    is_new = not path.exists()
    f = open(path, "a", newline="")
    writer = csv.writer(f)
    if is_new:
        writer.writerow(_HEADER)
    return f, writer, path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-d", "--dir", default="data/csi", help="CSV 저장 디렉토리")
    ap.add_argument("--host", default=os.getenv("REDIS_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.getenv("REDIS_PORT", 6379)))
    args = ap.parse_args()

    out_dir = Path(args.dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    r = redis.Redis(host=args.host, port=args.port, decode_responses=False)
    try:
        r.ping()
    except redis.exceptions.ConnectionError as e:
        print(f"[csi_csv] Redis 연결 실패 ({args.host}:{args.port}): {e}")
        return

    print(f"[csi_csv] Redis {args.host}:{args.port} 연결 OK")
    print(f"[csi_csv] 저장 디렉토리: {out_dir.resolve()}")
    print(f"[csi_csv] csi:raw 스트림 구독 중... (Ctrl-C로 중단)\n")

    stop = False

    def _on_stop(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _on_stop)
    signal.signal(signal.SIGTERM, _on_stop)

    # 현재 시각 이후 항목부터 읽기 (과거 데이터 스킵)
    last_id = "$"
    cur_label = current_minute_label()
    csv_file, writer, cur_path = open_csv(out_dir, cur_label)
    print(f"[csi_csv] 파일 열림: {cur_path.name}")

    rx = 0
    start = time.time()
    last_report = start

    try:
        while not stop:
            # 500ms block — 데이터 없으면 대기
            entries = r.xread({"csi:raw": last_id}, count=200, block=500)
            if not entries:
                continue

            for _stream, messages in entries:
                for entry_id, fields in messages:
                    label = current_minute_label()
                    if label != cur_label:
                        csv_file.close()
                        print(f"[csi_csv] 파일 완료: {cur_path.name} ({rx} rows)")
                        cur_label = label
                        rx = 0
                        csv_file, writer, cur_path = open_csv(out_dir, cur_label)
                        print(f"[csi_csv] 새 파일: {cur_path.name}")

                    node_id = int(fields.get(b"node", 0))
                    ts_ms   = int(fields.get(b"ts_ms", 0))
                    raw64   = _decode_block(fields.get(b"data_raw", b""))
                    resp64  = _decode_block(fields.get(b"data_resp", b""))
                    heart64 = _decode_block(fields.get(b"data_heart", b""))

                    writer.writerow(
                        [entry_id.decode(), node_id, ts_ms]
                        + raw64 + resp64 + heart64
                    )
                    rx += 1
                    last_id = entry_id

            csv_file.flush()

            now = time.time()
            if now - last_report >= 10:
                elapsed = now - start
                print(f"[csi_csv] {elapsed:.0f}s | 이번 파일 {rx} rows | {rx / max(elapsed,1):.1f} row/s")
                last_report = now

    finally:
        csv_file.close()
        elapsed = time.time() - start
        print(f"\n[csi_csv] 종료: 마지막 파일 {cur_path.name}, {elapsed:.1f}초")


if __name__ == "__main__":
    main()
