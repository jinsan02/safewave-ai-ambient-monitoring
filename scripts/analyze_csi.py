"""
CSI 데이터 분석 스크립트 — data/csi/*.csv (ESP32 실측, 77분)
"""
import os, glob, csv
import numpy as np

os.chdir(r"C:\rp5")
files = sorted(glob.glob("data/csi/csi_*.csv"))
print(f"파일 수: {len(files)}")
print(f"시간 범위: 17:08 ~ 18:24 (2026-06-11)")

with open(files[0], newline='') as f:
    rows0 = list(csv.DictReader(f))
print(f"파일당 행 수(1분): {len(rows0)}")

ts0 = [int(r['ts_ms']) for r in rows0]
dt0 = np.diff(ts0)
print(f"ts_ms 간격: min={dt0.min()}ms max={dt0.max()}ms mean={dt0.mean():.2f}ms => {1000/dt0.mean():.1f}Hz")
print(f"node_id: {set(r['node_id'] for r in rows0)}")

# 전체 로드
all_rows = []
for fp in files:
    with open(fp, newline='') as f:
        all_rows.extend(list(csv.DictReader(f)))
print(f"\n전체 패킷 수: {len(all_rows):,}")

raw_cols   = [f"raw_{i}"   for i in range(64)]
resp_cols  = [f"resp_{i}"  for i in range(64)]
heart_cols = [f"heart_{i}" for i in range(64)]

raw   = np.array([[float(r[c]) for c in raw_cols]   for r in all_rows], dtype=np.float32)
resp  = np.array([[float(r[c]) for c in resp_cols]  for r in all_rows], dtype=np.float32)
heart = np.array([[float(r[c]) for c in heart_cols] for r in all_rows], dtype=np.float32)

# 제로 채널
raw_zero = (raw == 0).all(axis=0)
active   = ~raw_zero
print(f"\n[채널 구조]")
print(f"  항상 0인 서브캐리어(raw): {np.where(raw_zero)[0].tolist()}")
print(f"  유효 서브캐리어: {active.sum()}개 / 64")

# 진폭 통계
print(f"\n[진폭 통계]")
print(f"  raw(활성)   mean={raw[:,active].mean():.4f}  std={raw[:,active].std():.4f}  p05={np.percentile(raw[:,active], 5):.4f}  p95={np.percentile(raw[:,active], 95):.4f}")
print(f"  resp(전체)  mean={resp.mean():.6f}  std={resp.std():.6f}  |max|={np.abs(resp).max():.6f}")
print(f"  heart(전체) mean={heart.mean():.6f}  std={heart.std():.6f}  |max|={np.abs(heart).max():.6f}")

# 서브캐리어 에너지
resp_e  = (resp**2).mean(axis=0)
heart_e = (heart**2).mean(axis=0)
top5_r = np.argsort(resp_e)[::-1][:5].tolist()
top5_h = np.argsort(heart_e)[::-1][:5].tolist()
print(f"\n[resp 에너지 TOP5 채널]: {top5_r}")
for idx in top5_r:
    print(f"  ch{idx:3d}: {resp_e[idx]:.8f}")
print(f"[heart 에너지 TOP5 채널]: {top5_h}")
for idx in top5_h:
    print(f"  ch{idx:3d}: {heart_e[idx]:.8f}")

# raw 시간 분산
active_idx = np.where(active)[0]
raw_tstd = raw[:,active].std(axis=0)
print(f"\n[raw 채널 시간분산] mean={raw_tstd.mean():.4f} std={raw_tstd.std():.4f}")
print(f"  가장 안정적: ch{active_idx[raw_tstd.argmin()]} (std={raw_tstd.min():.4f})")
print(f"  가장 가변적: ch{active_idx[raw_tstd.argmax()]} (std={raw_tstd.max():.4f})")

# FFT 생체신호 추정 (분당)
FS = 100.0
def dominant_freq(sig, f_min, f_max):
    fft = np.abs(np.fft.rfft(sig - sig.mean()))
    freqs = np.fft.rfftfreq(len(sig), 1/FS)
    m = (freqs >= f_min) & (freqs <= f_max)
    if not m.any():
        return 0.0, 0.0
    pw = fft[m]**2
    pf = freqs[m][np.argmax(pw)]
    snr = pw.max() / (pw.sum() - pw.max() + 1e-12)
    return pf, snr

