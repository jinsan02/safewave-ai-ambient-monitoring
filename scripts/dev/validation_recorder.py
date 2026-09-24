#!/usr/bin/env python3
"""노트북 실제 검증(영상 시나리오 시험)용 — 모델별 출력을 시간순 CSV로 저장한다. 실험 기록용(운영 경로 아님).

Redis 스트림 3개를 새로 들어오는 것부터 읽는다(서비스 코드·Redis 키는 건드리지 않음).
  ai:result    → m1_fall.csv(M1 창 점수·K/N 투표), m2_vital.csv(심박·호흡), gate.csv(규칙 점수·등급·M5 호출 여부)
  audio:result → m3_env.csv(환경음), m4_stt.csv(전사·키워드) — 오디오 이벤트 1건당 1행
  ai:emergency → m5_emergency.csv(M5 판단 전부 + ai-experts 규칙 경보, slm_mode로 구분)
모든 행에 세 가지 시각을 적는다: stream_ms(Redis 기록 시각), ts_ms(payload 시각), host_iso(노트북 시계 —
영상 속 화면 시계와 맞출 때 사용). audio:result·ai:emergency 원문은 raw_*.jsonl에도 남긴다
(--raw-result면 ai:result 원문도, 시간당 약 100 MB).

저장 위치: data/validation/<시작시각>_<label>/ (Git 제외). 행마다 바로 flush — 중간에 꺼져도 남는다.
멈추기: Ctrl+C 또는 <저장 폴더>/STOP 파일 생성. 끝나면 summary.json(건수·시간 범위).
사용:
  python scripts/dev/validation_recorder.py --label scenario1
  python scripts/dev/validation_recorder.py --label day1 --nodes 1,2,3 --raw-result
"""
import argparse
import csv
import json
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

import redis

STREAMS = ("ai:result", "audio:result", "ai:emergency")

COLUMNS = {
    "m1_fall": ["fall_score", "window_fall_detected", "fall_detected", "fall_votes",
                "fall_vote_samples", "fall_vote_required", "input_status", "infer_confidence"],
    "m2_vital": ["heart_rate", "breathing_rate", "infer_confidence"],
    "gate": ["risk_score", "risk_level", "emergency", "slm_needed", "rule_alert",
             "fall_consensus_bypass", "fall_hazard_bypass", "vital_bypass", "keyword_fall_bonus",
             "temporal_escalation", "breakdown"],
    "m3_env": ["env_sound_label", "env_sound_confidence", "impact_prob", "impact_alert", "raw_peak",
               "env_sound_source", "duration_ms", "peak_db", "probs"],
    "m4_stt": ["transcript_ko", "keywords", "stt_confidence", "speech_detected", "duration_ms"],
    "m5_emergency": ["slm_mode", "risk_level", "risk_score", "gate_score", "emergency", "slm_invoked",
                     "qwen_reason", "summary", "breakdown"],
}
BASE = ["stream_id", "stream_ms", "host_iso", "ts_ms", "node_id"]


