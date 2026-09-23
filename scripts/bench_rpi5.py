#!/usr/bin/env python3
"""RPi5 기본값/모델 비교 측정 도구.

RPi5 호스트에서 실행한다 (redis-py, docker, vcgencmd, ffmpeg 사용).
측정 동안 자원·지연·손실을 주기적으로 모으고, 끝나면 요약과 원시 기록을 남긴다.

    python3 scripts/bench_rpi5.py --label idle --duration 300
    python3 scripts/bench_rpi5.py --label audio --audio data/bench_audio/emergency_ko.mp3 --audio-every 10
    python3 scripts/bench_rpi5.py --label m5 --threshold 0.0 --duration 180
    python3 scripts/bench_rpi5.py --label m1-only --models m1

주의
- M3/M4 지연은 audio:result 스트림 ID 시각 − payload ts_ms (event→result, 대기열 포함).
  ai:result의 expert_latency_ms.env_sound/speech_ko는 오디오 워커 밖이라 쓰지 않는다.
- M5 지연은 ai-qwen 로그의 qwen_invoked.qwen_infer_ms.
- 패킷 손실은 node:N:health의 누적 비율이 아니라 측정 구간의 rx·last_seq 증가량으로 계산한다.
- --models/--threshold로 바꾼 설정은 종료 시(오류 포함) 원래 값으로 되돌린다.
- 결과는 reports/bench/ 아래에 쓴다 (Git 제외 경로). 운영 데이터가 아니라 측정 기록이다.
"""
import argparse
import datetime as dt
import json
import os
import platform
import re
import socket
import statistics
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import redis

REPO = Path(__file__).resolve().parents[1]
CONTAINERS = [
    "rp5-ai-experts",
    "rp5-ai-qwen",
    "rp5-sensing",
    "rp5-api",
    "rp5-db",
    "rp5-mqtt",
    "rp5-audio-sensing",
    "rp5-tts-worker",
    "rp5-ha",
]
EXPERT_LOG_EVENTS = [
    "csi_backlog_skipped", "packet_gap_detected", "expert_timeout", "expert_failure",
    "audio_m3_failed", "audio_m4_failed", "warmup_failed", "audio_worker_error",
]
ENV_KEYS = re.compile(
    r"^(ORT_|M[1-5]_|QWEN_|SLM_|EXPERT_|AI_DOCKER|SNAPSHOT_|RESULT_STREAM|"
    r"OMP_|OPENBLAS_|MKL_|NUMEXPR_|MALLOC_)"
)


# ── 유틸 ────────────────────────────────────────────────────────
def sh(cmd, timeout=30):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception as exc:  # 측정 보조 명령은 실패해도 측정 전체를 멈추지 않는다
        return f"<error: {exc}>"


def docker_logs(name, since):
    # 서비스 로그는 stderr로 나가므로 stdout과 합쳐서 읽는다
    try:
        return subprocess.run(["docker", "logs", "--since", since, name], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                              timeout=60).stdout
    except Exception as exc:
        return f"<error: {exc}>"


def http(api, path, body=None, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(api + path, data=data, method="POST" if body is not None else "GET",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def swap_used_mb():
    try:
        m = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, v = line.split(":", 1)
            m[k] = int(v.split()[0])
        return (m["SwapTotal"] - m["SwapFree"]) // 1024
    except (OSError, KeyError, ValueError):
        return None


def pressure_avg10(resource):
    """Linux PSI avg10. CPU 경합과 메모리 stall을 단순 CPU%와 별도로 기록한다."""
    try:
        rows = {}
        for line in Path(f"/proc/pressure/{resource}").read_text().splitlines():
            parts = line.split()
            rows[parts[0]] = {k: float(v) for k, v in (part.split("=", 1) for part in parts[1:])}
        return {kind: values.get("avg10") for kind, values in rows.items()}
    except (OSError, ValueError, IndexError):
        return {}


def restart_counts():
    out = {}
    for name in CONTAINERS:
        v = sh(["docker", "inspect", "--format", "{{.RestartCount}}", name]).strip()
        out[name] = int(v) if v.isdigit() else None
    return out


def pct(values, q):
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(round(q / 100 * (len(s) - 1))))]


def dist(values):
    if not values:
        return {"n": 0}
    return {"n": len(values), "p50": pct(values, 50), "p95": pct(values, 95),
            "max": max(values), "mean": round(statistics.fmean(values), 2)}


