#!/usr/bin/env python3
"""N_pose 노드 품질·보드 M1 판정 측정 — 지정 시간 동안 m1:score와 노드 헬스를 읽어 노드별 표를 만든다.

  - CSI2 수신(노드 헬스 증가량): 수신 Hz, 손실 %, 새 프레임 비율(김태연 GO 기준 ≥ 85%)
  - CSR!(m1:score): 추론 Hz, 창이 찬 비율, coverage(창 안 새 프레임) 분포, 쓸 수 있는 추론 비율,
    sig_mode·model_ver·무작위 모델 여부, 보드 추론 시간, 점수 분포·0.80 초과 비율
  - 판정 재현: ai/runtime_inputs.BoardScoreAggregator(= ai-experts M1_SOURCE=board = N_pose hub/receiver.py 규칙)를
    200 ms마다 돌려 노드 경보·허브 경보 상승 횟수(시간당)를 규칙 후보별(임계·K/N·min_nodes)로 낸다.
    낙상 쪽 재현율은 WiFall 검증 세트(handoff/M1_WIFALL_VAL_20261005.md)와 짝지어 규칙을 고른다.
  - 원 점수는 --save-rows(.npz)로 남겨 나중에 다른 규칙으로 다시 계산할 수 있다.
운영 설정(sys:settings의 M1 켜짐 여부)과 무관하게 잰다 — M1을 꺼 둔 채 재면 경보·푸시가 나가지 않는다.

사용: python scripts/dev/npose_quality.py --seconds 300 --label empty_room --out reports/laptop/val/npose_quality.json
"""
import argparse
import json
import os
import sys
import time

import numpy as np
import redis

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "ai"))
from runtime_inputs import BoardScoreAggregator  # noqa: E402

NODES = range(1, 7)


def health(r):
    out = {}
    for n in NODES:
        h = r.hgetall(f"node:{n}:health")
        if h:
            h = {k.decode(): v.decode() for k, v in h.items()}
            out[n] = {"rx": int(h.get("rx", 0)), "lost": int(h.get("lost", 0)),
                      "fresh_rate": float(h.get("fresh_rate", "nan")), "rssi": int(h.get("rssi", 0))}
    return out