def _jsonish(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return value


class Recorder:
    def __init__(self, out_dir: Path, raw_result: bool):
        self.out_dir = out_dir
        self.files, self.writers, self.counts = {}, {}, {}
        for name, cols in COLUMNS.items():
            f = open(out_dir / f"{name}.csv", "w", newline="", encoding="utf-8-sig")
            w = csv.writer(f)
            w.writerow(BASE + cols)
            self.files[name], self.writers[name], self.counts[name] = f, w, 0
        raw_names = ["audio:result", "ai:emergency"] + (["ai:result"] if raw_result else [])
        self.raw = {s: open(out_dir / f"raw_{s.replace(':', '_')}.jsonl", "w", encoding="utf-8")
                    for s in raw_names}
        self.first_ms = self.last_ms = None

    def row(self, name, base, values):
        cols = COLUMNS[name]
        self.writers[name].writerow(base + [_jsonish(values.get(c)) for c in cols])
        self.files[name].flush()
        self.counts[name] += 1

    def handle(self, stream, msg_id, fields, nodes):
        sid = msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)
        stream_ms = int(sid.split("-")[0])
        raw = fields.get(b"data") or fields.get("data") or b"{}"
        raw = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        try:
            p = json.loads(raw)
        except ValueError:
            return
        node = p.get("node_id")
        if nodes and node is not None and int(node) not in nodes:
            return
        self.first_ms = self.first_ms or stream_ms
        self.last_ms = stream_ms
        host_iso = datetime.now().isoformat(timespec="milliseconds")
        base = [sid, stream_ms, host_iso, p.get("ts_ms"), node]
        if stream in self.raw:
            self.raw[stream].write(json.dumps({"stream_id": sid, "host_iso": host_iso, "data": p},
                                              ensure_ascii=False) + "\n")
            self.raw[stream].flush()

        if stream == "ai:result":
            ex = p.get("experts") or {}
            self.row("m1_fall", base, ex.get("fall") or {})
            self.row("m2_vital", base, ex.get("vital") or {})
            bd = p.get("emergency_breakdown") or {}
            gate = {k: p.get(k) for k in ("risk_score", "risk_level", "emergency", "slm_needed", "rule_alert")}
            gate.update({k: bd.get(k) for k in ("fall_consensus_bypass", "fall_hazard_bypass",
                                                 "vital_bypass", "keyword_fall_bonus", "temporal_escalation")})
            gate["breakdown"] = {k: v for k, v in bd.items() if k in ("fall", "vital", "sound", "speech")}
            self.row("gate", base, gate)
        elif stream == "audio:result":
            env = dict(p.get("env_sound") or {})
            env.update({"duration_ms": p.get("duration_ms"), "peak_db": p.get("peak_db")})
            env.setdefault("probs", env.get("env_sound_probs"))
            self.row("m3_env", base, env)
            stt = dict(p.get("speech_ko") or {})
            stt["duration_ms"] = p.get("duration_ms")
            self.row("m4_stt", base, stt)
        elif stream == "ai:emergency":
            p["breakdown"] = p.get("emergency_breakdown")
            self.row("m5_emergency", base, p)

    def close(self, meta):
        for f in list(self.files.values()) + list(self.raw.values()):
            f.close()
        meta.update({
            "rows": self.counts,
            "first_stream_ms": self.first_ms, "last_stream_ms": self.last_ms,
            "first_iso": datetime.fromtimestamp(self.first_ms / 1000).isoformat() if self.first_ms else None,
            "last_iso": datetime.fromtimestamp(self.last_ms / 1000).isoformat() if self.last_ms else None,
        })
        (self.out_dir / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                                   encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--label", default="session")
    ap.add_argument("--root", default="data/validation")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=6379)
    ap.add_argument("--nodes", default="", help="예: 1,2,3 (비우면 전부)")
    ap.add_argument("--raw-result", action="store_true", help="ai:result 원문도 jsonl로 저장(용량 큼)")
    args = ap.parse_args()

    started = datetime.now()
    out_dir = Path(args.root) / f"{started:%Y%m%d_%H%M%S}_{args.label}"
    out_dir.mkdir(parents=True, exist_ok=False)
    nodes = {int(n) for n in args.nodes.split(",") if n.strip()}
    r = redis.Redis(host=args.host, port=args.port)
    r.ping()
    rec = Recorder(out_dir, args.raw_result)
    last_ids = {s: "$" for s in STREAMS}   # 지금부터 들어오는 것만
    stop = {"flag": False}
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))
    print(f"[validation] 기록 시작 {started:%H:%M:%S} → {out_dir}  (멈추기: Ctrl+C 또는 {out_dir / 'STOP'})",
          flush=True)
    last_print = time.time()
    while not stop["flag"] and not (out_dir / "STOP").exists():
        try:
            resp = r.xread(last_ids, block=1000, count=500)
        except redis.exceptions.ConnectionError as exc:
            print(f"[validation] Redis 재연결 대기: {exc}", file=sys.stderr, flush=True)
            time.sleep(2)
            continue
        for stream, messages in resp or []:
            s = stream.decode() if isinstance(stream, bytes) else stream
            for msg_id, fields in messages:
                last_ids[s] = msg_id
                rec.handle(s, msg_id, fields, nodes)
        if time.time() - last_print >= 60:
            last_print = time.time()
            print(f"[validation] {datetime.now():%H:%M:%S} " +
                  " ".join(f"{k}={v}" for k, v in rec.counts.items()), flush=True)
    rec.close({"label": args.label, "started": started.isoformat(), "ended": datetime.now().isoformat(),
               "nodes": sorted(nodes) or "all", "raw_result": args.raw_result})
    print(f"[validation] 종료 — {out_dir / 'summary.json'}", flush=True)


if __name__ == "__main__":
    main()
