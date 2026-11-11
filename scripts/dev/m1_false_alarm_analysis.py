#!/usr/bin/env python3
"""M1 오경보 감사 — 기록된 CSI(csv_logger CSV 또는 1분 엑셀)에 인계 M1 모델을 그대로 돌려
데이터 계약·시계·입력 조립 방식별 낙상 판정 빈도와 모델 민감도를 본다. 낙상이 없는 기록이면 판정 = 오경보.

입력 조립(모두 200 ms마다, 노드 1·2·3 → (1,5,64,100), 임계 0.80, 5회 중 3회, 규칙 경보 재발동 90 s)
  runtime : 지금 ai/main.py 그대로 — 노드별 deque(수신 시각 격자, 빈 칸 0, 같은 칸은 나중 프레임),
            창끝 게이트 15 ms. 노드마다 자기 마지막 프레임에서 창이 끝난다.
  loader  : 학습 로더(m1_runtime.align_to_grid)와 같은 수신 시각 공통 격자, 공통 창끝을 50 ms 늦춰
            늦게 온 패킷까지 넣는다. 지금 모델의 학습 입력 분포와 가장 가깝다.
  board   : 보드 시계(ts_ms) + 노드별 오프셋(파일 중앙값) 공통 격자, 창끝 50 ms 지연. 새 모델용 정렬 후보.
            지금 모델은 이렇게 꽉 찬 창을 학습에서 본 적이 없다(학습 채움 71~97%).

사용: python scripts/dev/m1_false_alarm_analysis.py <csv|xlsx> [...] [--sens] [--out result.json]
"""
import argparse
import json
import os
import sys
from collections import deque

import numpy as np
import onnxruntime as ort
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "ai"))
from runtime_inputs import (build_m1_input, grid_slot, m1_tail_ready,  # noqa: E402
                            m1_window_ready, place_m1_grid_frame)

NODES = (1, 2, 3)
GUARD = [0, 1, 2, 3, 4, 5, 32, 59, 60, 61, 62, 63]
THR, K, N, TICK_MS, LAG_SLOTS, COOLDOWN_S = 0.80, 3, 5, 200, 5, 90


def load(path):
    df = pd.read_excel(path) if path.lower().endswith((".xlsx", ".xls")) else pd.read_csv(path)
    rx = df["stream_id"].astype(str).str.split("-").str[0].astype(np.int64).to_numpy()
    order = np.argsort(rx, kind="stable")
    raw = df[[f"raw_{i}" for i in range(64)]].to_numpy(np.float32)[order]
    return rx[order], df["node_id"].to_numpy(int)[order], df["ts_ms"].to_numpy(np.int64)[order], raw


def contract(rx, nd, ts, raw):
    """가드 0·프레임 피크 1(ESP 정규화), 연속 동일 프레임(오래된 CSI 재전송), 두 종류 프레임 혼재, 시계."""
    out = {}
    for n in NODES:
        m = nd == n
        if m.sum() < 200:
            continue
        x, t, r = raw[m], ts[m], rx[m]
        peak = np.abs(np.delete(x, GUARD, axis=1)).max(1)
        rep = np.r_[False, np.abs(np.diff(x, axis=0)).max(1) == 0]
        xn = x[:, 6:59] / (np.linalg.norm(x[:, 6:59], axis=1, keepdims=True) + 1e-9)
        c0 = xn[0]; c1 = xn[int(np.argmin(xn @ c0))]
        for _ in range(20):  # 코사인 2-평균
            lab = (xn @ c1) > (xn @ c0)
            if lab.all() or (~lab).all():
                break
            c0 = xn[~lab].mean(0); c0 /= np.linalg.norm(c0)
            c1 = xn[lab].mean(0); c1 /= np.linalg.norm(c1)
        dt = np.diff(t)
        off = r - t
        dur_s = (r[-1] - r[0]) / 1000
        out[n] = {
            "frames": int(m.sum()), "rx_hz": round(m.sum() / dur_s, 2),
            "guard_max": float(np.abs(x[:, GUARD]).max()), "peak_not_1": round(float(np.mean(np.abs(peak - 1) > 1e-5)), 4),
            "nan": int(np.isnan(x).sum()),
            "repeat_ratio": round(float(rep.mean()), 3), "fresh_hz": round(float((~rep).sum() / dur_s), 1),
            "mix_minor": round(float(min(lab.mean(), 1 - lab.mean())), 3), "mix_switch_prob": round(float(np.mean(lab[1:] != lab[:-1])), 3),
            "dev_dt_not_10": round(float(np.mean(dt != 10)), 4), "dev_jumps_gt50ms": int(np.sum(np.abs(dt - 10) > 50)),
            "rx_minus_ts_p1_p99": [float(np.percentile(off - np.median(off), q)) for q in (1, 99)],
            "drift_ms_per_s": round(float(np.polyfit((r - r[0]) / 1000.0, off - off[0], 1)[0]), 4),
        }
    return out


