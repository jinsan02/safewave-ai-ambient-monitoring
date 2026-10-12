#!/usr/bin/env python3
"""N_pose 입력 기록·재생 — 노트북 실측 입력을 그대로 RPi5에 다시 넣어 같은 입력으로 성능을 비교한다. 실험용(운영 경로 아님).

record: Redis에서 새로 들어오는 csi:raw(CSI2) · m1:score(CSR!) · audio:events(마이크 이벤트)를
        도착 순서대로 jsonl 한 줄씩 저장한다. 시각 기준은 스트림 ID(= 받은 Redis 서버 시계, ms).
        저장 위치: data/capture/<시작시각>_<label>/capture.jsonl (Git 제외). 1초마다 flush.
        멈추기: Ctrl+C 또는 <저장 폴더>/STOP 파일. 끝나면 summary.json.
replay: 기록을 원래 간격대로 다시 보낸다.
        CSI2·CSR! → UDP 패킷으로 다시 조립해 sensing에(보드가 보낸 것과 같은 경로),
        오디오 → audio:events에 XADD(ts_ms는 재생 시각으로 바꿈 — 오디오 결과 나이 판정이 재생 시각 기준이 되게).
        패킷 ts_ms는 원래 값을 둔다(보드 판정은 스트림 ID 시각으로 함).
  복원 한계: CSI2의 n_samples·auto-pin 비트는 Redis에 없어 64·0으로, rssi는 같은 노드 직전 CSR!의 rssi로 채운다.
  주의: 재생하는 동안 실제 보드가 같은 sensing으로 보내면 섞인다 → 보드 전원을 끄거나 공유기에서 떼고 재생한다.

사용:
  기록(노트북):  python scripts/dev/npose_capture.py record --label fall_test1
  재생(RPi5, 컨테이너 안에서 — redis·numpy가 있고 sensing·db 이름으로 닿는다):
    docker cp npose_capture.py rp5-ai-experts:/tmp/ && docker cp capture.jsonl rp5-ai-experts:/tmp/
    docker exec rp5-ai-experts python /tmp/npose_capture.py replay /tmp/capture.jsonl --udp-host sensing --redis-host db
  재생 옵션: --skip-s 30(앞 30초 건너뜀) --duration-s 300 --start-at 21:30:00(그 시각에 시작, 영상과 맞출 때)
"""
import argparse
import base64
import json
import signal
import socket
import struct
import sys
import time
from datetime import datetime
from pathlib import Path

STREAMS = {"csi:raw": "c", "m1:score": "r", "audio:events": "a"}
_CSI2 = struct.Struct("<4sBBHIIhH64ff")   # 280B, N_pose firmware/src/packet.h
_CSR = struct.Struct("<4sBBHIIhHfff")     # 32B


def _sid_ms(sid: str) -> int:
    return int(sid.split("-", 1)[0])


def _s(v) -> str:
    return v.decode() if isinstance(v, bytes) else str(v)


def record(args) -> None:
    import redis

    r = redis.Redis(host=args.host, port=args.port)
    out = Path(args.root) / f"{datetime.now():%Y%m%d_%H%M%S}_{args.label}"
    out.mkdir(parents=True, exist_ok=True)
    stop_file = out / "STOP"
    f = open(out / "capture.jsonl", "w", encoding="utf-8")
    counts = {k: 0 for k in STREAMS.values()}
    first = last = None
    last_ids = {k: "$" for k in STREAMS}
    running = {"on": True}
    signal.signal(signal.SIGINT, lambda *_: running.update(on=False))
    print(f"[capture] 기록 시작 → {out}", flush=True)
    last_flush = last_log = time.time()
    while running["on"] and not stop_file.exists():
        resp = r.xread(last_ids, block=500, count=2000) or []
        for stream, entries in resp:
            name = _s(stream)
            kind = STREAMS[name]
            for sid, fields in entries:
                sid = _s(sid)
                last_ids[name] = sid
                if kind == "c" and b"seq" not in fields:
                    continue  # 옛 788B 형식은 기록하지 않는다
                row = {"k": kind, "id": sid}
                for key, val in fields.items():
                    key = _s(key)
                    if key in ("data_raw", "waveform"):
                        row[key] = base64.b64encode(val).decode()
                    else:
                        row[key] = _s(val)
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                counts[kind] += 1
                ms = _sid_ms(sid)
                first = ms if first is None else min(first, ms)
                last = ms if last is None else max(last, ms)
        now = time.time()
        if now - last_flush >= 1:
            f.flush()
            last_flush = now
        if now - last_log >= 10:
            print(f"[capture] {datetime.now():%H:%M:%S} csi={counts['c']} score={counts['r']} audio={counts['a']}", flush=True)
            last_log = now
    f.close()
    summary = {"label": args.label, "counts": counts, "first_ms": first, "last_ms": last,
               "first_iso": datetime.fromtimestamp(first / 1000).isoformat(timespec="milliseconds") if first else None,
               "last_iso": datetime.fromtimestamp(last / 1000).isoformat(timespec="milliseconds") if last else None,
               "seconds": round((last - first) / 1000, 1) if first else 0}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[capture] 끝 {summary}", flush=True)


