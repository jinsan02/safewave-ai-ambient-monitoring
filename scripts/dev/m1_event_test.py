#!/usr/bin/env python3
"""사람 움직임 시험(5분 영상 시험) 분석 — CSI 기록 + 동작 시각표 → 동작별 M1·급변 신호·판정 표와 그림.

무엇을 재나
  1) 데이터 품질: 프레임 혼재·반복·가드·최댓값·장치 시각(m1_false_alarm_analysis.contract)
  2) 지금 M1: ai/main.py 그대로 재현한 1초 창 점수, 5회 중 3회 판정
  3) 급변 신호(재학습 없음): 노드별로 새 프레임만, 두 프레임 종류를 나눠 **종류마다 30 s 지수 평균 기준선**을 빼고
     잔차 크기의 1초 평균 → 기준(빈 방 구간 중앙값) 대비 배수. 비교용으로 '다수 종류만 남긴 연속 차이'도 같이 낸다.
  4) 판정 후보: 급변 배수 ≥ R인 노드 ≥ 2개(즉시), 5초 평균 + 히스테리시스, M1 3/5 + 직전 2 s 급변
  5) (선택) validation_recorder 출력이 있으면 M3 환경음·M4 전사·규칙 경보를 동작 구간에 붙인다

입력
  기록: csi_excel_logger(.xlsx) / csi_csv_logger(.csv) / 변환 npz — 열 stream_id, node_id, ts_ms, raw_0..63
  시각표 CSV: start,end,label  (시각은 'HH:MM:SS'(기록한 날 한국 시각) 또는 기록 시작부터 초)
    label에 'empty'(빈 방)가 들어간 구간을 급변 기준으로 쓴다. 없으면 기록 처음 60 s.
    급변 기준선은 처음 30 s 예열하므로, 기록은 시험 시작 1분 이상 전에 켠다.

사용
  python scripts/dev/m1_event_test.py data/csi/excel/csi_*_test1.xlsx --timeline reports/laptop/m1_test/timeline_test1.csv \
      --validation data/validation/<label> --out reports/laptop/m1_test/test1
"""
import argparse
import datetime as dt
import glob
import json
import os
import sys
from collections import deque

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from m1_false_alarm_analysis import assemble, contract, score  # noqa: E402
import onnxruntime as ort  # noqa: E402

NODES = (1, 2, 3)          # 지금 M1이 쓰는 노드. 급변 신호는 기록에 있는 노드 전부(1~5)
SC = slice(6, 59)          # 가드 제외 부반송파
KST = dt.timezone(dt.timedelta(hours=9))
STEP_MS, WIN_MS = 200, 1000


def load(paths):
    parts = []
    for p in paths:
        if p.endswith(".npz"):
            z = np.load(p)
            parts.append(pd.DataFrame({"rx": z["rx"], "node": z["node"], "ts": z["ts"]}).assign(_raw=list(z["raw"])))
            continue
        df = pd.read_excel(p, sheet_name="csi") if p.endswith(".xlsx") else pd.read_csv(p)
        rx = df.stream_id.astype(str).str.split("-").str[0].astype(np.int64)
        parts.append(pd.DataFrame({"rx": rx, "node": df.node_id.astype(int), "ts": df.ts_ms.astype(np.int64)})
                     .assign(_raw=list(df[[f"raw_{i}" for i in range(64)]].to_numpy(np.float32))))
    d = pd.concat(parts, ignore_index=True).sort_values("rx", kind="stable")
    return d.rx.to_numpy(np.int64), d.node.to_numpy(int), d.ts.to_numpy(np.int64), np.stack(d._raw.to_numpy())


def load_timeline(path, rx0):
    tl = pd.read_csv(path)
    day = dt.datetime.fromtimestamp(rx0 / 1000, KST).date()

    def to_ms(v):
        v = str(v).strip()
        if ":" in v:
            hh, mm, *ss = [int(x) for x in v.split(":")]
            t = dt.datetime(day.year, day.month, day.day, hh, mm, ss[0] if ss else 0, tzinfo=KST)
            return int(t.timestamp() * 1000)
        return rx0 + int(float(v) * 1000)
    tl["start_ms"] = tl.start.map(to_ms)
    tl["end_ms"] = tl.end.map(to_ms)
    return tl


