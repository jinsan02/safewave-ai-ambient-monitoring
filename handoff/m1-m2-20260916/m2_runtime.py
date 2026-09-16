#!/usr/bin/env python
"""M2 스펙트럼 추정기 런타임 — 전처리 → onnxruntime → 6 dB 게이트 → dict.

이 파일이 **입출력 계약의 단일 기준**이다. 상수와 전처리가 여기 있고,
scripts/export_m2_spectrum.py 가 이걸 import 해서 ONNX 를 만든다. 두 곳이 어긋날 수 없다.

torch 를 쓰지 않는다. numpy + onnxruntime 만 있으면 돈다.

**먼저 읽을 것: docs/M2_HANDOFF.md 의 "유의사항".**
현재 수집 데이터에서는 호흡·심박이 검출되지 않았다. 여기서 나오는 bpm 은
통합 테스트·배선 확인용이고 생체신호로 믿으면 안 된다. 그래서 반환 dict 에
`trustworthy: False` 를 명시적으로 넣는다.

사용 예
  from m2_runtime import M2SpectrumRuntime
  rt = M2SpectrumRuntime()
  out = rt.infer(frames)        # frames: (3, 3000, 64) float, 노드×시간×서브캐리어

CLI
  python scripts/m2_runtime.py --demo
  python scripts/m2_runtime.py --csv <경로> --start-sec 0
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ─────────────────────────────────────────────────────────────────────────────
# 입출력 계약 — 팀 병합 대상. 바꾸면 rp5 배선과 ONNX 가 같이 깨진다.
# ─────────────────────────────────────────────────────────────────────────────
INPUT_NAME = "csi_window"
OUTPUT_NAMES = ("resp_spectrum", "resp_bpm", "resp_snr_db", "heart_bpm", "heart_snr_db")

N_NODES = 3          # 내부안테나 3보드
N_SUB = 64           # 서브캐리어
N_TIME = 3000        # 30초 @ 100 Hz — 계약 확정값
                     # 주의: configs/m2_vital.yaml 의 window_frames(1000) 과 다르다.
                     # 그건 쓰지 않기로 한 LSTM 학습 경로의 값이다 (HANDOFF 3절 참조).
FS_HZ = 100.0

# 가드/널 서브캐리어. 802.11n HT20 은 64개 중 52개만 쓴다.
# 수집 데이터 3노드 전부에서 이 12개의 표준편차가 0 임을 확인했다.
GUARD_SUBCARRIERS = (0, 1, 2, 3, 4, 5, 32, 59, 60, 61, 62, 63)
N_LIVE_SUB = N_SUB - len(GUARD_SUBCARRIERS)   # 52

N_BINS = 151                     # DFT bin 0..150 → 0 ~ 5.0 Hz
BIN_HZ = FS_HZ / N_TIME          # 1/30 Hz ≈ 0.033333 Hz
# 6000/3000 = 2.0 정확. (BIN_HZ * 60.0) 은 1.9999999999999998 이 되므로 이렇게 쓴다.
BPM_PER_BIN = FS_HZ * 60.0 / N_TIME   # 2.0 회/분 — bpm 해상도가 이것이다

# 대역 → bin 구간 [lo, hi). 부동소수 비교를 피하려고 정수로 박는다.
# 네 경계 모두 정확히 정수 bin 에 떨어진다: 0.1→3, 0.6→18, 0.8→24, 3.0→90
RESP_BIN_LO, RESP_BIN_HI = 3, 19     # 0.1-0.6 Hz, 16개 bin
HEART_BIN_LO, HEART_BIN_HI = 24, 91  # 0.8-3.0 Hz, 67개 bin

GATE_DB = 6.0        # configs/m2_vital.yaml snr.gate_db
NORM_EPS = 1e-12

DEFAULT_ONNX = os.path.join(_REPO_ROOT, "models", "m2_spectrum_20260916.onnx")

FREQS_HZ = (np.arange(N_BINS, dtype=np.float32) * BIN_HZ)

_WARNING = (
    "현재 수집 데이터에서 호흡·심박 신호가 검출되지 않았고 대조군이 역전돼 있다. "
    "bpm 값을 생체신호로 쓰지 말 것. 통합 테스트·배선 확인 용도. docs/M2_HANDOFF.md 참조."
)


# ─────────────────────────────────────────────────────────────────────────────
def preprocess_window(frames: np.ndarray) -> np.ndarray:
    """(N_NODES, N_TIME, N_SUB) → (1, N_NODES, N_SUB, N_TIME) float32.

    1) 프레임별 피크 정규화 — ESP 측과 동일하게 각 프레임을 그 프레임의 최댓값으로 나눈다.
    2) 가드 서브캐리어를 0 으로.

    주의: 1) 은 프레임마다 양의 스칼라로 나누는 연산이라, 모든 서브캐리어가 똑같이
    흔들리는 '공통모드' 변조를 정확히 0 으로 만든다. 서브캐리어마다 다르게 실리는
    차동 성분만 살아남는다 (export 스크립트 검증 [3b]/[3c] 참조).
    """
    x = np.asarray(frames, dtype=np.float32)
    if x.shape != (N_NODES, N_TIME, N_SUB):
        raise ValueError(
            f"frames 는 (노드{N_NODES}, 시간{N_TIME}, 서브캐리어{N_SUB}) 여야 한다. 받은 건 {x.shape}"
        )
    x = x.copy()
    peak = np.abs(x).max(axis=2, keepdims=True)          # (3,3000,1)
    x /= np.maximum(peak, NORM_EPS)
    x[:, :, list(GUARD_SUBCARRIERS)] = 0.0
    # (노드, 시간, 서브캐리어) → (배치1, 노드, 서브캐리어, 시간)
    return np.ascontiguousarray(np.transpose(x, (0, 2, 1))[None, ...], dtype=np.float32)


# ─────────────────────────────────────────────────────────────────────────────
class M2SpectrumRuntime:
    """ONNX 세션 한 개를 들고 있는 얇은 래퍼."""

    def __init__(
        self,
        onnx_path: str = DEFAULT_ONNX,
        gate_db: float = GATE_DB,
        providers: Optional[Sequence[str]] = None,
    ) -> None:
        import onnxruntime as ort   # 지연 import — 모듈 로드만으로 ORT 를 요구하지 않는다

        if not os.path.isfile(onnx_path):
            raise FileNotFoundError(
                f"ONNX 가 없다: {onnx_path}\n"
                f"먼저 만들어라: python scripts/export_m2_spectrum.py"
            )
        self.onnx_path = onnx_path
        self.gate_db = float(gate_db)
        self.session = ort.InferenceSession(
            onnx_path, providers=list(providers) if providers else ["CPUExecutionProvider"]
        )

    # ── 노드 선택 ───────────────────────────────────────────────────────
    @staticmethod
    def _select(bpm: np.ndarray, snr: np.ndarray, passed: np.ndarray) -> Dict[str, Any]:
        """게이트를 통과한 노드 중 SNR 이 가장 높은 하나를 고른다.

        노드 평균을 내지 않는다. snr.py 에 기록된 실측대로, 피험자에서 먼 노드는
        움직임·호흡 배음이 대역을 지배해 가짜 피크를 낸다. 평균을 내면 그게 섞인다.
        """
        idx = np.flatnonzero(passed)
        if idx.size == 0:
            return {"node": None, "bpm": None, "snr_db": None, "n_passed": 0}
        best = int(idx[np.argmax(snr[idx])])
        return {
            "node": best,
            "bpm": float(bpm[best]),
            "snr_db": float(snr[best]),
            "n_passed": int(idx.size),
        }

    # ── 추론 ────────────────────────────────────────────────────────────
    def infer(self, frames: np.ndarray) -> Dict[str, Any]:
        """frames (3,3000,64) → 결과 dict. 전처리·게이트 포함."""
        return self.infer_preprocessed(preprocess_window(frames))

    def infer_preprocessed(self, csi_window: np.ndarray) -> Dict[str, Any]:
        """이미 (1,3,64,3000) 로 전처리된 입력을 받는 경로."""
        x = np.asarray(csi_window, dtype=np.float32)
        if x.shape != (1, N_NODES, N_SUB, N_TIME):
            raise ValueError(f"csi_window 는 (1,{N_NODES},{N_SUB},{N_TIME}) 여야 한다. 받은 건 {x.shape}")

        outs = dict(zip(OUTPUT_NAMES, self.session.run(None, {INPUT_NAME: x})))
        resp_pass = outs["resp_snr_db"] >= self.gate_db
        heart_pass = outs["heart_snr_db"] >= self.gate_db

        nodes: List[Dict[str, Any]] = []
        for i in range(N_NODES):
            nodes.append({
                "node": i,
                "resp_bpm": float(outs["resp_bpm"][i]),
                "resp_snr_db": float(outs["resp_snr_db"][i]),
                "resp_pass": bool(resp_pass[i]),
                "heart_bpm": float(outs["heart_bpm"][i]),
                "heart_snr_db": float(outs["heart_snr_db"][i]),
                "heart_pass": bool(heart_pass[i]),
            })

        return {
            # ── 원시 출력 (ONNX 계약 그대로) ──
            "resp_spectrum": outs["resp_spectrum"],        # (3,151) 크기 스펙트럼
            "resp_bpm": outs["resp_bpm"],                  # (3,)
            "resp_snr_db": outs["resp_snr_db"],            # (3,)
            "heart_bpm": outs["heart_bpm"],                # (3,)
            "heart_snr_db": outs["heart_snr_db"],          # (3,)
            "freqs_hz": FREQS_HZ,                          # (151,) 스펙트럼 주파수 축
            # ── 게이트 ──
            "gate_db": self.gate_db,
            "resp_pass": resp_pass,                        # (3,) bool
            "heart_pass": heart_pass,
            "nodes": nodes,
            "resp_selected": self._select(outs["resp_bpm"], outs["resp_snr_db"], resp_pass),
            "heart_selected": self._select(outs["heart_bpm"], outs["heart_snr_db"], heart_pass),
            # ── 신뢰도 ──
            # 하드코딩 False 다. 신호가 검출되지 않은 상태에서 이 값을 쓰는 코드가
            # 조용히 통과하면 안 된다.
            "trustworthy": False,
            "warning": _WARNING,
            "bpm_resolution": BPM_PER_BIN,                 # 2.0 — ArgMax bin 그대로라 이게 한계
        }


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
def _load_window_from_csv(path: str, start_sec: float) -> np.ndarray:
    import pandas as pd

    cols = ["node_id", "ts_ms"] + [f"raw_{i}" for i in range(N_SUB)]
    df = pd.read_csv(path, usecols=cols)
    start = int(round(start_sec * FS_HZ))
    frames = np.zeros((N_NODES, N_TIME, N_SUB), dtype=np.float32)
    node_ids = sorted(df["node_id"].unique())
    if len(node_ids) < N_NODES:
        raise ValueError(f"노드가 {len(node_ids)}개뿐이다 ({node_ids}). {N_NODES}개가 필요하다.")
    for i, node in enumerate(node_ids[:N_NODES]):
        g = df[df["node_id"] == node].sort_values("ts_ms")
        blk = g[[f"raw_{j}" for j in range(N_SUB)]].to_numpy(np.float32)
        if blk.shape[0] < start + N_TIME:
            raise ValueError(
                f"node {node} 프레임 {blk.shape[0]}개 — {start_sec}초부터 {N_TIME}프레임을 못 뜬다."
            )
        frames[i] = blk[start:start + N_TIME]
    return frames


def main(argv=None) -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="M2 스펙트럼 추정기 런타임 데모")
    ap.add_argument("--onnx", default=DEFAULT_ONNX)
    ap.add_argument("--csv", help="원본 CSI CSV 경로 (raw_0..63 컬럼)")
    ap.add_argument("--start-sec", type=float, default=0.0)
    ap.add_argument("--gate-db", type=float, default=GATE_DB)
    ap.add_argument("--demo", action="store_true", help="무작위 입력으로 배선만 확인")
    args = ap.parse_args(argv)

    if not args.csv and not args.demo:
        ap.error("--csv 또는 --demo 중 하나가 필요하다")

    rt = M2SpectrumRuntime(args.onnx, gate_db=args.gate_db)
    if args.csv:
        frames = _load_window_from_csv(args.csv, args.start_sec)
        src = f"{os.path.basename(args.csv)}  start={args.start_sec}s"
    else:
        frames = np.random.default_rng(0).random((N_NODES, N_TIME, N_SUB)).astype(np.float32)
        src = "무작위 입력 (배선 확인용)"

    out = rt.infer(frames)
    print(f"입력  : {src}")
    print(f"ONNX  : {out['resp_spectrum'].shape} 스펙트럼, 게이트 {out['gate_db']:.1f} dB, "
          f"bpm 해상도 {out['bpm_resolution']:.1f}")
    print()
    print(f"{'node':>5}{'resp_bpm':>10}{'resp_snr':>10}{'통과':>6}"
          f"{'heart_bpm':>11}{'heart_snr':>11}{'통과':>6}")
    print("-" * 59)
    for n in out["nodes"]:
        print(f"{n['node']:>5}{n['resp_bpm']:>10.1f}{n['resp_snr_db']:>10.2f}"
              f"{('O' if n['resp_pass'] else 'X'):>6}"
              f"{n['heart_bpm']:>11.1f}{n['heart_snr_db']:>11.2f}"
              f"{('O' if n['heart_pass'] else 'X'):>6}")
    print()
    for tag, label in (("resp_selected", "호흡"), ("heart_selected", "심박")):
        s = out[tag]
        if s["node"] is None:
            print(f"{label}: 게이트 통과 노드 없음")
        else:
            print(f"{label}: node {s['node']} 채택 — {s['bpm']:.1f} "
                  f"(SNR {s['snr_db']:.2f} dB, 통과 {s['n_passed']}/{N_NODES} 노드)")
    print()
    print(f"trustworthy = {out['trustworthy']}")
    print(f"경고: {out['warning']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
