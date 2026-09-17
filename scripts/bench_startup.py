#!/usr/bin/env python3
"""RPi5 서비스 기동 준비 시간 측정.

컨테이너를 다시 만들고(up -d --force-recreate) 서비스별 준비 신호까지의 시간과
기동 중 메모리·스왑 최고치를 잰다. RPi5 호스트의 ~/safewave에서 실행한다.

    python3 scripts/bench_startup.py --runs 3
    python3 scripts/bench_startup.py --runs 1 --services ai-experts

준비 신호
- ai-experts: 로그 engine_init_completed (단계별 engine_init_step 시각 포함)
- ai-qwen:    로그 qwen_service_started (qwen_warmup_completed 포함)
- api:        GET /status 응답
- sensing:    node:*:health last_seen이 기동 이후로 갱신
- 파이프라인: 기동 이후 첫 ai:result 항목

측정마다 파이프라인이 1분가량 멈춘다. 모델 파일은 페이지 캐시에 올라간 상태(warm)라
재부팅 직후(cold) 값과 다를 수 있으므로 결과에 조건을 적는다.
"""
import argparse
import datetime as dt
import json
import re
import statistics
import subprocess
import time
import urllib.request
from pathlib import Path

import redis

REPO = Path(__file__).resolve().parents[1]
CONTAINER = {"ai-experts": "rp5-ai-experts", "ai-qwen": "rp5-ai-qwen", "api": "rp5-api", "sensing": "rp5-sensing"}


def run(cmd, timeout=120):
    return subprocess.run(cmd, cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, timeout=timeout).stdout


def meminfo():
    m = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        k, v = line.split(":", 1)
        m[k] = int(v.split()[0]) // 1024
    return {"used_mb": m["MemTotal"] - m["MemAvailable"], "swap_used_mb": m["SwapTotal"] - m["SwapFree"]}


def events(container, since):
    out = {}
    for line in run(["docker", "logs", "--since", since, container]).splitlines():
        m = re.search(r'"event":\s*"([^"]+)".*?"ts_ms":\s*(\d+)', line)
        if not m:
            continue
        ev, ts = m.group(1), int(m.group(2))
        if ev == "engine_init_step":
            step = re.search(r'"step":\s*"([^"]+)"', line)
            ev = f"step:{step.group(1)}" if step else ev
        out.setdefault(ev, ts)
    return out