def assemble(rx, nd, ts, raw, mode):
    """200 ms tick마다 (1,5,64,100) 창, 창 채움, 창 시각(수신 ms)을 만든다."""
    nodes = [n for n in NODES if (nd == n).any()]
    off = {n: int(np.median(rx[nd == n] - ts[nd == n])) for n in nodes}
    bufs, last_slot, last_arr, per = {}, {}, {}, {n: {} for n in NODES}
    last_tick, X, fill, T = 0, [], [], []
    for ms, n, t, f in zip(rx.tolist(), nd.tolist(), ts.tolist(), raw):
        if n not in NODES:
            continue
        slot = grid_slot(ms, 10) if mode != "board" else (t + off[n] + 5) // 10
        if mode == "runtime":
            b = bufs.setdefault(n, deque(maxlen=100))
            place_m1_grid_frame(b, f, slot, last_slot.get(n))
            last_slot[n] = slot
        last_arr[n] = ms
        per[n].setdefault(slot, f) if mode == "loader" else per[n].__setitem__(slot, f)
        if ms - last_tick < TICK_MS:
            continue
        if mode == "runtime":
            if not (m1_window_ready(NODES, bufs, max_nodes=5, window_frames=100)
                    and m1_tail_ready(NODES, last_arr, ms, max_age_ms=15)):
                continue
            x = build_m1_input(NODES, bufs, max_nodes=5, window_frames=100)
        else:
            if any(len(per[k]) < 100 + LAG_SLOTS for k in NODES):
                continue
            end = (ms + 5) // 10 - LAG_SLOTS if mode == "loader" else max(max(per[k]) for k in NODES) - LAG_SLOTS
            x = np.zeros((1, 5, 64, 100), np.float32)
            for k in NODES:
                for j, s in enumerate(range(end - 99, end + 1)):
                    v = per[k].get(s)
                    if v is not None:
                        x[0, k - 1, :, j] = v
        X.append(x)
        fill.append(float(np.mean(np.abs(x[0, :3]).max(1) > 0)))
        T.append(ms)
        last_tick = ms
    return (np.concatenate(X) if X else np.zeros((0, 5, 64, 100), np.float32)), np.array(fill), np.array(T, np.int64)


def score(sess, X):
    name = sess.get_inputs()[0].name
    if not len(X):
        return np.zeros(0)
    return np.concatenate([sess.run(None, {name: X[i:i + 64]})[0].reshape(-1) for i in range(0, len(X), 64)])


def events(scores):
    """5회 중 3회 판정 시작 수, 90 s 억제 경보 수."""
    votes, det = deque(maxlen=N), []
    for s in scores:
        votes.append(s >= THR)
        det.append(len(votes) == N and sum(votes) >= K)
    rises = sum(1 for i, d in enumerate(det) if d and (i == 0 or not det[i - 1]))
    alerts, last = 0, -1e9
    for i, d in enumerate(det):
        if d and i * TICK_MS / 1000 - last >= COOLDOWN_S:
            alerts, last = alerts + 1, i * TICK_MS / 1000
    return rises, alerts, int(sum(det))


