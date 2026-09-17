"""ai/main.py의 M1 런타임 입력·판정 보조 함수."""

from collections import deque
from collections.abc import Iterable, Mapping, Sequence

import numpy as np


_UINT32_MODULUS = 1 << 32


def device_time_delta_ms(current_ms: int, previous_ms: int) -> int:
    """ESP uint32 millisecond clock의 wrap을 고려한 signed delta를 반환한다."""
    current = int(current_ms)
    previous = int(previous_ms)
    if 0 <= current < _UINT32_MODULUS and 0 <= previous < _UINT32_MODULUS:
        delta = (current - previous) % _UINT32_MODULUS
        return delta if delta < (_UINT32_MODULUS // 2) else delta - _UINT32_MODULUS
    return current - previous


def m1_window_ready(
    required_nodes: Iterable[int],
    node_buffers: Mapping[int, Sequence],
    *,
    max_nodes: int,
    window_frames: int,
) -> bool:
    nodes = tuple(required_nodes)
    return bool(nodes) and all(
        1 <= node_id <= max_nodes
        and len(node_buffers.get(node_id, ())) == window_frames
        for node_id in nodes
    )


def append_m1_grid_frame(
    buffer: deque,
    frame,
    gap_ms: int,
    *,
    frame_interval_ms: int = 10,
) -> tuple[int, bool]:
    """100Hz 격자를 유지하며 결측 슬롯을 0으로 채운 뒤 실제 프레임을 추가한다.

    반환값은 ``(zero_filled_frames, clock_reset)``이다. 음수 gap은 장치 시계 재시작으로
    보고 이전 창을 버린다. 양수 gap의 결측 수는 deque 길이까지만 채워 과도한 반복을 막는다.
    """
    if frame_interval_ms <= 0:
        raise ValueError("frame_interval_ms must be positive")

    if gap_ms < 0:
        buffer.clear()
        clock_reset = True
        missing = 0
    else:
        clock_reset = False
        missing = max(0, int((gap_ms + frame_interval_ms // 2) // frame_interval_ms) - 1)
        if buffer.maxlen is not None:
            missing = min(missing, buffer.maxlen)

    frame_array = np.asarray(frame, dtype=np.float32)
    for _ in range(missing):
        buffer.append(np.zeros_like(frame_array))
    buffer.append(frame_array)
    return missing, clock_reset


def m1_tail_ready(
    required_nodes: Iterable[int],
    last_arrival_ms: Mapping[int, int],
    reference_ms: int,
    *,
    max_age_ms: int = 10,
) -> bool:
    """필수 노드의 가장 최근 실제 프레임이 현재 100Hz 슬롯에 모두 있는지 판정한다."""
    nodes = tuple(required_nodes)
    if not nodes:
        return False
    return all(
        node_id in last_arrival_ms
        and 0 <= reference_ms - int(last_arrival_ms[node_id]) <= max_age_ms
        for node_id in nodes
    )


def aggregate_m1_result(
    result: Mapping,
    votes: deque,
    *,
    required_votes: int = 3,
    window_size: int = 5,
) -> dict:
    """창 단위 판정을 최근 N회 중 K회 규칙으로 집계한다.

    원본 점수는 M5 가중치용으로 유지하고, ``fall_detected``만 사건 집계값으로 바꾼다.
    """
    if required_votes <= 0 or window_size <= 0 or required_votes > window_size:
        raise ValueError("invalid K/N aggregation")

    raw_detected = bool(result.get("fall_detected", False))
    votes.append(raw_detected)
    while len(votes) > window_size:
        votes.popleft()
    vote_count = sum(bool(value) for value in votes)

    aggregated = dict(result)
    aggregated.update({
        "window_fall_detected": raw_detected,
        "fall_detected": len(votes) >= window_size and vote_count >= required_votes,
        "input_status": "ready",
        "insufficient_input": False,
        "fall_votes": vote_count,
        "fall_vote_samples": len(votes),
        "fall_vote_required": required_votes,
        "fall_vote_window": window_size,
    })
    return aggregated


def insufficient_m1_result(*, required_votes: int = 3, window_size: int = 5) -> dict:
    """모델을 호출하지 못한 입력 부족 상태. 모델 점수와 혼동하지 않는다."""
    return {
        "fall_score": 0.0,
        "window_fall_detected": False,
        "fall_detected": False,
        "input_status": "insufficient_input",
        "insufficient_input": True,
        "infer_source": "skipped",
        "infer_confidence": 0.0,
        "fall_votes": 0,
        "fall_vote_samples": 0,
        "fall_vote_required": required_votes,
        "fall_vote_window": window_size,
    }


def build_m1_input(
    active_nodes: Iterable[int],
    node_buffers: Mapping[int, Sequence],
    *,
    max_nodes: int,
    window_frames: int,
    channels: int = 64,
) -> np.ndarray:
    """node_id N을 슬롯 N-1에 고정한 (1,node,channel,frame) 입력을 만든다."""
    slots = [np.zeros((channels, window_frames), dtype=np.float32) for _ in range(max_nodes)]
    for node_id in active_nodes:
        frames = node_buffers.get(node_id)
        if not 1 <= node_id <= max_nodes or frames is None or len(frames) != window_frames:
            continue
        slots[node_id - 1] = np.stack(list(frames), axis=0).T.astype(np.float32)
    return np.stack(slots, axis=0)[None, ...]
