#!/usr/bin/env python3
"""같은 실측 입력(npose_capture 기록)을 환경별로 재생해 결과·지연·자원을 같은 형식으로 남긴다. 실험용(운영 경로 아님).

한 번 실행 = 한 환경 1회:
  1) 대상 서버의 Phase 2 잠금(phase2:active:*)이 풀릴 때까지 대기(앞 회차 경보가 다음 회차를 막지 않게)
  2) validation_recorder(결과 CSV) + 자원 기록(/system/resources 1초 간격) 시작
  3) npose_capture replay — CSI2·CSR!는 UDP로 sensing에, 오디오 이벤트는 audio:events에(노트북 M3·M4가 받은 입력 그대로)
  4) 재생이 끝나면 --tail-s 동안 더 기록하고 멈춘다
  5) ai-qwen 로그에서 M5 추론 시간(qwen_invoked)을 모은다
  --auto-ack-s N: 앱 대신 경보마다 N초 뒤 확인(POST /alerts/{id}/ack, 기기 replay-app)을 넣는다.
    실측 때 보호자가 앱에서 확인을 누른 흐름(응급 유지 → 해제)을 폰 없이 같은 조건으로 재현한다(10-12 실측 앱 확인 5.7초).
결과: data/replay_bench/<시각>_<env>/ (Git 제외) — validation/*.csv, resources.csv, m5_log.jsonl, run.json

재생 중에는 실제 보드·마이크 입력이 섞이면 안 된다(보드 전원 끔·다른 망, 마이크 창 닫기).
RPi5: --ssh csi@<주소>로 재생을 RPi5 안(ai-experts 컨테이너)에서 돌리고, Redis는 ssh 터널(-L 6380:127.0.0.1:6379)로 읽는다.

사용:
  python scripts/dev/replay_bench.py --env laptop_gpu --capture data/clips/20261012_2307-2310_demo_2256/capture/capture.jsonl
  python scripts/dev/replay_bench.py --env rpi5 --capture ... --ssh csi@192.168.1.2 --api http://192.168.1.2:8000
"""
import argparse
import csv
import json
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import redis

ROOT = Path(__file__).resolve().parents[2]
PY = sys.executable


def wait_unlocked(r: redis.Redis, max_s: float) -> None:
    end = time.time() + max_s
    while time.time() < end:
        keys = list(r.scan_iter(match="phase2:active:*", count=100))
        if not keys:
            return
        print(f"[bench] Phase 2 잠금 {len(keys)}개 — 풀릴 때까지 대기", flush=True)
        time.sleep(5)
    print("[bench] 잠금이 남아 있지만 시작한다", flush=True)


def poll_resources(api: str, path: Path, stop: threading.Event) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts_ms", "cpu_pct", "per_core", "load1", "temp_c", "mem_used_gb", "mem_total_gb"])
        while not stop.is_set():
            try:
                d = json.load(urllib.request.urlopen(api + "/system/resources", timeout=3))
                m = d["memory"]
                w.writerow([int(time.time() * 1000), d["cpu"]["percent"], " ".join(map(str, d["cpu"]["per_core"])),
                            d["cpu"]["load_avg"][0], d.get("temp_c"),
                            round((m["total"] - m["available"]) / 1024 ** 3, 3), round(m["total"] / 1024 ** 3, 3)])
                f.flush()
            except Exception as exc:
                print(f"[bench] 자원 조회 실패: {exc}", flush=True)
            stop.wait(1.0)


def auto_ack(api: str, r: redis.Redis, delay_s: float, stop: threading.Event, log: list) -> None:
    last = f"{int(time.time() * 1000)}-0"
    pending = []                                   # (ack 시각, msg_id)
    while not stop.is_set():
        for sid, f in r.xrange("ai:emergency", min=f"({last}", max="+"):
            last = sid.decode()
            level = json.loads(f[b"data"]).get("risk_level")
            if level in ("warning", "critical"):
                pending.append((time.time() + delay_s, last))
        now = time.time()
        for due, mid in [p for p in pending if p[0] <= now]:
            pending.remove((due, mid))
            req = urllib.request.Request(f"{api}/alerts/{mid}/ack", method="POST",
                                         data=json.dumps({"device_id": "replay-app", "action": "seen"}).encode(),
                                         headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req, timeout=3)
                log.append({"msg_id": mid, "ack_ms": int(now * 1000)})
                print(f"[bench] 자동 확인 {mid}", flush=True)
            except Exception as exc:
                print(f"[bench] 자동 확인 실패 {mid}: {exc}", flush=True)
        stop.wait(0.5)


