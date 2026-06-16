"""
노드별 분리 CSI 재분석 — 1노드 구간 vs 2노드 구간 비교
"""
import csv, glob, collections, os
import numpy as np

os.chdir(r"C:\rp5")
files = sorted(glob.glob("data/csi/csi_*.csv"))

resp_cols  = [f"resp_{i}"  for i in range(64)]
heart_cols = [f"heart_{i}" for i in range(64)]
raw_cols   = [f"raw_{i}"   for i in range(64)]

# 유효 채널 (이전 분석에서 확인)
zero_chs = {0,1,2,3,4,5,32,59,60,61,62,63}
active = np.array([i not in zero_chs for i in range(64)])

FS = 100.0

def dominant_freq(sig, f_min, f_max):
    if len(sig) < 60:
        return 0.0, 0.0
    fft = np.abs(np.fft.rfft(sig - sig.mean()))
    freqs = np.fft.rfftfreq(len(sig), 1/FS)
    m = (freqs >= f_min) & (freqs <= f_max)
    if not m.any():
        return 0.0, 0.0
    pw = fft[m]**2
    pf = freqs[m][np.argmax(pw)]
    snr = pw.max() / (pw.sum() - pw.max() + 1e-12)
    return pf, snr

# ── 파일별 노드 분리 분석 ─────────────────────────────────────
print("[파일별 노드별 FFT 생체신호 추정]")
print(f"{'시각':6s} {'노드':4s} {'N행':5s} {'RR-bpm':7s} {'RR-SNR':7s} {'HR-bpm':7s} {'HR-SNR':7s} {'r_std':9s} {'h_std':9s} {'페이즈':8s}")

results_n1_1node = []
results_n1_2node = []
results_n2_2node = []

for fp in files:
    label = fp.split("_")[-1].replace(".csv", "")
    # 노드별로 분리
    node_rows = collections.defaultdict(list)
    with open(fp, newline="") as f:
        for row in csv.DictReader(f):
            node_rows[row["node_id"]].append(row)

    n_nodes = len(node_rows)
    phase = f"{'1노드' if n_nodes == 1 else '2노드'}"

    for nid in sorted(node_rows.keys()):
        rows = node_rows[nid]
        if len(rows) < 60:
            continue
        # ts_ms 기준 정렬 (인터리브 해제)
        rows.sort(key=lambda r: int(r["ts_ms"]))

        R = np.array([[float(r[c]) for c in resp_cols]  for r in rows], dtype=np.float32)
        H = np.array([[float(r[c]) for c in heart_cols] for r in rows], dtype=np.float32)

        r_ts = R[:, active].mean(axis=1)
        h_ts = H[:, active].mean(axis=1)

        rf, rsnr = dominant_freq(r_ts, 0.1, 0.6)
        hf, hsnr = dominant_freq(h_ts, 0.8, 3.0)
        rstd = r_ts.std()
        hstd = h_ts.std()

        row_dict = {
            "label": label, "nid": nid, "n": len(rows),
            "rr": rf*60, "rsnr": rsnr,
            "hr": hf*60, "hsnr": hsnr,
            "rstd": rstd, "hstd": hstd
        }

        print(f"{label:6s} {nid:4s} {len(rows):5d} {rf*60:7.1f} {rsnr:7.3f} {hf*60:7.1f} {hsnr:7.3f} {rstd:9.6f} {hstd:9.6f} {phase}")

        if n_nodes == 1 and nid == "1":
            results_n1_1node.append(row_dict)
        elif n_nodes == 2 and nid == "1":
            results_n1_2node.append(row_dict)
        elif n_nodes == 2 and nid == "2":
            results_n2_2node.append(row_dict)

# ── 구간별 요약 ───────────────────────────────────────────────
print()
print("=" * 60)
print("[구간별 요약]")

def summarize(label, data):
    if not data:
        return
    rr = [d["rr"] for d in data]
    hr = [d["hr"] for d in data]
    rstd = [d["rstd"] for d in data]
    hstd = [d["hstd"] for d in data]
    print(f"\n  {label} (n={len(data)}분)")
    print(f"    resp_std   mean={np.mean(rstd)*1000:.3f}e-3  max={np.max(rstd)*1000:.3f}e-3")
    print(f"    heart_std  mean={np.mean(hstd)*1000:.3f}e-3  max={np.max(hstd)*1000:.3f}e-3")
    print(f"    RR 추정    mean={np.mean(rr):.1f} BPM  std={np.std(rr):.1f}")
    print(f"    HR 추정    mean={np.mean(hr):.1f} BPM  std={np.std(hr):.1f}")

summarize("1노드 구간 - node1", results_n1_1node)
summarize("2노드 구간 - node1", results_n1_2node)
summarize("2노드 구간 - node2", results_n2_2node)

# ── 노드1 전후 비교 (같은 노드, 다른 시기) ─────────────────
print()
print("=" * 60)
print("[node1: 1노드 구간 vs 2노드 구간 resp_std 비교]")
print("  (같은 노드인데 2노드 추가 후 신호가 달라졌는지?)")
n1_1p_rstd = np.mean([d["rstd"] for d in results_n1_1node]) if results_n1_1node else 0
n1_2p_rstd = np.mean([d["rstd"] for d in results_n1_2node]) if results_n1_2node else 0
print(f"  1노드 구간 node1 resp_std: {n1_1p_rstd*1000:.4f}e-3")
print(f"  2노드 구간 node1 resp_std: {n1_2p_rstd*1000:.4f}e-3")
ratio = n1_2p_rstd / n1_1p_rstd if n1_1p_rstd > 0 else 0
print(f"  비율: {ratio:.3f}x  ({'감소' if ratio < 1 else '증가'} — {'노드 추가 영향 있음' if abs(ratio-1) > 0.1 else '큰 차이 없음'})")

# ── node1 vs node2 동시 구간 비교 ────────────────────────────
print()
print("[2노드 구간 — node1 vs node2 비교 (같은 시간대)]")
n1_2p = {d["label"]: d for d in results_n1_2node}
n2_2p = {d["label"]: d for d in results_n2_2node}
common = sorted(set(n1_2p.keys()) & set(n2_2p.keys()))
print(f"{'시각':6s} {'n1_rstd':10s} {'n2_rstd':10s} {'n1_RR':7s} {'n2_RR':7s} {'rstd_ratio':10s}")
for lbl in common[:20]:  # 처음 20분만
    d1, d2 = n1_2p[lbl], n2_2p[lbl]
    ratio = d2["rstd"] / d1["rstd"] if d1["rstd"] > 0 else 0
    print(f"{lbl:6s} {d1['rstd']*1000:10.4f} {d2['rstd']*1000:10.4f} {d1['rr']:7.1f} {d2['rr']:7.1f} {ratio:10.3f}x")

if common:
    r_corr = np.corrcoef(
        [n1_2p[l]["rstd"] for l in common],
        [n2_2p[l]["rstd"] for l in common]
    )[0,1]
    rr_corr = np.corrcoef(
        [n1_2p[l]["rr"] for l in common],
        [n2_2p[l]["rr"] for l in common]
    )[0,1]
    print(f"\n  node1-node2 resp_std 상관: r={r_corr:.4f}")
    print(f"  node1-node2 RR 추정 상관:  r={rr_corr:.4f}")