print(f"\n[분당 생체신호 FFT 추정]")
print(f"{'시각':6s}  {'호흡Hz':>7s}  {'RR-bpm':>7s}  {'rSNR':>6s}  {'심박Hz':>7s}  {'HR-bpm':>7s}  {'hSNR':>6s}  {'r_std':>9s}  {'h_std':>9s}")
stats = []
for fp in files:
    t = fp.split('_')[-1].replace('.csv','')
    with open(fp, newline='') as f:
        rm = list(csv.DictReader(f))
    if len(rm) < 60:
        continue
    R = np.array([[float(r[c]) for c in resp_cols]  for r in rm], dtype=np.float32)[:,active].mean(axis=1)
    H = np.array([[float(r[c]) for c in heart_cols] for r in rm], dtype=np.float32)[:,active].mean(axis=1)
    rf, rsnr = dominant_freq(R, 0.1, 0.6)
    hf, hsnr = dominant_freq(H, 0.8, 3.0)
    stats.append({'t':t,'rf':rf,'hf':hf,'rsnr':rsnr,'hsnr':hsnr,'rstd':R.std(),'hstd':H.std()})
    print(f"{t:6s}  {rf:7.4f}  {rf*60:7.1f}  {rsnr:6.2f}  {hf:7.4f}  {hf*60:7.1f}  {hsnr:6.2f}  {R.std():9.6f}  {H.std():9.6f}")

rbpms = [s['rf']*60 for s in stats if s['rsnr'] > 1]
hbpms = [s['hf']*60 for s in stats if s['hsnr'] > 1]
print(f"\n[전체 요약]")
print(f"  호흡: {np.mean(rbpms):.1f} +/- {np.std(rbpms):.1f} BPM  (신뢰 {len(rbpms)}/{len(stats)}분)")
print(f"  심박: {np.mean(hbpms):.1f} +/- {np.std(hbpms):.1f} BPM  (신뢰 {len(hbpms)}/{len(stats)}분)")

# raw 채널 간 상관
raw_ch = raw[:,active]
corr_mat = np.corrcoef(raw_ch.T)
upper = corr_mat[np.triu_indices(corr_mat.shape[0], k=1)]
print(f"\n[raw 채널 간 Pearson 상관]")
print(f"  mean={upper.mean():.4f}  std={upper.std():.4f}  min={upper.min():.4f}  max={upper.max():.4f}")

# resp/heart 채널 간 상관 (활성 채널)
resp_corr  = np.corrcoef(resp[:,active].T)
heart_corr = np.corrcoef(heart[:,active].T)
resp_upper  = resp_corr[np.triu_indices(resp_corr.shape[0], k=1)]
heart_upper = heart_corr[np.triu_indices(heart_corr.shape[0], k=1)]
print(f"[resp 채널 간 상관]  mean={resp_upper.mean():.4f}  std={resp_upper.std():.4f}")
print(f"[heart 채널 간 상관] mean={heart_upper.mean():.4f}  std={heart_upper.std():.4f}")

# 전체 시간축 활동성 (10분 구간)
print(f"\n[10분 구간별 resp 활동성]")
all_ts = [int(r['ts_ms']) for r in all_rows]
t0 = all_ts[0]
for seg in range(8):
    start_ms = t0 + seg*600_000
    end_ms   = start_ms + 600_000
    mask = [(t >= start_ms and t < end_ms) for t in all_ts]
    seg_resp = resp[mask][:,active].mean(axis=1) if sum(mask) > 0 else np.array([])
    if len(seg_resp) == 0:
        continue
    rf, rsnr = dominant_freq(seg_resp, 0.1, 0.6)
    print(f"  구간{seg+1} ({seg*10}~{seg*10+10}분): n={sum(mask):5d}  resp_std={seg_resp.std():.6f}  RR={rf*60:.1f}BPM  SNR={rsnr:.2f}")
