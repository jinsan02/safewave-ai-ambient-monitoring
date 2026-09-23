#!/usr/bin/env python3
"""csi:raw를 1분 단위 엑셀(.xlsx) 파일로 저장한다 — 실험 기록용(운영 경로 아님).

열은 csi_csv_logger.py와 같다: stream_id, node_id, ts_ms, raw_0..63, resp_0..63, heart_0..63.
분이 바뀌면 모은 행을 백그라운드 스레드에서 xlsx로 쓰고, 수신은 계속한다.
멈추기: Ctrl+C 또는 <dir>/STOP 파일 생성(현재 분까지 저장하고 끝낸다).
사용: python scripts/dev/csi_excel_logger.py --dir data/csi/excel --label empty_room
"""
import argparse
import json
import queue
import struct
import sys
import threading
import time
from pathlib import Path

import redis
import xlsxwriter

_F64 = struct.Struct("<64f")
HEADER = (["stream_id", "node_id", "ts_ms"] + [f"raw_{i}" for i in range(64)]
          + [f"resp_{i}" for i in range(64)] + [f"heart_{i}" for i in range(64)])


def block(b):
    return list(_F64.unpack(b[:256])) if b and len(b) >= 256 else [0.0] * 64


def writer_loop(jobs, log):
    while True:
        job = jobs.get()
        if job is None:
            return
        path, rows, meta = job
        t0 = time.time()
        wb = xlsxwriter.Workbook(str(path), {"constant_memory": True})
        ws = wb.add_worksheet("csi")
        ws.write_row(0, 0, HEADER)
        for i, row in enumerate(rows, start=1):
            ws.write_row(i, 0, row)
        info = wb.add_worksheet("meta")
        for i, (k, v) in enumerate(meta.items()):
            info.write_row(i, 0, [k, json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v])
        wb.close()
        log(f"saved {path.name} rows={len(rows)} per_node={meta['per_node']} write_s={time.time() - t0:.1f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="data/csi/excel")
    ap.add_argument("--label", default="", help="파일명에 붙일 조건 (예: empty_room)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=6379)
    args = ap.parse_args()

    out = Path(args.dir)
    out.mkdir(parents=True, exist_ok=True)
    stop_file = out / "STOP"
    stop_file.unlink(missing_ok=True)
    log_path = out / "logger.log"

    def log(msg):
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
        print(line, flush=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    r = redis.Redis(host=args.host, port=args.port, decode_responses=False)
    r.ping()
    jobs = queue.Queue()
    th = threading.Thread(target=writer_loop, args=(jobs, log), daemon=False)
    th.start()
    suffix = f"_{args.label}" if args.label else ""
    log(f"start dir={out.resolve()} label={args.label or '-'} (stop: Ctrl+C or create {stop_file})")

    def flush(minute, rows, per_node):
        if rows:
            meta = {"minute": minute, "label": args.label, "rows": len(rows), "per_node": per_node,
                    "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")}
            jobs.put((out / f"csi_{minute}{suffix}.xlsx", rows, meta))

    last, minute, rows, per_node = "$", time.strftime("%Y%m%d_%H%M"), [], {}
    try:
        while not stop_file.exists():
            now_minute = time.strftime("%Y%m%d_%H%M")
            if now_minute != minute:
                flush(minute, rows, per_node)
                minute, rows, per_node = now_minute, [], {}
            for _s, msgs in r.xread({"csi:raw": last}, count=1000, block=500) or []:
                for eid, f in msgs:
                    last = eid
                    node = int(f.get(b"node", 0))
                    per_node[node] = per_node.get(node, 0) + 1
                    rows.append([eid.decode(), node, int(f.get(b"ts_ms", 0))]
                                + block(f.get(b"data_raw")) + block(f.get(b"data_resp")) + block(f.get(b"data_heart")))
    except KeyboardInterrupt:
        pass
    finally:
        flush(minute, rows, per_node)
        jobs.put(None)
        th.join()
        log(f"stopped (pending files written). queue_left={jobs.qsize()}")


if __name__ == "__main__":
    sys.exit(main())