def pct(a, q):
    return round(float(np.percentile(a, q)), 4) if len(a) else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=300)
    ap.add_argument("--label", default="session")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=6379)
    ap.add_argument("--threshold", type=float, default=0.8)
    ap.add_argument("--min-coverage", type=float, default=0.9)
    ap.add_argument("--out", required=True)
    ap.add_argument("--save-rows", default=None, help="원 점수 저장(.npz). 기본: --out 옆 같은 이름")
    a = ap.parse_args()
    r = redis.Redis(host=a.host, port=a.port)
    h0, t0 = health(r), time.time()
    last_id, rows = "$", []
    print(f"[npose] {a.seconds:.0f}s 수집 시작 ({a.label})", flush=True)
    while time.time() - t0 < a.seconds:
        resp = r.xread({"m1:score": last_id}, block=1000, count=1000)
        for _, msgs in resp or []:
            for mid, f in msgs:
                last_id = mid
                f = {k.decode(): v.decode() for k, v in f.items()}
                rows.append((int(mid.decode().split("-")[0]), int(f["node"]), float(f["score"]), float(f["coverage"]),
                             int(f["flags"]), int(f["infer_ms"]), int(f["model_ver"])))
    h1, t1 = health(r), time.time()
    dur = t1 - t0
    arr = np.array(rows, dtype=float) if rows else np.zeros((0, 7))
    rep = {"label": a.label, "seconds": round(dur, 1), "started": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t0)),
           "rule": {"threshold": a.threshold, "k": 3, "n": 5, "min_coverage": a.min_coverage}, "nodes": {}}
    for n in NODES:
        m = arr[:, 1] == n if len(arr) else np.zeros(0, bool)
        d = arr[m]
        node = {}
        if n in h0 and n in h1:
            drx, dlost = h1[n]["rx"] - h0[n]["rx"], h1[n]["lost"] - h0[n]["lost"]
            fresh = (h1[n]["fresh_rate"] * h1[n]["rx"] - h0[n]["fresh_rate"] * h0[n]["rx"]) / drx if drx > 0 else None
            node.update(csi_hz=round(drx / dur, 2), loss_pct=round(100 * dlost / max(drx + dlost, 1), 3),
                        fresh_pct=round(100 * fresh, 1) if fresh is not None and fresh == fresh else None,
                        rssi=h1[n]["rssi"])
        if len(d):
            flags = d[:, 4].astype(int)
            full, rand = (flags & 2) > 0, (flags & 1) > 0
            usable = full & (d[:, 3] >= a.min_coverage) & ~rand
            node.update(result_hz=round(len(d) / dur, 2), window_full_pct=round(100 * full.mean(), 1),
                        coverage_p10_p50=[pct(d[:, 3], 10), pct(d[:, 3], 50)],
                        usable_pct=round(100 * usable.mean(), 1),
                        sig_mode=sorted({int(v) for v in (flags >> 2) & 3}),
                        model_ver=sorted({int(v) for v in d[:, 6]}), random_model=bool(rand.any()),
                        infer_ms_p50_p95_max=[pct(d[:, 5], 50), pct(d[:, 5], 95), int(d[:, 5].max())],
                        score_p50_p95_max=[pct(d[:, 2], 50), pct(d[:, 2], 95), round(float(d[:, 2].max()), 4)],
                        fire80_usable_pct=round(100 * float((usable & (d[:, 2] >= a.threshold)).mean()), 2))
        if node:
            rep["nodes"][n] = node
    np.savez_compressed(a.save_rows or os.path.splitext(a.out)[0] + "_rows.npz", rows=arr, t0=t0, t1=t1,
                        cols=np.array(["recv_ms", "node", "score", "coverage", "flags", "infer_ms", "model_ver"]))
    # 판정 재현(200 ms 격자) — 규칙 후보별
    hours = dur / 3600
    rules = [(a.threshold, 3, 5, 1), (0.8, 2, 5, 1), (0.8, 1, 5, 1), (0.9, 1, 5, 1), (0.9, 2, 5, 1),
             (0.95, 1, 5, 1), (0.8, 3, 5, 2), (0.9, 1, 5, 2)]
    for thr, k, n_, mn in rules:
        agg = BoardScoreAggregator(threshold=thr, required_votes=k, window_size=n_, min_nodes=mn,
                                   min_coverage=a.min_coverage)
        i, prev_hub, rises, node_rises, prev_nodes, on_ticks, ticks = 0, False, 0, 0, set(), 0, 0
        order = np.argsort(arr[:, 0], kind="stable") if len(arr) else []
        srt = arr[order] if len(arr) else arr
        for now in np.arange(t0 * 1000, t1 * 1000, 200):
            while i < len(srt) and srt[i, 0] <= now:
                agg.add(int(srt[i, 1]), int(srt[i, 0]), srt[i, 2], srt[i, 3], int(srt[i, 4]))
                i += 1
            res = agg.result(int(now))
            hub, nodes = bool(res["fall_detected"]), set(res.get("alarm_nodes", []))
            rises += hub and not prev_hub
            node_rises += len(nodes - prev_nodes)
            on_ticks += hub
            ticks += 1
            prev_hub, prev_nodes = hub, nodes
        rep[f"hub_thr{thr}_k{k}of{n_}_min{mn}"] = {"alarm_rises": rises, "alarm_rises_per_h": round(rises / hours, 1),
                                      "alarm_on_pct": round(100 * on_ticks / max(ticks, 1), 2),
                                      "node_alarm_rises_per_h": round(node_rises / hours, 1)}
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(rep, fh, ensure_ascii=False, indent=1)
    for n, v in rep["nodes"].items():
        print(n, json.dumps(v, ensure_ascii=False))
    print("hub", json.dumps({k: v for k, v in rep.items() if k.startswith("hub")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
