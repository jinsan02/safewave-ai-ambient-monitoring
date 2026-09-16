#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""m1_runtime.py — M1 낙상감지 런타임 (전처리 → ONNX 추론 → 판정) 한 파일.

학습 레포의 `src/m1_fall/dataset.py` 전처리를 그대로 복제한 것이다. 학습과 한
단계라도 다르면 점수가 무의미해지므로, 이 파일을 고칠 때는 dataset.py 와
대조해서 고쳐라.

────────────────────────────────────────────────────────────────────────────
입력 규격
────────────────────────────────────────────────────────────────────────────
세 가지 중 하나로 넣는다.

1) CSV 파일 경로 (수집기 산출물)
     필수 컬럼: stream_id, node_id, ts_ms, raw_0 .. raw_63
     stream_id 형식: "<epoch_ms>-<seq>"  예) "1781165460004-0"
     node_id: 1 .. N_NODES (정수)
     >>> predict_from_csv("csi_20260916_1327_fall.csv")

2) 패킷 리스트 (실시간 스트림)
     [{"stream_id": "1781165460004-0", "node_id": 1, "raw": [64개 float]}, ...]
     ts_ms 는 없어도 된다 — 정렬에 쓰지 않는다 (아래 주의 참조).
     >>> predict_from_packets(packets)

3) 이미 만들어진 텐서 (1, N_NODES, 64, 100) float32
     >>> M1Runtime().predict_tensor(x)

⚠ 노드 간 시각 정렬은 반드시 **stream_id 앞부분의 epoch**(rp5 수신 시각)로 한다.
  `ts_ms` 는 보드마다 다른 장치 시계(32비트 랩)라 노드를 교차 정렬하면 어긋난다.
  이 파일은 ts_ms 를 읽기만 하고 정렬에는 쓰지 않는다.

────────────────────────────────────────────────────────────────────────────
출력 규격  — predict_* 가 돌려주는 dict
────────────────────────────────────────────────────────────────────────────
  score            float   0~1 낙상 확률 (sigmoid 적용 후)
  logit            float   sigmoid 적용 전 원시 로짓 (ONNX 출력 그대로)
  fired            bool    score >= threshold
  threshold        float   판정에 쓴 임계값
  n_nodes          int     모델이 기대하는 노드 수 (ONNX 에서 읽는다)
  node_coverage    list[float]  노드별 실제 프레임이 채워진 비율 0~1 (길이 n_nodes)
  coverage_mean    float   위의 평균 — 낮으면 수집/동기 문제를 먼저 의심하라
  window_epoch_ms  [int, int]  창의 첫/마지막 그리드 슬롯 시각 (epoch ms)
  n_windows        int     입력에서 만들어진 창 개수 (predict_from_csv 에서만 >1 가능)

predict_from_csv 는 `windows` 키에 창별 dict 리스트도 함께 담는다.

────────────────────────────────────────────────────────────────────────────
전처리 순서 (학습과 동일)
────────────────────────────────────────────────────────────────────────────
  1. 행마다 epoch = stream_id 앞부분
  2. 노드별로 guard 서브캐리어 0 처리 후 **프레임별 peak 정규화**
     (ESP 가 이미 하는 정규화와 맞춘다. 재정규화·절대진폭 복원 금지)
  3. 공통 100 Hz epoch 그리드(10 ms 간격)에 노드별 프레임을 최근접 슬롯(±5 ms)
     으로 매핑. 한 슬롯에 둘이 겹치면 더 가까운 쪽을 남긴다
  4. 비어 있는 (노드, 슬롯) 은 0 으로 둔다
  5. 100 프레임 창 → (1, N_NODES, 64, 100)

주의: 모델 출력은 **pre-sigmoid 로짓**이다. ONNX 그래프에 Sigmoid 가 없으므로
      이 파일에서 한 번만 적용한다. 두 번 적용하면 점수가 0.5~0.73 으로 뭉개져
      임계값이 무의미해진다.

사용 예
    python scripts/m1_runtime.py --csv data/raw_main/csi_20260916_1327_fall.csv
    python scripts/m1_runtime.py --csv <파일> --threshold 0.8 --json
