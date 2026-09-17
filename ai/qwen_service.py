"""
ai/qwen_service.py
M5 Qwen SLM 서비스 루프.

ai-experts 컨테이너가 ai:result에 slm_needed=True로 기록한 항목을 소비하고
Qwen 추론 결과를 ai:emergency에 XADD한다.
"""

import json
import logging
import os
import time

import redis as _redis

# 추론 백엔드 선택: SLM_BACKEND = gguf | 15b | 05b
# (gguf는 llama.cpp 백엔드 — M5 한정 예외. ai-qwen 컨테이너 격리라 M1~M4 ONNX 무관)
_SLM_BACKEND = os.getenv("SLM_BACKEND", "gguf").lower()
if _SLM_BACKEND == "gguf":
    from logic.qwen_gguf import QwenLogic
elif _SLM_BACKEND == "15b":
    from logic.qwen_15b import QwenLogic
else:
    from logic.qwen_05b import QwenLogic
from utils import (
    stream_id_ts_ms as _stream_id_ts_ms,
    json_loads as _json_loads,
    build_context_window,
)


RESULT_STREAM    = "ai:result"
EMERGENCY_STREAM = "ai:emergency"
EMERGENCY_STREAM_MAXLEN = int(os.getenv("EMERGENCY_STREAM_MAXLEN", "3600"))
SLM_MIN_INTERVAL_MS     = int(os.getenv("SLM_MIN_INTERVAL_MS", "5000"))
SLM_CANDIDATE_MAX_AGE_MS = int(os.getenv("SLM_CANDIDATE_MAX_AGE_MS", "30000"))
SLM_READ_COUNT          = int(os.getenv("SLM_READ_COUNT", "100"))
SLM_DRAIN_MAX_BATCHES   = int(os.getenv("SLM_DRAIN_MAX_BATCHES", "20"))
CONTEXT_WINDOW_MINUTES  = int(os.getenv("CONTEXT_WINDOW_MINUTES", "10"))
MODEL_PATH = os.getenv("MODEL_PATH", "/app/models")
SLM_MODEL  = os.getenv("SLM_MODEL",  "qwen_15b_gguf_q5")
# gguf 백엔드용 chat_template/tokenizer 폴더 (models/ 하위)
SLM_TOKENIZER = os.getenv("SLM_TOKENIZER", "qwen_15b")

LOGGER = logging.getLogger("rp5.ai.qwen_svc")
if not LOGGER.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(message)s"))
    LOGGER.addHandler(_h)
LOGGER.setLevel(logging.INFO)
LOGGER.propagate = False


def _log(level: int, event: str, **fields):
    payload = {"service": "ai-qwen", "event": event, "ts_ms": int(time.time() * 1000)}
    payload.update(fields)
    LOGGER.log(level, json.dumps(payload, ensure_ascii=False))


def _connect_redis() -> _redis.Redis:
    host = os.getenv("REDIS_HOST", "db")
    port = int(os.getenv("REDIS_PORT", 6379))
    while True:
        try:
            r = _redis.Redis(host=host, port=port, decode_responses=False,
                             socket_connect_timeout=3)
            r.ping()
            _log(logging.INFO, "redis_connected", host=host, port=port)
            return r
        except _redis.exceptions.ConnectionError as exc:
            _log(logging.WARNING, "redis_not_ready", error=str(exc), retry_in_sec=2)
            time.sleep(2)


def _build_context_window(r: _redis.Redis, ts_ms: int) -> dict:
    return build_context_window(r, ts_ms, EMERGENCY_STREAM, CONTEXT_WINDOW_MINUTES)


def _warmup_qwen(qwen: QwenLogic):
    """ONNX Runtime 내부 버퍼/스레드 사전 초기화."""
    dummy = {
        "fall":      {"fall_score": 0.0, "infer_confidence": 0.5},
        "vital":     {"heart_rate": 72.0, "breathing_rate": 16.0, "infer_confidence": 0.5},
        "env_sound": {"label": "silence", "confidence": 0.0, "infer_confidence": 0.0},
        "speech_ko": {"keywords": [], "stt_confidence": 0.0, "speech_detected": False, "infer_confidence": 0.0},
    }
    try:
        qwen.evaluate(dummy)
        _log(logging.INFO, "qwen_warmup_completed")
    except Exception as exc:
        _log(logging.WARNING, "qwen_warmup_failed", error=str(exc))


def _write_emergency(r: _redis.Redis, snapshot: dict, fused: dict):
    entry = {
        "ts_ms":                  snapshot.get("ts_ms"),
        "node_id":                snapshot.get("node_id", 0),
        "risk_score":             fused.get("risk_score", snapshot.get("risk_score", 0.0)),
        "risk_level":             fused.get("risk_level", snapshot.get("risk_level", "warning")),
        "emergency":              fused.get("emergency", snapshot.get("emergency", False)),
        "qwen_reason":            fused.get("qwen_reason"),
        "slm_invoked":            True,
        "is_outlier":             bool(fused.get("is_outlier", False)),
        "correlated_with_history": bool(fused.get("correlated_with_history", False)),
        "slm_mode":               fused.get("slm_mode"),
        "summary":                fused.get("summary") or fused.get("qwen_reason", ""),
        "emergency_breakdown":    snapshot.get("emergency_breakdown"),
    }
    r.xadd(
        EMERGENCY_STREAM,
        {"data": json.dumps(entry, ensure_ascii=False)},
        maxlen=EMERGENCY_STREAM_MAXLEN,
        approximate=True,
    )