def _csi2_packet(row: dict, rssi: int) -> bytes:
    flags = (1 if row.get("fresh") == "1" else 0) | ((int(row.get("sig_mode", 0)) & 3) << 2)
    raw = struct.unpack("<64f", base64.b64decode(row["data_raw"])[:256])
    return _CSI2.pack(b"CSI2", int(row["node"]), flags, 64, int(row["seq"]) & 0xFFFFFFFF,
                      int(row["ts_ms"]) & 0xFFFFFFFF, rssi, 0, *raw, float(row.get("frame_peak", 0)))


def _csr_packet(row: dict) -> bytes:
    return _CSR.pack(b"CSR!", int(row["node"]), int(row["model_ver"]), int(row["flags"]),
                     int(row["seq"]) & 0xFFFFFFFF, int(row["ts_ms"]) & 0xFFFFFFFF, int(row["rssi"]),
                     int(row["infer_ms"]), float(row["score"]), float(row["logit"]), float(row["coverage"]))


def replay(args) -> None:
    r = None
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dest = (args.udp_host, args.udp_port)
    rssi = {}
    counts = {"c": 0, "r": 0, "a": 0}
    t0_rec = t0_wall = None
    end_rec = None
    last_log = time.time()
    with open(args.file, encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                break  # 기록 중 끊긴 마지막 줄
            ms = _sid_ms(row["id"])
            if t0_rec is None:
                t0_rec = ms + int(args.skip_s * 1000)
                end_rec = t0_rec + int(args.duration_s * 1000) if args.duration_s else None
                if args.start_at:
                    hh, mm, ss = (int(x) for x in args.start_at.split(":"))
                    target = datetime.now().replace(hour=hh, minute=mm, second=ss, microsecond=0).timestamp()
                    print(f"[replay] {args.start_at}까지 대기", flush=True)
                    time.sleep(max(0.0, target - time.time()))
                t0_wall = time.time()
                print(f"[replay] 시작 — 기록 시각 {datetime.fromtimestamp(t0_rec / 1000):%H:%M:%S.%f}"[:-3]
                      + f" → {dest}", flush=True)
            if row["k"] == "r":
                rssi[row["node"]] = int(row["rssi"])
            if ms < t0_rec:
                continue
            if end_rec and ms > end_rec:
                break
            wait = t0_wall + (ms - t0_rec) / 1000 / args.speed - time.time()
            if wait > 0:
                time.sleep(wait)
            if row["k"] == "c":
                sock.sendto(_csi2_packet(row, rssi.get(row["node"], 0)), dest)
            elif row["k"] == "r":
                sock.sendto(_csr_packet(row), dest)
            elif not args.no_audio:
                if r is None:
                    import redis
                    r = redis.Redis(host=args.redis_host, port=args.redis_port)
                r.xadd("audio:events", {"node": row["node"], "ts_ms": int(time.time() * 1000),
                                        "data": row["data"], "waveform": base64.b64decode(row["waveform"])},
                       maxlen=120, approximate=True)
            counts[row["k"]] += 1
            if time.time() - last_log >= 10:
                print(f"[replay] 기록 시각 {datetime.fromtimestamp(ms / 1000):%H:%M:%S} "
                      f"csi={counts['c']} score={counts['r']} audio={counts['a']}", flush=True)
                last_log = time.time()
    print(f"[replay] 끝 csi={counts['c']} score={counts['r']} audio={counts['a']}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser("record")
    rec.add_argument("--label", default="session")
    rec.add_argument("--root", default="data/capture")
    rec.add_argument("--host", default="127.0.0.1")
    rec.add_argument("--port", type=int, default=6379)
    rep = sub.add_parser("replay")
    rep.add_argument("file")
    rep.add_argument("--udp-host", default="127.0.0.1")
    rep.add_argument("--udp-port", type=int, default=5005)
    rep.add_argument("--redis-host", default="127.0.0.1")
    rep.add_argument("--redis-port", type=int, default=6379)
    rep.add_argument("--skip-s", type=float, default=0.0)
    rep.add_argument("--duration-s", type=float, default=0.0)
    rep.add_argument("--speed", type=float, default=1.0)
    rep.add_argument("--start-at", default="", help="HH:MM:SS — 이 시각에 재생 시작")
    rep.add_argument("--no-audio", action="store_true")
    args = ap.parse_args()
    record(args) if args.cmd == "record" else replay(args)


if __name__ == "__main__":
    sys.exit(main())