def one_run(r, services, api, timeout):
    before = meminfo()
    t0 = time.time()
    since = str(int(t0) - 1)
    run(["docker", "compose", "up", "-d", "--force-recreate", "--no-deps", *services], timeout=180)
    t_up = time.time()

    ready, peak = {}, dict(before)
    first_result = None
    last_log_check = 0.0
    while time.time() - t0 < timeout:
        mi = meminfo()
        peak = {k: max(peak[k], mi[k]) for k in peak}
        now = time.time()
        # docker logs 호출은 무거우므로 2초 간격 (로그 이벤트의 ts_ms를 쓰므로 정밀도는 유지)
        check_logs = now - last_log_check >= 2.0
        if check_logs:
            last_log_check = now
        if check_logs and "ai-experts" in services and "ai-experts" not in ready:
            ev = events(CONTAINER["ai-experts"], since)
            if "engine_init_completed" in ev:
                ready["ai-experts"] = ev
        if check_logs and "ai-qwen" in services and "ai-qwen" not in ready:
            ev = events(CONTAINER["ai-qwen"], since)
            if "qwen_service_started" in ev or "qwen_warmup_failed" in ev:
                ready["ai-qwen"] = ev
        if "api" in services and "api" not in ready:
            try:
                urllib.request.urlopen(api + "/status", timeout=2).read()
                ready["api"] = {"http_ok": int(now * 1000)}
            except Exception:
                pass
        if "sensing" in services and "sensing" not in ready:
            seen = [float(r.hget(k, "last_seen") or 0) for k in r.scan_iter("node:*:health")]
            if any(s > t_up for s in seen):
                ready["sensing"] = {"node_seen": int(max(seen) * 1000)}
        if first_result is None:
            rows = r.xrange("ai:result", min=f"{int(t_up * 1000)}-0", max="+", count=1)
            if rows:
                first_result = int(rows[0][0].split("-")[0])
        if all(s in ready for s in services) and first_result is not None:
            break
        time.sleep(0.5)

    base = int(t0 * 1000)
    res = {"up_cmd_s": round(t_up - t0, 2), "timeout": time.time() - t0 >= timeout}
    for svc in services:
        ev = ready.get(svc)
        if ev is None:
            res[svc] = None
            continue
        done = ev.get("engine_init_completed") or ev.get("qwen_service_started") or ev.get("qwen_warmup_failed") \
            or ev.get("http_ok") or ev.get("node_seen")
        detail = {k: round((v - base) / 1000, 2) for k, v in sorted(ev.items(), key=lambda kv: kv[1])}
        res[svc] = {"ready_s": round((done - base) / 1000, 2), "events_s": detail}
    res["first_ai_result_s"] = round((first_result - base) / 1000, 2) if first_result else None
    res["mem_before"], res["mem_peak"], res["mem_after"] = before, peak, meminfo()
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--services", nargs="+", default=["ai-experts", "ai-qwen", "api", "sensing"])
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--timeout", type=float, default=600)
    ap.add_argument("--settle", type=float, default=30, help="회차 사이 안정화 대기(초)")
    ap.add_argument("--condition", default="warm (모델 파일 페이지 캐시 적재 상태)")
    ap.add_argument("--out", default=str(REPO / "reports" / "startup"))
    args = ap.parse_args()

    r = redis.Redis(decode_responses=True)
    runs = []
    for i in range(1, args.runs + 1):
        print(f"[startup] {i}/{args.runs} 재기동: {' '.join(args.services)}", flush=True)
        res = one_run(r, args.services, args.api, args.timeout)
        runs.append(res)
        print(f"[startup]   " + "  ".join(f"{s}={(res[s] or {}).get('ready_s')}s" for s in args.services)
              + f"  첫 ai:result={res['first_ai_result_s']}s  메모리 최고 {res['mem_peak']['used_mb']}MB"
              + f" 스왑 {res['mem_peak']['swap_used_mb']}MB", flush=True)
        if i < args.runs:
            time.sleep(args.settle)

    def col(get):
        vals = [v for v in (get(x) for x in runs) if v is not None]
        return f"{statistics.median(vals):.1f} ({min(vals):.1f}~{max(vals):.1f})" if vals else "—"

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(args.out) / stamp
    out.mkdir(parents=True, exist_ok=True)
    commit = run(["git", "rev-parse", "--short", "HEAD"]).strip()
    md = [f"# RPi5 기동 준비 시간 — {stamp}", "",
          f"- 커밋 `{commit}`, 회차 {args.runs}, 조건: {args.condition}",
          f"- 재기동 대상: {', '.join(args.services)} (`docker compose up -d --force-recreate --no-deps`)", "",
          "| 항목 | 중앙값 (최소~최대) s |", "|---|---|"]
    for s in args.services:
        md.append(f"| {s} 준비 | {col(lambda x, s=s: (x[s] or {}).get('ready_s'))} |")
    md.append(f"| 첫 ai:result | {col(lambda x: x['first_ai_result_s'])} |")
    md.append(f"| 기동 중 메모리 최고 MB | {col(lambda x: x['mem_peak']['used_mb'])} |")
    md.append(f"| 기동 중 스왑 최고 MB | {col(lambda x: x['mem_peak']['swap_used_mb'])} |")
    md.append(f"| 안정 후 메모리 MB | {col(lambda x: x['mem_after']['used_mb'])} |")
    if runs and runs[0].get("ai-experts"):
        md += ["", "ai-experts 단계별 시각 (1회차, 재기동 명령 기준 s)", ""]
        md += [f"- {k}: {v}" for k, v in runs[0]["ai-experts"]["events_s"].items()]
    if runs and runs[0].get("ai-qwen"):
        md += ["", "ai-qwen 단계별 시각 (1회차)", ""]
        md += [f"- {k}: {v}" for k, v in runs[0]["ai-qwen"]["events_s"].items()]
    (out / "raw.json").write_text(json.dumps({"args": vars(args), "commit": commit, "runs": runs},
                                             ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "summary.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))
    print(f"[startup] 저장: {out}")


if __name__ == "__main__":
    main()