def _select_candidate(messages, now_ms: int) -> tuple[bytes, dict] | None:
    """한 번에 읽은 M5 후보 중 최고 위험을, 동점이면 최신 항목을 선택한다."""
    candidates: list[tuple[float, int, bytes, dict]] = []
    for msg_id, fields in messages:
        if fields.get(b"slm_needed") != b"True":
            continue
        snapshot = _json_loads(fields.get(b"data", b""))
        if not snapshot.get("experts"):
            continue
        stream_ts_ms = _stream_id_ts_ms(msg_id)
        age_ms = max(0, now_ms - stream_ts_ms)
        if age_ms > SLM_CANDIDATE_MAX_AGE_MS:
            continue
        try:
            risk_score = float(snapshot.get("risk_score", 0.0) or 0.0)
        except (TypeError, ValueError):
            risk_score = 0.0
        candidates.append((risk_score, stream_ts_ms, msg_id, snapshot))

    if not candidates:
        return None
    _, _, msg_id, snapshot = max(candidates, key=lambda item: (item[0], item[1]))
    return msg_id, snapshot


def _phase2_locked(r: _redis.Redis, node_id: int) -> bool:
    try:
        return r.get(f"phase2:active:{node_id}") is not None
    except _redis.exceptions.RedisError:
        return False


def _drain_result_messages(r: _redis.Redis, entries, last_id):
    """현재 ai:result backlog를 설정된 상한까지 소진해 flat list와 마지막 ID를 반환한다."""
    messages = []
    pending = entries
    for _ in range(max(1, SLM_DRAIN_MAX_BATCHES)):
        for _, batch in pending:
            if batch:
                messages.extend(batch)
                last_id = batch[-1][0]
        pending = r.xread({RESULT_STREAM: last_id}, count=max(1, SLM_READ_COUNT))
        if not pending:
            break
    return messages, last_id


def _select_unlocked_candidate(r: _redis.Redis, messages, now_ms: int) -> dict | None:
    """Phase 2 lock이 없는 노드 중 최고 위험·최신 snapshot을 선택한다."""
    remaining = list(messages)
    while remaining:
        selected = _select_candidate(remaining, now_ms)
        if selected is None:
            return None
        selected_id, snapshot = selected
        node_id = int(snapshot.get("node_id", 0) or 0)
        if not _phase2_locked(r, node_id):
            return snapshot
        _log(logging.INFO, "qwen_skipped_phase2_lock", node_id=node_id)
        remaining = [item for item in remaining if item[0] != selected_id]
    return None


def run():
    r = _connect_redis()

    # 모델 로딩·워밍업 중 들어온 최신 후보를 복구하되, 오래된 운영 백로그는 재생하지 않는다.
    startup_ms = int(time.time() * 1000)
    last_id = f"{max(0, startup_ms - SLM_CANDIDATE_MAX_AGE_MS)}-0"
    if _SLM_BACKEND == "gguf":
        qwen = QwenLogic(os.path.join(MODEL_PATH, SLM_MODEL),
                         tokenizer_dir=os.path.join(MODEL_PATH, SLM_TOKENIZER))
    else:
        qwen = QwenLogic(os.path.join(MODEL_PATH, SLM_MODEL))
    qwen.redis_client = r
    _warmup_qwen(qwen)

    last_completed_ms: int = 0
    _log(logging.INFO, "qwen_service_started", stream=RESULT_STREAM,
         candidate_max_age_ms=SLM_CANDIDATE_MAX_AGE_MS,
         min_interval_ms=SLM_MIN_INTERVAL_MS)

    while True:
        try:
            entries = r.xread(
                {RESULT_STREAM: last_id}, count=max(1, SLM_READ_COUNT), block=2000
            )
            if not entries:
                continue

            # M5 추론 중 쌓인 ai:result를 bounded drain해 첫 batch가 아니라
            # 현재 backlog의 위험 후보를 비교한다.
            flat_messages, last_id = _drain_result_messages(r, entries, last_id)

            now_ms = int(time.time() * 1000)
            snapshot = _select_unlocked_candidate(r, flat_messages, now_ms)
            if snapshot is None:
                continue

            node_id = int(snapshot.get("node_id", 0) or 0)
            if now_ms - last_completed_ms < SLM_MIN_INTERVAL_MS:
                continue

            expert_results = snapshot["experts"]
            ts_ms = int(snapshot.get("ts_ms", now_ms))
            context_window = _build_context_window(r, ts_ms)
            try:
                fused = qwen.evaluate(expert_results, context_window=context_window)
                _write_emergency(r, snapshot, fused)
                _log(logging.INFO, "qwen_invoked",
                     node_id=node_id,
                     gate_risk_score=snapshot.get("risk_score", 0.0),
                     risk_score=fused.get("risk_score", snapshot.get("risk_score", 0.0)),
                     risk_level=fused.get("risk_level", "?"),
                     qwen_infer_ms=fused.get("qwen_infer_ms"))
            except Exception as exc:
                _log(logging.ERROR, "qwen_failed", error=str(exc))
            finally:
                # 성공·실패 모두 완료 시점부터 간격을 둬 오류 루프와 backlog 재실행을 막는다.
                last_completed_ms = int(time.time() * 1000)

        except _redis.exceptions.ConnectionError as exc:
            _log(logging.WARNING, "redis_reconnecting", error=str(exc))
            time.sleep(2)
            r = _connect_redis()
            qwen.redis_client = r

        except Exception as exc:
            _log(logging.ERROR, "qwen_service_error", error=str(exc))
            time.sleep(1)


if __name__ == "__main__":
    run()