def two_types(xn):
    s = xn[:: max(1, len(xn) // 20000)]
    c0 = s[0]; c1 = s[int(np.argmin(s @ c0))]
    for _ in range(30):
        lab = (s @ c1) > (s @ c0)
        if lab.all() or (~lab).all():
            break
        c0 = s[~lab].mean(0); c0 /= np.linalg.norm(c0); c1 = s[lab].mean(0); c1 /= np.linalg.norm(c1)
    lab = (xn @ c1) > (xn @ c0)
    return lab if lab.mean() <= 0.5 else ~lab          # True = 소수 종류


def win_mean(val, t, grid):
    ok = ~np.isnan(val)
    cs = np.r_[0, np.cumsum(np.where(ok, val, 0))]; cn = np.r_[0, np.cumsum(ok)]
    lo = np.searchsorted(t, grid - WIN_MS); hi = np.searchsorted(t, grid)
    cnt = cn[hi] - cn[lo]
    return np.where(cnt > 5, (cs[hi] - cs[lo]) / np.maximum(cnt, 1), np.nan)


def motion(rx, nd, ts, raw, grid, nodes, ema_s=30.0):
    """노드별 (종류별 기준선 잔차, 다수 종류 연속 차이) 1초 평균."""
    out = {}
    for n in nodes:
        m = nd == n
        x = raw[m][:, SC].astype(np.float64); t = ts[m] + int(np.median(rx[m] - ts[m]))
        o = np.argsort(t, kind="stable"); x, t = x[o], t[o]
        fresh = np.r_[True, np.abs(np.diff(x, axis=0)).max(1) > 0]
        x, t = x[fresh], t[fresh]
        xn = x / (np.linalg.norm(x, axis=1, keepdims=True) + 1e-9)
        minor = two_types(xn)
        fresh_hz = len(x) / max((t[-1] - t[0]) / 1000, 1)
        alpha = 1.0 / (ema_s * fresh_hz / 2)
        base = np.zeros_like(x)
        for k in (False, True):
            idx = np.flatnonzero(minor == k)
            if len(idx):
                base[idx] = pd.DataFrame(x[idx]).ewm(alpha=alpha, adjust=False).mean().to_numpy()
        resid = np.abs(x - base).mean(1)
        resid[t < t[0] + ema_s * 1000] = np.nan       # 기준선 예열 구간(처음 30 s)은 쓰지 않는다
        xm, tm = x[~minor], t[~minor]
        dmaj = np.r_[0, np.abs(np.diff(xm, axis=0)).mean(1)]
        ok = np.r_[True, np.diff(tm) <= 60]
        out[n] = {"resid": win_mean(resid, t, grid), "diff_major": win_mean(np.where(ok, dmaj, 0), tm, grid),
                  "minor_ratio": float(minor.mean()), "fresh_hz": round(fresh_hz, 1)}
    return out


def hysteresis(v, on, off):
    st, out = False, []
    for x in v:
        if not np.isnan(x):
            st = (x >= on) if not st else (x >= off)
        out.append(st)
    return np.array(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--timeline", required=True)
    ap.add_argument("--validation", default=None, help="validation_recorder 출력 폴더(선택)")
    ap.add_argument("--model", default="volumes/models/m1_wifi_pose_onnx/m1_wifi_pose.onnx")
    ap.add_argument("--ratio", type=float, default=2.0, help="급변 배수 기준(빈 방 113분 최대 1.65)")
    ap.add_argument("--out", default="reports/laptop/m1_test/run")
    a = ap.parse_args()
    files = sorted(sum((glob.glob(f) for f in a.files), []))
    os.makedirs(a.out, exist_ok=True)
    rx, nd, ts, raw = load(files)
    tl = load_timeline(a.timeline, int(rx[0]))
    rep = {"files": [os.path.basename(f) for f in files], "minutes": round((rx[-1] - rx[0]) / 60000, 2),
           "contract": contract(rx, nd, ts, raw)}

    # 2) M1 재현
    sess = ort.InferenceSession(a.model, providers=["CPUExecutionProvider"])
    X, fill, T = assemble(rx, nd, ts, raw, "runtime")
    s1 = score(sess, X)
    votes, det1 = deque(maxlen=5), []
    for v in s1:
        votes.append(v >= 0.8); det1.append(len(votes) == 5 and sum(votes) >= 3)
    det1 = np.array(det1)

    # 3) 급변 신호(200 ms 격자)
    grid = np.arange(int(rx[0]) + WIN_MS, int(rx[-1]), STEP_MS)
    mnodes = tuple(int(n) for n in np.unique(nd) if (nd == n).sum() >= 1000)
    mo = motion(rx, nd, ts, raw, grid, mnodes)
    empty = np.zeros(len(grid), bool)
    for _, r in tl[tl.label.str.contains("empty", case=False)].iterrows():
        empty |= (grid >= r.start_ms) & (grid < r.end_ms)
    if empty.sum() < 100:
        empty = grid < grid[0] + 60000
        rep["baseline"] = "기록 처음 60 s(시각표에 empty 구간 없음)"
    else:
        rep["baseline"] = f"empty 구간 {empty.sum() * STEP_MS / 1000:.0f} s"
    ratio = {}
    for kind in ("resid", "diff_major"):
        ratio[kind] = np.column_stack([mo[n][kind] / np.nanmedian(mo[n][kind][empty]) for n in mnodes])
    rep["motion"] = {n: {"fresh_hz": mo[n]["fresh_hz"], "minor_ratio": round(mo[n]["minor_ratio"], 3),
                         "empty_p999_resid": round(float(np.nanpercentile(ratio["resid"][empty, i], 99.9)), 2)}
                     for i, n in enumerate(mnodes)}

    # 4) 판정 후보
    r = ratio["resid"]
    nodes_hi = np.nansum(r >= a.ratio, 1)
    cand = {"급변 ≥2노드(즉시)": nodes_hi >= 2,
            "급변 5초평균 히스테리시스": hysteresis(pd.Series(np.nanmax(r, 1)).rolling(25, min_periods=5).mean().to_numpy(),
                                          a.ratio, a.ratio * 0.75)}
    idx = np.clip(np.searchsorted(grid, T), 0, len(grid) - 1)
    recent = pd.Series((nodes_hi >= 2).astype(float)).rolling(10, min_periods=1).max().to_numpy() > 0
    cand["M1 3/5 + 직전 2 s 급변"] = np.zeros(len(grid), bool)
    hit = det1 & recent[idx]
    cand["M1 3/5 + 직전 2 s 급변"][idx[hit]] = True
    m1_on_grid = np.zeros(len(grid), bool); m1_on_grid[idx[det1]] = True
    cand["M1 3/5 단독(현재)"] = m1_on_grid

    # 5) (선택) 다른 모델 기록
    val = {}
    if a.validation:
        for name in ("m3_env", "m4_stt", "gate", "m5_emergency"):
            p = os.path.join(a.validation, f"{name}.csv")
            if os.path.exists(p):
                val[name] = pd.read_csv(p, encoding="utf-8-sig")

    rows = []
    for _, seg in tl.iterrows():
        g = (grid >= seg.start_ms) & (grid < seg.end_ms)
        w = (T >= seg.start_ms) & (T < seg.end_ms)
        row = {"label": seg.label, "start": seg.start, "end": seg.end, "sec": round((seg.end_ms - seg.start_ms) / 1000, 1),
               "m1_score_med": round(float(np.median(s1[w])), 3) if w.any() else None,
               "m1_fire80": round(float((s1[w] >= .8).mean()), 3) if w.any() else None,
               "m1_3of5_any": bool(det1[w].any()) if w.any() else None}
        for i, n in enumerate(mnodes):
            row[f"resid_x_max_n{n}"] = round(float(np.nanmax(r[g, i])), 2) if g.any() else None
            row[f"diffmaj_x_max_n{n}"] = round(float(np.nanmax(ratio["diff_major"][g, i])), 2) if g.any() else None
        row["nodes_ge_R_max"] = int(nodes_hi[g].max()) if g.any() else None
        for k, v in cand.items():
            on = v[g]
            row[k] = bool(on.any()) if g.any() else None
            if g.any() and on.any():
                row[k + " 지연s"] = round((grid[g][np.argmax(on)] - seg.start_ms) / 1000, 1)
        if "m3_env" in val:
            m3 = val["m3_env"]; mm = (m3.stream_ms >= seg.start_ms) & (m3.stream_ms < seg.end_ms)
            row["m3_labels"] = m3.loc[mm, "env_sound_label"].value_counts().to_dict() if mm.any() else {}
        if "m4_stt" in val:
            m4 = val["m4_stt"]; mm = (m4.stream_ms >= seg.start_ms) & (m4.stream_ms < seg.end_ms)
            row["m4_text"] = " | ".join(m4.loc[mm, "transcript_ko"].dropna().astype(str))[:120]
            if "emergency_phrase_detected" in m4:
                row["m4_emergency"] = bool(m4.loc[mm, "emergency_phrase_detected"].astype(str).eq("True").any())
        if "m5_emergency" in val:
            m5 = val["m5_emergency"]; mm = (m5.stream_ms >= seg.start_ms) & (m5.stream_ms < seg.end_ms + 30000)
            row["alerts(+30s)"] = m5.loc[mm, "slm_mode"].astype(str).value_counts().to_dict() if mm.any() else {}
        rows.append(row)
    per = pd.DataFrame(rows)
    per.to_csv(os.path.join(a.out, "per_action.csv"), index=False, encoding="utf-8-sig")
    # 빈 방 구간 오탐(후보별, 시간당 켜짐 횟수)
    rep["empty_false_on_per_h"] = {}
    for k, v in cand.items():
        e = v & empty
        rises = int(e[0] + np.sum(e[1:] & ~e[:-1]))
        rep["empty_false_on_per_h"][k] = round(rises / max(empty.sum() * STEP_MS / 3.6e6, 1e-9), 1)
    json.dump(rep, open(os.path.join(a.out, "summary.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams["font.family"] = "Malgun Gothic"; plt.rcParams["axes.unicode_minus"] = False
    tm = lambda t: (np.asarray(t) - rx[0]) / 1000
    fig, ax = plt.subplots(3, 1, figsize=(15, 9), sharex=True)
    ax[0].plot(tm(T), s1, lw=.6); ax[0].axhline(.8, color="r", lw=.8); ax[0].set_ylabel("M1 점수"); ax[0].set_ylim(0, 1)
    ax[0].plot(tm(T)[det1], np.full(det1.sum(), .97), "r|", ms=6, label="M1 3/5")
    for i, n in enumerate(mnodes):
        ax[1].plot(tm(grid), r[:, i], lw=.7, label=f"노드 {n}")
    ax[1].axhline(a.ratio, color="r", lw=.8); ax[1].set_ylabel("급변 배수(종류별 기준선)"); ax[1].legend(loc="upper right")
    for j, (k, v) in enumerate(cand.items()):
        ax[2].plot(tm(grid), v * (j + 1), lw=1, label=k)
    ax[2].set_yticks([]); ax[2].legend(loc="upper right", fontsize=8); ax[2].set_xlabel("기록 시작부터 초")
    for _, seg in tl.iterrows():
        for x in ax:
            x.axvspan(tm(seg.start_ms), tm(seg.end_ms), alpha=.08, color="C1" if "empty" not in seg.label else "C2")
        ax[0].text(tm(seg.start_ms), 1.02, seg.label, fontsize=8, rotation=30)
    fig.tight_layout(); fig.savefig(os.path.join(a.out, "timeline.png"), dpi=110)
    with pd.option_context("display.max_columns", 50, "display.width", 250):
        print(per.to_string(index=False))
    print(json.dumps({k: rep[k] for k in ("baseline", "motion", "empty_false_on_per_h")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