def qwen_logs(since_s: int, ssh: str | None) -> list[dict]:
    cmd = ["docker", "logs", "--since", f"{since_s}s", "rp5-ai-qwen"]
    if ssh:
        cmd = ["ssh", "-o", "BatchMode=yes", ssh, " ".join(cmd)]
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    rows = []
    for line in (out.stdout + out.stderr).splitlines():
        if line.startswith("{") and '"qwen_' in line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", required=True, help="laptop_gpu / laptop_cpu / rpi5")
    ap.add_argument("--capture", required=True)
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    ap.add_argument("--redis-port", type=int, default=6379, help="RPi5는 ssh 터널 포트(6380)")
    ap.add_argument("--ssh", default="", help="RPi5: csi@주소 — 재생을 RPi5 컨테이너 안에서 돌린다")
    ap.add_argument("--tail-s", type=float, default=30.0)
    ap.add_argument("--auto-ack-s", type=float, default=0.0, help="경보마다 N초 뒤 앱 확인 대신 넣기(0이면 안 함)")
    ap.add_argument("--root", default="data/replay_bench")
    args = ap.parse_args()

    out = ROOT / args.root / f"{datetime.now():%Y%m%d_%H%M%S}_{args.env}"
    out.mkdir(parents=True, exist_ok=True)
    r = redis.Redis(host="127.0.0.1", port=args.redis_port)
    r.ping()
    wait_unlocked(r, 150)

    commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    meta = {"env": args.env, "capture": args.capture, "api": args.api, "ssh": args.ssh or None,
            "laptop_commit": commit, "started_iso": datetime.now().isoformat(timespec="milliseconds")}
    try:
        meta["settings"] = json.load(urllib.request.urlopen(args.api + "/settings", timeout=3))
    except Exception:
        pass

    stop = threading.Event()
    threading.Thread(target=poll_resources, args=(args.api, out / "resources.csv", stop), daemon=True).start()
    acks: list = []
    if args.auto_ack_s > 0:
        threading.Thread(target=auto_ack, args=(args.api, r, args.auto_ack_s, stop, acks), daemon=True).start()
    rec = subprocess.Popen([PY, "-X", "utf8", "-u", str(ROOT / "scripts/dev/validation_recorder.py"),
                            "--label", args.env, "--root", str(out), "--port", str(args.redis_port)])
    time.sleep(3)

    t_replay = int(time.time() * 1000)
    if args.ssh:
        cap = Path(args.capture)
        subprocess.run(["scp", "-q", str(ROOT / "scripts/dev/npose_capture.py"), str(cap), f"{args.ssh}:/tmp/"], check=True)
        remote = ("docker cp /tmp/npose_capture.py rp5-ai-experts:/tmp/ && "
                  f"docker cp /tmp/{cap.name} rp5-ai-experts:/tmp/ && "
                  f"docker exec rp5-ai-experts python -u /tmp/npose_capture.py replay /tmp/{cap.name} "
                  "--udp-host sensing --redis-host db")
        subprocess.run(["ssh", "-o", "BatchMode=yes", args.ssh, remote], check=True)
    else:
        subprocess.run([PY, "-X", "utf8", "-u", str(ROOT / "scripts/dev/npose_capture.py"), "replay", args.capture,
                        "--udp-host", "127.0.0.1", "--redis-host", "127.0.0.1"], check=True)
    t_end = int(time.time() * 1000)
    print(f"[bench] 재생 끝 — {args.tail_s:.0f}초 더 기록", flush=True)
    time.sleep(args.tail_s)

    rec_dirs = sorted(out.glob(f"*_{args.env}"))
    for d in rec_dirs:
        (d / "STOP").touch()
    rec.wait(timeout=30)
    stop.set()
    if rec_dirs:
        rec_dirs[-1].rename(out / "validation")

    logs = qwen_logs(int(time.time() - t_replay / 1000) + 5, args.ssh or None)
    with open(out / "m5_log.jsonl", "w", encoding="utf-8") as f:
        for row in logs:
            if row.get("ts_ms", 0) >= t_replay:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    meta.update({"auto_ack_s": args.auto_ack_s, "auto_acks": acks, "replay_start_ms": t_replay, "replay_end_ms": t_end,
                 "ended_iso": datetime.now().isoformat(timespec="milliseconds")})
    (out / "run.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[bench] 끝 → {out}", flush=True)


if __name__ == "__main__":
    main()
