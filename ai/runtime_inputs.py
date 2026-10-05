"""ai/main.py의 M1 런타임 입력·판정 보조 함수."""

from collections import deque
from collections.abc import Iterable, Mapping, Sequence

import numpy as np


def grid_slot(stream_ts_ms: int, frame_interval_ms: int = 10) -> int:
    """수신 시각(Redis stream id ms)을 100Hz 전역 격자 슬롯으로 스냅한다(±interval/2).

    학습 로더와 같이 장치 시계(ts_ms)가 아니라 RPi5 수신 시각을 쓴다. 장치 시계는 보드마다
    다르고 간격이 9~11ms로 흔들려 슬롯이 어긋난다(김태연 09-17 답변).
    """
    if frame_interval_ms <= 0:
        raise ValueError("frame_interval_ms must be positive")
    return (int(stream_ts_ms) + frame_interval_ms // 2) // frame_interval_ms


def place_m1_grid_frame(buffer: deque, frame, slot: int, last_slot: int | None) -> tuple[int, bool]:
    """전역 격자 슬롯에 프레임을 놓는다. 반환값은 ``(zero_filled_frames, replaced)``.

    빠진 슬롯은 0으로 채우고(deque 길이까지만), 같은 슬롯에 두 번째 프레임이 오면
    최신 프레임으로 바꾼다. stream id는 단조 증가라 역행·wrap 처리가 필요 없다.
    """
    frame_array = np.asarray(frame, dtype=np.float32)
    if last_slot is None or not buffer:
        buffer.append(frame_array)
        return 0, False
    step = int(slot) - int(last_slot)
    if step <= 0:
        buffer[-1] = frame_array
        return 0, True
    missing = step - 1
    if buffer.maxlen is not None:
        missing = min(missing, buffer.maxlen)
    for _ in range(missing):
        buffer.append(np.zeros_like(frame_array))
    buffer.append(frame_array)
    return missing, False


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


def expired_m1_ticks(now_ms: int, tick_start_ms: int, interval_ms: int) -> int:
    """추론 없이 끝난 200ms tick 수. 현재 진행 중인 tick은 세지 않는다."""
    if tick_start_ms <= 0 or interval_ms <= 0:
        return 0
    return max(0, (int(now_ms) - int(tick_start_ms)) // int(interval_ms) - 1)


def record_skipped_m1_ticks(
    votes: deque,
    count: int,
    previous: Mapping,
    *,
    required_votes: int = 3,
    window_size: int = 5,
    insufficient: bool = False,
) -> dict:
    """게이트로 생략된 tick을 "발화 아님"(0)으로 K/N 창에 넣는다.

    측정 전제(김태연 09-17 답변 (a))와 같이 창 N개가 항상 최근 N×200ms를 뜻하게 한다.
    """
    for _ in range(min(int(count), window_size)):
        votes.append(False)
    while len(votes) > window_size:
        votes.popleft()
    vote_count = sum(bool(value) for value in votes)
    if insufficient:
        result = insufficient_m1_result(required_votes=required_votes, window_size=window_size)
    else:
        result = dict(previous)
    result.update({
        "window_fall_detected": False,
        "fall_detected": len(votes) >= window_size and vote_count >= required_votes,
        "fall_votes": vote_count,
        "fall_vote_samples": len(votes),
        "skipped_ticks": int(previous.get("skipped_ticks", 0)) + int(count),
    })
    return result


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


class BoardScoreAggregator:
    """N_pose 보드 추론 결과(CSR!, 노드별 5 Hz) → 노드별 K/N → 허브 판정.

    규칙은 김태연 hub/receiver.py와 같다: 추론 1회는 창이 찼고(window_full) 새 프레임 비율이
    min_coverage 이상이며 벤치용 무작위 모델이 아닐 때만 '쓸 수 있는' 추론이고, 그중 score ≥ threshold면 발화.
    노드 최근 N회 중 K회 이상 발화면 노드 경보, 경보 노드 수 ≥ min_nodes면 허브 경보(fall_detected).
    max_age_ms보다 오래 결과가 없는 노드는 꺼진 것으로 보고 판정에서 뺀다.
    """

    def __init__(self, *, threshold: float = 0.8, required_votes: int = 3, window_size: int = 5,
                 min_nodes: int = 1, min_coverage: float = 0.9, allow_random: bool = False,
                 max_age_ms: int = 1000):
        if required_votes <= 0 or window_size <= 0 or required_votes > window_size or min_nodes <= 0:
            raise ValueError("invalid board aggregation rule")
        self.threshold, self.k, self.n = float(threshold), int(required_votes), int(window_size)
        self.min_nodes, self.min_coverage = int(min_nodes), float(min_coverage)
        self.allow_random, self.max_age_ms = bool(allow_random), int(max_age_ms)
        self._nodes: dict[int, dict] = {}

    def add(self, node_id: int, recv_ms: int, score: float, coverage: float, flags: int) -> None:
        window_full, rand = bool(flags & 2), bool(flags & 1)
        usable = window_full and coverage >= self.min_coverage and (self.allow_random or not rand)
        st = self._nodes.setdefault(int(node_id), {"votes": deque(maxlen=self.n)})
        st["votes"].append(usable and score >= self.threshold)
        st.update(recv_ms=int(recv_ms), score=float(score), coverage=float(coverage),
                  window_full=window_full, random=rand, usable=usable)

    def result(self, now_ms: int) -> dict:
        fresh = {n: st for n, st in self._nodes.items() if 0 <= now_ms - st["recv_ms"] <= self.max_age_ms}
        if not fresh:
            return insufficient_m1_result(required_votes=self.k, window_size=self.n) | {
                "infer_source": "board", "alarm_nodes": [], "min_nodes": self.min_nodes, "nodes": {}}
        nodes, alarm_nodes = {}, []
        for n, st in sorted(fresh.items()):
            votes = sum(st["votes"])
            alarm = votes >= self.k
            if alarm:
                alarm_nodes.append(n)
            nodes[str(n)] = {"score": round(st["score"], 4), "votes": votes, "samples": len(st["votes"]),
                             "alarm": alarm, "coverage": round(st["coverage"], 3),
                             "window_full": st["window_full"], "random": st["random"],
                             "age_ms": int(now_ms - st["recv_ms"])}
        usable_scores = [st["score"] for st in fresh.values() if st["usable"]]
        top = max(fresh.values(), key=lambda st: sum(st["votes"]))
        return {
            # M5·게이트용 대표 점수 = 쓸 수 있는 추론 중 최고 노드 점수
            "fall_score": round(max(usable_scores), 4) if usable_scores else 0.0,
            "window_fall_detected": any(st["votes"] and st["votes"][-1] for st in fresh.values()),
            "fall_detected": len(alarm_nodes) >= self.min_nodes,
            "input_status": "ready",
            "insufficient_input": False,
            "infer_source": "board",
            "infer_confidence": 0.75,
            "fall_votes": sum(top["votes"]),
            "fall_vote_samples": len(top["votes"]),
            "fall_vote_required": self.k,
            "fall_vote_window": self.n,
            "alarm_nodes": alarm_nodes,
            "min_nodes": self.min_nodes,
            "nodes": nodes,
        }


def audio_event_plan(event_ms: int, tail_ms: int | None, now_ms: int, max_wait_ms: int) -> str:
    """오디오 이벤트 처리 방식: 'full'(M4+M3) / 'm4_only'(뒤에 밀린 이벤트가 있음) / 'drop'(너무 오래 밀림).

    밀린 이벤트를 건너뛰면 그 안의 긴급 음성도 사라지므로 순서대로 처리하되, 뒤에 새 이벤트가
    있으면 M3를 빼서 따라잡는다(마지막 이벤트만 M3까지). max_wait_ms보다 오래 기다린 것만 버린다.
    tail_ms: 스트림 마지막 이벤트 시각(이 이벤트 자신이 마지막이면 None).
    """
    if max_wait_ms > 0 and now_ms - event_ms > max_wait_ms:
        return "drop"
    if tail_ms is not None and tail_ms >= event_ms:
        return "m4_only"
    return "full"