def sensitivity(sess, X, rs):
    """모델이 시간 변화·노드·0 채움 중 무엇에 반응하는지. 기준 창은 무작위 150개."""
    Xs = X[rs.choice(len(X), min(150, len(X)), replace=False)]
    base = score(sess, Xs)
    tests = {"time_shuffle": Xs[:, :, :, rs.permutation(100)],
             "time_mean_static": np.repeat(Xs.mean(3, keepdims=True), 100, 3),
             "zero_last10": np.concatenate([Xs[..., :90], np.zeros_like(Xs[..., 90:])], 3),
             "zeros": np.zeros_like(Xs[:1])}
    for n in NODES:
        z = Xs.copy(); z[:, n - 1] = 0; tests[f"zero_node{n}"] = z
    for frac in (0.1, 0.25, 0.5):
        mask = (rs.rand(len(Xs), 1, 1, 100) < frac) & (np.arange(5)[None, :, None, None] < 3)
        tests[f"zero_random_{frac}"] = np.where(mask, 0, Xs)
    out = {"base": [round(float(np.median(base)), 3), round(float((base >= THR).mean()), 3)]}
    for k, v in tests.items():
        s = score(sess, v)
        out[k] = [round(float(np.median(s)), 3), round(float((s >= THR).mean()), 3)]
    return out  # [점수 중앙, 0.80 초과 비율]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--model", default="volumes/models/m1_wifi_pose_onnx")
    ap.add_argument("--modes", default="runtime,loader,board")
    ap.add_argument("--sens", action="store_true", help="모델 민감도(파일마다 board 창 150개)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    path = args.model if args.model.endswith(".onnx") else os.path.join(args.model, "m1_wifi_pose.onnx")
    sess = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
    modes = args.modes.split(",")
    rs = np.random.RandomState(0)
    report = {"model": path, "files": {}, "total": {}}
    acc = {m: {"scores": [], "fill": [], "rises": 0, "alerts": 0, "det": 0, "minutes": 0.0} for m in modes}
    for f in args.files:
        data = load(f)
        rec = {"contract": contract(*data), "modes": {}}
        for m in modes:
            X, fill, _ = assemble(*data, m)
            s = score(sess, X)
            rises, alerts, det = events(s)
            minutes = len(s) * TICK_MS / 60000
            rec["modes"][m] = {"windows": len(s), "fill_median": round(float(np.median(fill)), 3) if len(fill) else None,
                               "score_median": round(float(np.median(s)), 3) if len(s) else None,
                               "fire80": round(float((s >= THR).mean()), 3) if len(s) else None,
                               "rises_per_h": round(rises / minutes * 60, 1) if minutes else None}
            a = acc[m]; a["scores"].append(s); a["fill"].append(fill)
            a["rises"] += rises; a["alerts"] += alerts; a["det"] += det; a["minutes"] += minutes
            if args.sens and m == "board" and len(X):
                rec["sensitivity"] = sensitivity(sess, X, rs)
        report["files"][os.path.basename(f)] = rec
        print(os.path.basename(f), json.dumps(rec["modes"], ensure_ascii=False), flush=True)
    for m, a in acc.items():
        s = np.concatenate(a["scores"]); fl = np.concatenate(a["fill"])
        report["total"][m] = {"minutes": round(a["minutes"], 1), "windows": len(s),
                              "fill_median": round(float(np.median(fl)), 3), "score_median": round(float(np.median(s)), 3),
                              "fire80": round(float((s >= THR).mean()), 3), "fire90": round(float((s >= 0.90).mean()), 3),
                              "detected_ratio": round(a["det"] / len(s), 3),
                              "rises_per_h": round(a["rises"] / a["minutes"] * 60, 1),
                              "alerts_per_h_90s": round(a["alerts"] / a["minutes"] * 60, 1)}
        print("TOTAL", m, json.dumps(report["total"][m], ensure_ascii=False))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