def parse_size_mb(value):
    """docker stats의 2.5GiB/430MiB 형식을 MiB로 변환한다. 0B는 측정 불가로 본다."""
    match = re.fullmatch(r"\s*([0-9.]+)\s*([KMGT]?i?B)\s*", str(value), re.IGNORECASE)
    if not match:
        return None
    number = float(match.group(1))
    unit = match.group(2).lower()
    factors = {
        "b": 1 / (1024 * 1024), "kb": 1000 / (1024 * 1024), "kib": 1 / 1024,
        "mb": 1_000_000 / (1024 * 1024), "mib": 1,
        "gb": 1_000_000_000 / (1024 * 1024), "gib": 1024,
        "tb": 1_000_000_000_000 / (1024 * 1024), "tib": 1024 * 1024,
    }
    size_mb = number * factors[unit]
    return round(size_mb, 2) if size_mb > 0 else None


def stream_id_ms(entry_id):
    return int(entry_id.split("-")[0])


# ── 수집기 ──────────────────────────────────────────────────────
class Collector:
    def __init__(self, r, api):
        self.r, self.api = r, api
        self.cursor = {}
        self.ai_result, self.audio_result, self.emergency = [], [], []
        self.resources, self.docker_stats, self.throttled, self.nodes = [], [], [], []

    def start_streams(self, start_ms):
        for s in ("ai:result", "audio:result", "ai:emergency"):
            self.cursor[s] = f"{start_ms}-0"

    def pull_stream(self, name, sink):
        # 커서 이후만 이어받는다 (MAXLEN 트리밍으로 앞부분이 사라지기 전에)
        while True:
            rows = self.r.xrange(name, min=f"({self.cursor[name]}", max="+", count=2000)
            if not rows:
                return
            for entry_id, fields in rows:
                try:
                    sink.append((entry_id, json.loads(fields.get("data", "{}"))))
                except ValueError:
                    pass
            self.cursor[name] = rows[-1][0]

    def node_health(self):
        snap = {}
        for key in sorted(self.r.scan_iter("node:*:health")):
            h = self.r.hgetall(key)
            snap[key.split(":")[1]] = {k: float(v) for k, v in h.items()}
        return snap

    def sample(self):
        t = time.time()
        os_pressure = {
            "cpu": pressure_avg10("cpu"),
            "memory": pressure_avg10("memory"),
        }
        try:
            self.resources.append({"t": t, **http(self.api, "/system/resources"),
                                   "swap_used_mb": swap_used_mb(), "pressure": os_pressure})
        except Exception as exc:
            self.resources.append({"t": t, "error": str(exc),
                                   "swap_used_mb": swap_used_mb(), "pressure": os_pressure})
        stats, memory_mb = {}, {}
        for line in sh(["docker", "stats", "--no-stream", "--format", "{{json .}}"]).splitlines():
            try:
                row = json.loads(line)
                stats[row["Name"]] = float(row["CPUPerc"].rstrip("%"))
                used_memory = str(row.get("MemUsage", "")).split("/", 1)[0].strip()
                parsed_memory = parse_size_mb(used_memory)
                if parsed_memory is not None:
                    memory_mb[row["Name"]] = parsed_memory
            except (ValueError, KeyError):
                pass
        self.docker_stats.append({"t": t, "cpu": stats, "memory_mb": memory_mb})
        self.throttled.append({"t": t, "raw": sh(["vcgencmd", "get_throttled"]).strip()})
        self.nodes.append({"t": t, "nodes": self.node_health()})
        self.pull_stream("ai:result", self.ai_result)
        self.pull_stream("audio:result", self.audio_result)
        self.pull_stream("ai:emergency", self.emergency)