"""
from __future__ import annotations

import argparse
import csv as _csv
import json
import sys
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np

# ── 계약 상수 — 학습과 묶여 있다. 임의로 바꾸지 마라 ─────────────────────────
MODEL_PATH = "models/m1_base_3node_20260916.onnx"
INPUT_NAME = "csi_data"          # ONNX 입력 이름
OUTPUT_NAME = "fall_logit"       # ONNX 출력 이름 (pre-sigmoid)
N_SUBCARRIERS = 64
N_FRAMES = 100                   # 1초 @ 100 Hz
SAMPLE_RATE_HZ = 100
PERIOD_MS = 10                   # 1000 / SAMPLE_RATE_HZ
TOL_MS = 5                       # 최근접 슬롯 허용 오차 (PERIOD_MS // 2)
GUARD_INDICES = [0, 1, 2, 3, 4, 5, 32, 59, 60, 61, 62, 63]
NORM_EPS = 1e-6
# 운영 권고값. f1 최적점이 아니라 우도비(낙상 재현율 / 빈 방 발화율) 기준으로 골랐다.
#
# 검증셋 양성 비율은 24.35% 지만 실제 운영에서 낙상 창 비율은 10만분의 1 수준이다.
# 이 사전확률 차이 때문에 검증셋 f1 최적점은 배포에서 오경보로 무너진다.
# 그래서 f1(0.75 에서 0.503 vs 0.80 에서 0.511)이 아니라
# 우도비(7.67 vs 38.08)로 고른다 — 0.80 이 5배 낫다.
#
# @0.80 실측: 빈 방 발화 3/238 = 1.3% · 낙상 재현율 108/225 = 0.480
# 근거와 임계값 전체 표: docs/M1_HANDOFF.md §3
#
# 단, 이 값은 M1 이 단독으로 이진 판정을 내려야 할 때의 기본값이다.
# M5 와 연동한다면 임계값을 적용하지 말고 score(또는 logit)를 그대로 넘겨라
# (docs/M1_HANDOFF.md "M5 연동 권고" 참조).
DEFAULT_THRESHOLD = 0.80

RAW_COLS = [f"raw_{i}" for i in range(N_SUBCARRIERS)]


# ── 전처리 ───────────────────────────────────────────────────────────────────

def epoch_of(stream_id: str) -> int:
    """'1781165460004-0' -> 1781165460004 (rp5 수신 시각 = 노드 간 유일한 공통 시계)."""
    head = str(stream_id).split("-", 1)[0].strip()
    if not head.isdigit():
        raise ValueError(f"stream_id 에서 epoch 를 읽을 수 없다: {stream_id!r} "
                         "('<epoch_ms>-<seq>' 형식이어야 한다)")
    return int(head)


def normalize_per_frame(frames: np.ndarray) -> np.ndarray:
    """(T, 64) -> 프레임별 peak 정규화. guard 는 0, 나머지는 그 프레임의 최댓값으로 나눈다.

    ESP 가 패킷 시점에 하는 정규화와 같다. 절대 진폭은 의도적으로 사라진다 —
    상대적인 채널 패턴과 시간 변화만 남긴다. 노드끼리 묶어 정규화하면 안 된다.
    """
    out = np.asarray(frames, dtype=np.float32).copy()
    guard = np.zeros(out.shape[1], dtype=bool)
    guard[GUARD_INDICES] = True
    out[:, guard] = 0.0
    peak = np.max(np.abs(out[:, ~guard]), axis=1, keepdims=True)
    out[:, ~guard] = out[:, ~guard] / np.maximum(peak, NORM_EPS)
    return out


def align_to_grid(epochs: np.ndarray, node_ids: np.ndarray, raw: np.ndarray,
                  n_nodes: int) -> tuple:
    """epoch 기준 100 Hz 공통 그리드로 노드를 정렬한다.

    반환: (grid_epoch_ms (T,), frames (T, n_nodes, 64), presence (T, n_nodes) bool)
    빈 (슬롯, 노드) 는 frames 가 0, presence 가 False 다.
    """
    epochs = np.asarray(epochs, dtype=np.int64)
    node_ids = np.asarray(node_ids, dtype=np.int64)
    raw = np.asarray(raw, dtype=np.float32)

    bad = sorted(set(node_ids[(node_ids < 1) | (node_ids > n_nodes)].tolist()))
    if bad:
        raise ValueError(f"node_id {bad} 가 1..{n_nodes} 범위 밖이다. "
                         f"모델은 노드 {n_nodes}개를 기대한다.")

    t0 = int(epochs.min())
    n_grid = int((int(epochs.max()) - t0) // PERIOD_MS) + 1
    frames = np.zeros((n_grid, n_nodes, N_SUBCARRIERS), dtype=np.float32)
    presence = np.zeros((n_grid, n_nodes), dtype=bool)
    grid_epoch_ms = t0 + PERIOD_MS * np.arange(n_grid, dtype=np.int64)

    for nid in sorted(set(node_ids.tolist())):
        sel = node_ids == nid
        e = epochs[sel]
        norm = normalize_per_frame(raw[sel])          # 정렬 전에 노드별로 정규화

        idx = np.round((e - t0) / PERIOD_MS).astype(np.int64)
        resid = np.abs(e - (t0 + idx * PERIOD_MS))
        keep = (resid <= TOL_MS) & (idx >= 0) & (idx < n_grid)
        idx, resid, norm = idx[keep], resid[keep], norm[keep]
        if len(idx) == 0:
            continue

        # 한 슬롯에 둘 이상 들어오면 슬롯 시각에 더 가까운 쪽만 남긴다
        order = np.lexsort((resid, idx))
        idx, norm = idx[order], norm[order]
        first = np.ones(len(idx), dtype=bool)
        first[1:] = idx[1:] != idx[:-1]
        idx, norm = idx[first], norm[first]

        frames[idx, nid - 1] = norm
        presence[idx, nid - 1] = True

    return grid_epoch_ms, frames, presence


def read_csv_rows(csv_path: str | Path) -> tuple:
    """수집 CSV -> (epochs, node_ids, raw (N,64)). 표준 라이브러리만 쓴다."""
    path = Path(csv_path)
    with path.open(encoding="utf-8", newline="") as fh:
        rdr = _csv.DictReader(fh)
        cols = rdr.fieldnames or []
        missing = [c for c in (["stream_id", "node_id"] + RAW_COLS) if c not in cols]
        if missing:
            raise ValueError(f"{path.name}: 필수 컬럼 없음 {missing[:5]}"
                             f"{'...' if len(missing) > 5 else ''}")
        eps, nids, raws = [], [], []
        for row in rdr:
            eps.append(epoch_of(row["stream_id"]))
            nids.append(int(float(row["node_id"])))
            raws.append([float(row[c]) for c in RAW_COLS])
    if not eps:
        raise ValueError(f"{path.name}: 데이터 행이 없다")
    return (np.asarray(eps, np.int64), np.asarray(nids, np.int64),
            np.asarray(raws, np.float32))


def packets_to_arrays(packets: Iterable[dict]) -> tuple:
    """[{stream_id, node_id, raw[64]}, ...] -> (epochs, node_ids, raw (N,64))."""
    eps, nids, raws = [], [], []
    for p in packets:
        eps.append(epoch_of(p["stream_id"]))
        nids.append(int(p["node_id"]))
        vec = p.get("raw", p.get("data_raw"))
        if vec is None or len(vec) != N_SUBCARRIERS:
            raise ValueError(f"raw 는 {N_SUBCARRIERS}개 float 여야 한다 "
                             f"(받은 길이: {0 if vec is None else len(vec)})")
        raws.append(list(vec))
    if not eps:
        raise ValueError("패킷이 비어 있다")
    return (np.asarray(eps, np.int64), np.asarray(nids, np.int64),
            np.asarray(raws, np.float32))


# ── 런타임 ───────────────────────────────────────────────────────────────────

class M1Runtime:
    """ONNX 세션을 한 번만 만들어 재사용한다. 스레드마다 하나씩 두는 것을 권한다."""

    def __init__(self, model_path: str | Path = MODEL_PATH,
                 threshold: float = DEFAULT_THRESHOLD, providers: Sequence[str] | None = None):
        import onnxruntime as ort
        path = Path(model_path)
        if not path.exists():
            raise FileNotFoundError(f"ONNX 가 없다: {path}")
        self.threshold = float(threshold)
        self.session = ort.InferenceSession(
            str(path), providers=list(providers) if providers else ["CPUExecutionProvider"])

        inp = self.session.get_inputs()[0]
        if inp.name != INPUT_NAME:
            raise ValueError(f"입력 이름이 {inp.name!r} 이다. 계약은 {INPUT_NAME!r}")
        # shape 예: ['batch', 3, 64, 100] — 노드 수를 모델에서 직접 읽는다
        _, n_nodes, n_sub, n_frames = inp.shape
        if (n_sub, n_frames) != (N_SUBCARRIERS, N_FRAMES):
            raise ValueError(f"ONNX 입력이 (?,?,{n_sub},{n_frames}) 다. "
                             f"계약은 (?,?,{N_SUBCARRIERS},{N_FRAMES})")
        self.n_nodes = int(n_nodes)
        self.model_path = str(path)

    # ── 텐서 하나 추론 ────────────────────────────────────────────────────
    def predict_tensor(self, x: np.ndarray) -> dict:
        """(1, n_nodes, 64, 100) float32 -> 판정 dict. 배치도 받지만 첫 창만 요약한다."""
        x = np.asarray(x, dtype=np.float32)
        if x.ndim == 3:
            x = x[None, ...]
        expected = (self.n_nodes, N_SUBCARRIERS, N_FRAMES)
        if x.shape[1:] != expected:
            raise ValueError(f"입력 shape {x.shape[1:]} != 계약 {expected}")
        logits = np.asarray(self.session.run(None, {INPUT_NAME: x})[0]).reshape(-1)
        scores = 1.0 / (1.0 + np.exp(-logits))        # sigmoid — 여기서 딱 한 번
        return {"score": float(scores[0]), "logit": float(logits[0]),
                "fired": bool(scores[0] >= self.threshold), "threshold": self.threshold,
                "n_nodes": self.n_nodes, "_scores": scores, "_logits": logits}

    # ── 정렬된 세션에서 창을 잘라 추론 ────────────────────────────────────
    def predict_arrays(self, epochs, node_ids, raw, stride: int | None = None) -> dict:
        grid_ms, frames, presence = align_to_grid(epochs, node_ids, raw, self.n_nodes)
        if len(frames) < N_FRAMES:
            raise ValueError(f"프레임이 {len(frames)}개뿐이다. {N_FRAMES}개(1초) 이상 필요하다.")

        if stride is None:                            # 기본: 가장 최근 창 하나
            starts = [len(frames) - N_FRAMES]
        else:
            starts = list(range(0, len(frames) - N_FRAMES + 1, int(stride)))

        wins = np.stack([frames[s:s + N_FRAMES].transpose(1, 2, 0) for s in starts])
        logits = np.asarray(self.session.run(None, {INPUT_NAME: wins.astype(np.float32)})[0]).reshape(-1)
        scores = 1.0 / (1.0 + np.exp(-logits))        # sigmoid — 여기서 딱 한 번

        windows = []
        for k, s in enumerate(starts):
            cov = presence[s:s + N_FRAMES].mean(axis=0)
            windows.append({
                "score": float(scores[k]), "logit": float(logits[k]),
                "fired": bool(scores[k] >= self.threshold), "threshold": self.threshold,
                "n_nodes": self.n_nodes,
                "node_coverage": [float(c) for c in cov],
                "coverage_mean": float(cov.mean()),
                "window_epoch_ms": [int(grid_ms[s]), int(grid_ms[s + N_FRAMES - 1])],
            })
        out = dict(windows[-1])                       # 대표값 = 가장 최근 창
        out["n_windows"] = len(windows)
        out["windows"] = windows
        return out

    def predict_from_packets(self, packets: Iterable[dict]) -> dict:
        return self.predict_arrays(*packets_to_arrays(packets))

    def predict_from_csv(self, csv_path: str | Path, stride: int | None = 50) -> dict:
        return self.predict_arrays(*read_csv_rows(csv_path), stride=stride)


# ── 모듈 수준 편의 함수 (세션을 매번 만든다 — 반복 호출은 M1Runtime 을 직접 써라) ──

def predict_from_csv(csv_path, model_path=MODEL_PATH, threshold=DEFAULT_THRESHOLD, stride=50):
    return M1Runtime(model_path, threshold).predict_from_csv(csv_path, stride=stride)


def predict_from_packets(packets, model_path=MODEL_PATH, threshold=DEFAULT_THRESHOLD):
    return M1Runtime(model_path, threshold).predict_from_packets(packets)


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="M1 낙상감지 런타임")
    ap.add_argument("--csv", required=True, help="수집 CSV 경로")
    ap.add_argument("--model", default=MODEL_PATH)
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    ap.add_argument("--stride", type=int, default=50,
                    help="창 간격(프레임). 0 이면 가장 최근 창 하나만")
    ap.add_argument("--json", action="store_true", help="전체 결과를 JSON 으로 출력")
    a = ap.parse_args()

    rt = M1Runtime(a.model, a.threshold)
    res = rt.predict_from_csv(a.csv, stride=(a.stride or None))

    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    print(f"모델   : {rt.model_path} (노드 {rt.n_nodes}개, 임계값 {a.threshold})")
    print(f"창     : {res['n_windows']}개 · 노드 커버리지 평균 {res['coverage_mean']:.1%}")
    fired = sum(1 for w in res["windows"] if w["fired"])
    print(f"발화   : {fired}/{res['n_windows']} 창 ({fired / res['n_windows']:.1%})")
    print(f"최근 창: score {res['score']:.4f} (logit {res['logit']:+.4f}) "
          f"-> {'낙상 의심' if res['fired'] else '정상'}")
    print(f"         노드별 커버리지 " +
          " ".join(f"n{i + 1}={c:.1%}" for i, c in enumerate(res["node_coverage"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