# ── 오디오 주입 ─────────────────────────────────────────────────
def load_waveform(path):
    if str(path).lower().endswith(".wav"):
        # 16 kHz mono 16-bit WAV(평가 세트 형식)는 ffmpeg 없이 읽는다(노트북 Windows 호스트).
        import wave
        with wave.open(str(path), "rb") as w:
            if (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 16000):
                import array
                pcm = array.array("h")
                pcm.frombytes(w.readframes(w.getnframes()))
                return [round(v / 32768.0, 5) for v in pcm]
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "1", "-ar", "16000", "-"],
                         capture_output=True, check=True).stdout
    import array
    wav = array.array("f")
    wav.frombytes(raw[: len(raw) // 4 * 4])
    return [round(float(v), 5) for v in wav]


def audio_injector(api, waveform, every, stop, log):
    while not stop.wait(0):
        t = time.time()
        try:
            # trigger_ai=False: 실 ESP가 AI 루프를 계속 깨우므로 불필요. True면 API가 노드 CSI 스트림에
            # 빈 트리거 이벤트를 넣고(노드 입력 오염) csi:raw를 API 쪽 MAXLEN으로 잘라낸다.
            res = http(api, "/audio/events",
                       {"node_id": 1, "waveform": waveform, "sample_rate": 16000, "trigger_ai": False})
            log.append({"t": t, "ok": True, "id": res.get("audio_event_id")})
        except Exception as exc:
            log.append({"t": t, "ok": False, "error": str(exc)})
        stop.wait(max(0.0, every - (time.time() - t)))


# ── 요약 ────────────────────────────────────────────────────────
def summarize(c, qwen_ms, log_counts, start, end, injections):
    dur = end - start
    out = {"duration_s": round(dur, 1)}

    res = [x for x in c.resources if "cpu" in x]
    out["host_cpu_percent"] = dist([x["cpu"]["percent"] for x in res])
    out["host_temp_c"] = dist([x["temp_c"] for x in res if x.get("temp_c") is not None])
    out["host_mem_used_gb"] = dist([round((x["memory"]["total"] - x["memory"]["available"]) / 2**30, 2) for x in res])
    if res:
        out["disk_used_percent"] = round(res[-1]["disk"]["used"] / res[-1]["disk"]["total"] * 100, 1)
    out["container_cpu_percent"] = {
        name: dist([s["cpu"][name] for s in c.docker_stats if name in s["cpu"]]) for name in CONTAINERS
    }
    out["container_memory_mb"] = {
        name: dist([s["memory_mb"][name] for s in c.docker_stats if name in s.get("memory_mb", {})])
        for name in CONTAINERS
    }
    out["throttled"] = sorted({x["raw"] for x in c.throttled})
    out["swap_used_mb"] = dist([x["swap_used_mb"] for x in c.resources if x.get("swap_used_mb") is not None])
    out["cpu_pressure_some_avg10"] = dist([
        x.get("pressure", {}).get("cpu", {}).get("some") for x in c.resources
        if x.get("pressure", {}).get("cpu", {}).get("some") is not None
    ])
    out["memory_pressure_full_avg10"] = dist([
        x.get("pressure", {}).get("memory", {}).get("full") for x in c.resources
        if x.get("pressure", {}).get("memory", {}).get("full") is not None
    ])

    ai = [p for _, p in c.ai_result]
    lat = lambda k: [p["expert_latency_ms"][k] for p in ai if p.get("expert_latency_ms", {}).get(k)]
    out["ai_result"] = {
        "count": len(ai),
        "per_sec": round(len(ai) / dur, 2) if dur else None,
        "m1_fall_ms": dist(lat("fall")),
        "m2_vital_ms": dist(lat("vital")),
        "risk_levels": {lv: sum(1 for p in ai if p.get("risk_level") == lv) for lv in ("normal", "warning", "critical")},
        "slm_needed": sum(1 for p in ai if p.get("slm_needed")),
        "models": ai[-1].get("models") if ai else None,
        "m1_source": sorted({p.get("experts", {}).get("fall", {}).get("infer_source") for p in ai} - {None}),
        "m2_source": sorted({p.get("experts", {}).get("vital", {}).get("infer_source") for p in ai} - {None}),
    }

    delays = [stream_id_ms(i) - int(p["ts_ms"]) for i, p in c.audio_result if p.get("ts_ms")]
    gaps = [b - a for a, b in zip([stream_id_ms(i) for i, _ in c.audio_result][:-1],
                                  [stream_id_ms(i) for i, _ in c.audio_result][1:])]
    out["audio"] = {
        "injected": sum(1 for x in injections if x["ok"]),
        "inject_errors": sum(1 for x in injections if not x["ok"]),
        "results": len(c.audio_result),
        "m3_m4_event_to_result_ms": dist(delays),
        "result_interval_ms": dist(gaps),
        "m3_labels": sorted({p.get("env_sound", {}).get("env_sound_label") for _, p in c.audio_result} - {None, ""}),
        "m4_transcripts": sorted({p.get("speech_ko", {}).get("transcript_ko") for _, p in c.audio_result} - {None, ""})[:10],
        "m4_source": sorted({p.get("speech_ko", {}).get("stt_source") for _, p in c.audio_result} - {None}),
    }

    out["m5"] = {"emergency_entries": len(c.emergency), "qwen_infer_ms": dist(qwen_ms)}
    out["log_events"] = log_counts

    nodes = {}
    if len(c.nodes) >= 2:
        first, last = c.nodes[0]["nodes"], c.nodes[-1]["nodes"]
        span = c.nodes[-1]["t"] - c.nodes[0]["t"]
        for n in sorted(set(first) & set(last)):
            if not all(k in first[n] and k in last[n] for k in ("rx", "last_seq")):
                continue  # sensing을 거치지 않은 주입(더미)은 seq 정보가 없다
            d_rx = last[n]["rx"] - first[n]["rx"]
            d_seq = (int(last[n]["last_seq"]) - int(first[n]["last_seq"])) % (1 << 32)
            lost = max(0, d_seq - d_rx) if d_seq < (1 << 31) else None  # 역행이면 재부팅 → 산출 불가
            nodes[n] = {"rx_per_s": round(d_rx / span, 1) if span else None,
                        "loss_percent": round(lost / d_seq * 100, 2) if lost is not None and d_seq else None,
                        "rssi": last[n].get("rssi")}
    out["nodes"] = nodes
    out["csi_rx_per_s_total"] = round(sum(v["rx_per_s"] or 0 for v in nodes.values()), 1)
    return out


def fmt(d):
    if not d or not d.get("n"):
        return "—"
    return f"p50 {d['p50']:.1f} / p95 {d['p95']:.1f} / max {d['max']:.1f} (n={d['n']})"


def write_markdown(path, meta, s):
    L = [f"# RPi5 bench — {meta['label']}", "",
         f"- 시작: {meta['started']}  /  길이: {s['duration_s']} s",
         f"- 커밋: `{meta['git']['commit']}` ({meta['git']['branch']}){' — 워킹트리 변경 있음' if meta['git']['dirty'] else ''}",
         f"- 기기: {meta['host']['model']}  /  kernel {meta['host']['kernel']}",
         f"- 옵션: {json.dumps(meta['options'], ensure_ascii=False)}",
         f"- 모델 토글: {s['ai_result']['models']}",
         f"- 스로틀링: {', '.join(s['throttled'])}",
         f"- **유효성: {'유효' if s['valid'] else '무효 — ' + s.get('invalid_reason', 'API 조회 실패')}**"
         f"  (측정 중 재시작 {s['restarts']})", "",
         "## 자원", "",
         "| 항목 | 값 |", "|---|---|",
         f"| 호스트 CPU % | {fmt(s['host_cpu_percent'])} |",
         f"| CPU 온도 °C | {fmt(s['host_temp_c'])} |",
         f"| 메모리 사용 GB | {fmt(s['host_mem_used_gb'])} |",
         f"| 스왑 사용 MB | {fmt(s['swap_used_mb'])} |",
         f"| CPU PSI some avg10 % | {fmt(s['cpu_pressure_some_avg10'])} |",
         f"| 메모리 PSI full avg10 % | {fmt(s['memory_pressure_full_avg10'])} |",
         f"| 디스크 사용 % | {s.get('disk_used_percent', '—')} |"]
    for name, d in s["container_cpu_percent"].items():
        L.append(f"| {name} CPU % | {fmt(d)} |")
    for name, d in s["container_memory_mb"].items():
        L.append(f"| {name} memory MiB | {fmt(d)} |")
    a, au, m5 = s["ai_result"], s["audio"], s["m5"]
    L += ["", "## 지연", "", "| 모델 | 값 (ms) | 비고 |", "|---|---|---|",
          f"| M1 낙상 | {fmt(a['m1_fall_ms'])} | source {a['m1_source']} |",
          f"| M2 바이탈 | {fmt(a['m2_vital_ms'])} | source {a['m2_source']} |",
          f"| M3+M4 (이벤트→결과) | {fmt(au['m3_m4_event_to_result_ms'])} | 대기열 포함, 주입 {au['injected']}건 / 결과 {au['results']}건 |",
          f"| M5 Qwen | {fmt(m5['qwen_infer_ms'])} | ai:emergency {m5['emergency_entries']}건 |",
          "", "## 처리량·오류", "",
          f"- ai:result {a['count']}건 ({a['per_sec']}/s), 위험도 {a['risk_levels']}, slm_needed {a['slm_needed']}",
          f"- CSI 수신 합계 {s['csi_rx_per_s_total']} pkt/s",
          f"- 로그 이벤트: {s['log_events']}",
          f"- M3 라벨: {au['m3_labels']}  /  M4 source: {au['m4_source']}",
          f"- M4 전사 예: {au['m4_transcripts']}",
          "", "## 노드", "", "| 노드 | 수신 pkt/s | 손실 % | RSSI |", "|---|---|---|---|"]
    for n, v in s["nodes"].items():
        L.append(f"| {n} | {v['rx_per_s']} | {v['loss_percent']} | {v['rssi']} |")
    L += ["", "> 이 기록은 RPi5 실측이다. 지연·자원 수치는 낙상 정확도나 임상 안전성을 뜻하지 않는다."]
    path.write_text("\n".join(L) + "\n", encoding="utf-8")


# ── 메인 ────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label", required=True)
    ap.add_argument("--duration", type=int, default=300)
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--redis-host", default="localhost")
    ap.add_argument("--redis-port", type=int, default=6379)
    ap.add_argument("--audio", help="주입할 음성 파일 (ffmpeg로 16 kHz mono 디코드)")
    ap.add_argument("--audio-every", type=float, default=10.0)
    ap.add_argument("--models", help="측정 동안만 켤 모델, 예: m1,m3 (나머지는 끔)")
    ap.add_argument("--threshold", type=float, help="측정 동안만 적용할 risk_threshold")
    ap.add_argument("--warmup", type=float, default=10.0, help="설정 변경 후 수집 시작까지 대기(초)")
    ap.add_argument("--out", default=str(REPO / "reports" / "bench"))
    args = ap.parse_args()

    r = redis.Redis(host=args.redis_host, port=args.redis_port, decode_responses=True)
    r.ping()
    original = http(args.api, "/settings")
    changed = dict(original)
    if args.models:
        on = {m.strip() for m in args.models.split(",")}
        changed["models"] = {m: (m in on) for m in ("m1", "m2", "m3", "m4", "m5")}
    if args.threshold is not None:
        changed["risk_threshold"] = args.threshold

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out) / f"{stamp}-{args.label}"
    out_dir.mkdir(parents=True, exist_ok=True)

    meta = {
        "label": args.label,
        "started": dt.datetime.now().isoformat(timespec="seconds"),
        "options": {k: v for k, v in vars(args).items() if k not in ("out",)},
        "git": {
            "commit": sh(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"]).strip(),
            "branch": sh(["git", "-C", str(REPO), "branch", "--show-current"]).strip(),
            "dirty": bool(sh(["git", "-C", str(REPO), "status", "--porcelain", "--untracked-files=no"]).strip()),
        },
        "host": {
            "hostname": socket.gethostname(),
            "model": Path("/proc/device-tree/model").read_text(errors="ignore").strip("\x00\n")
            if Path("/proc/device-tree/model").exists() else platform.machine(),
            "kernel": platform.release(),
            "docker": sh(["docker", "version", "--format", "{{.Server.Version}}"]).strip(),
            "cpu_governor": (
                Path("/sys/devices/system/cpu/cpufreq/policy0/scaling_governor").read_text().strip()
                if Path("/sys/devices/system/cpu/cpufreq/policy0/scaling_governor").exists() else None
            ),
            "cgroup_controllers": (
                Path("/sys/fs/cgroup/cgroup.controllers").read_text().strip().split()
                if Path("/sys/fs/cgroup/cgroup.controllers").exists() else []
            ),
        },
        "settings_before": original,
        "settings_during": changed,
        "restarts_before": restart_counts(),
        "container_env": {},
        "container_images": {},
        "models": {},
    }
    for name in CONTAINERS:
        info = sh(["docker", "inspect", "--format", "{{json .}}", name])
        try:
            j = json.loads(info)
            meta["container_images"][name] = {
                "image": j["Config"]["Image"],
                "created": j["Created"],
                "cpu_shares": j.get("HostConfig", {}).get("CpuShares"),
            }
            meta["container_env"][name] = sorted(e for e in j["Config"]["Env"] if ENV_KEYS.match(e))
        except (ValueError, KeyError):
            meta["container_images"][name] = "not running"
    models_dir = REPO / "volumes" / "models"
    for p in sorted(models_dir.glob("*")):
        if p.is_dir():
            files = [f for f in p.rglob("*") if f.is_file()]
            meta["models"][p.name] = {"files": len(files), "bytes": sum(f.stat().st_size for f in files),
                                      "mtime": max((f.stat().st_mtime for f in files), default=0)}

    c = Collector(r, args.api)
    stop = threading.Event()
    injections = []
    injector = None
    try:
        if changed != original:
            http(args.api, "/settings", changed)
            print(f"[bench] 설정 변경 → {json.dumps({k: changed[k] for k in ('models', 'risk_threshold')})}", flush=True)
        if args.warmup:
            print(f"[bench] {args.warmup:.0f}초 대기 후 수집 시작", flush=True)
            time.sleep(args.warmup)

        start = time.time()
        c.start_streams(int(start * 1000))
        if args.audio:
            waveform = load_waveform(args.audio)
            print(f"[bench] 오디오 {len(waveform) / 16000:.1f}s, {args.audio_every:.0f}초마다 주입", flush=True)
            injector = threading.Thread(target=audio_injector,
                                        args=(args.api, waveform, args.audio_every, stop, injections), daemon=True)
            injector.start()

        next_t = start
        while time.time() - start < args.duration:
            c.sample()
            elapsed = time.time() - start
            last = c.resources[-1]
            cpu = last.get("cpu", {}).get("percent", "?")
            print(f"[bench] {elapsed:5.0f}/{args.duration}s  cpu={cpu}%  temp={last.get('temp_c')}  "
                  f"ai:result={len(c.ai_result)}  audio:result={len(c.audio_result)}  emergency={len(c.emergency)}",
                  flush=True)
            next_t += args.interval
            time.sleep(max(0.0, next_t - time.time()))
        stop.set()
        if injector:
            injector.join(timeout=30)
        time.sleep(3)  # 마지막 주입분 처리 여유
        c.sample()
        end = time.time()
    finally:
        stop.set()
        if changed != original:
            try:
                http(args.api, "/settings", original)
                print("[bench] 설정 원복 완료", flush=True)
            except Exception as exc:
                print(f"[bench] !!! 설정 원복 실패 — 수동으로 되돌릴 것: {exc}", flush=True)

    since = str(int(start))
    qwen_log = docker_logs("rp5-ai-qwen", since)
    qwen_ms = []
    for line in qwen_log.splitlines():
        if '"qwen_invoked"' in line:
            m = re.search(r'"qwen_infer_ms":\s*([0-9.]+)', line)
            if m:
                qwen_ms.append(float(m.group(1)))
    expert_log = docker_logs("rp5-ai-experts", since)
    log_counts = {ev: expert_log.count(f'"{ev}"') for ev in EXPERT_LOG_EVENTS}

    summary = summarize(c, qwen_ms, log_counts, start, end, injections)
    restarts_after = restart_counts()
    summary["restarts"] = {k: (restarts_after[k] - meta["restarts_before"][k])
                           for k in restarts_after
                           if restarts_after[k] is not None and meta["restarts_before"].get(k) is not None}
    oom_suspect = any(v > 0 for v in summary["restarts"].values())
    summary["valid"] = not oom_suspect and not any(x.get("error") for x in c.resources)
    if oom_suspect:
        summary["invalid_reason"] = "측정 중 컨테이너 재시작 (OOM 의심)"
    raw = {
        "meta": meta, "summary": summary, "injections": injections,
        "resources": c.resources, "docker_stats": c.docker_stats, "throttled": c.throttled, "nodes": c.nodes,
        "ai_result": [{"id": i, "ts_ms": p.get("ts_ms"), "node_id": p.get("node_id"),
                       "risk_level": p.get("risk_level"), "risk_score": p.get("risk_score"),
                       "slm_needed": p.get("slm_needed"), "expert_latency_ms": p.get("expert_latency_ms"),
                       "fall_score": p.get("experts", {}).get("fall", {}).get("fall_score")}
                      for i, p in c.ai_result],
        "audio_result": [{"id": i, **p} for i, p in c.audio_result],
        "emergency": [{"id": i, **p} for i, p in c.emergency],
        "qwen_infer_ms": qwen_ms,
    }
    (out_dir / "raw.json").write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "logs-ai-experts.txt").write_text(expert_log, encoding="utf-8")
    write_markdown(out_dir / "summary.md", meta, summary)
    print("\n" + (out_dir / "summary.md").read_text(encoding="utf-8"))
    print(f"[bench] 저장: {out_dir}")


if __name__ == "__main__":
    main()
