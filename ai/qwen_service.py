"""
ai/qwen_service.py
M5 Qwen-0.5B 전용 서비스 루프.

ai-experts 컨테이너가 ai:result에 slm_needed=True로 기록한 항목을 소비하고
Qwen 추론 결과를 ai:emergency에 XADD한다.
"""

import json
import logging
import os
import time
from typing import Any

import redis as _redis

from logic.qwen_05b import QwenLogic


RESULT_STREAM    = "ai:result"
EMERGENCY_STREAM = "ai:emergency"
EMERGENCY_STREAM_MAXLEN = int(os.getenv("EMERGENCY_STREAM_MAXLEN", "3600"))
SLM_MIN_INTERVAL_MS     = int(os.getenv("SLM_MIN_INTERVAL_MS", "3000"))
CONTEXT_WINDOW_MINUTES  = int(os.getenv("CONTEXT_WINDOW_MINUTES", "10"))
MODEL_PATH = os.getenv("MODEL_PATH", "/app/models")
SLM_MODEL  = os.getenv("SLM_MODEL",  "qwen_05b.onnx")

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


def _json_loads(raw: Any) -> dict:
    if not raw:
        return {}
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="ignore")
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _stream_id_ts_ms(stream_id: Any) -> int:
    if isinstance(stream_id, bytes):
        stream_id = stream_id.decode("utf-8", errors="ignore")
    try:
        return int(str(stream_id).split("-")[0])
    except Exception:
        return int(time.time() * 1000)


def _build_context_window(r: _redis.Redis, ts_ms: int) -> dict:
    """ai:emergency에서 최근 CONTEXT_WINDOW_MINUTES 분 위험 이력 수집."""
    since_ms = ts_ms - (CONTEXT_WINDOW_MINUTES * 60 * 1000)
    warning_count = critical_count = 0
    recent_events: list[str] = []
    try:
        entries = r.xrevrange(EMERGENCY_STREAM, count=128)
    except Exception:
        entries = []
    for msg_id, fields in entries:
        if _stream_id_ts_ms(msg_id) < since_ms:
            break
        payload = _json_loads(fields.get(b"data", b""))
        level = payload.get("risk_level", "normal")
        if level == "critical":
            critical_count += 1
        elif level == "warning":
            warning_count += 1
        summary = payload.get("summary")
        if summary and len(recent_events) < 5:
            recent_events.append(summary)
    parts = []
    if critical_count:
        parts.append(f"critical {critical_count}")
    if warning_count:
        parts.append(f"warning {warning_count}")
    if recent_events:
        parts.append("recent=" + " | ".join(recent_events))
    return {
        "window_minutes":       CONTEXT_WINDOW_MINUTES,
        "recent_warning_count":  warning_count,
        "recent_critical_count": critical_count,
        "recent_events":         recent_events,
        "text":                  "; ".join(parts),
    }


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
        "risk_score":             snapshot.get("risk_score", 0.0),
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


def run():
    r = _connect_redis()
    qwen = QwenLogic(os.path.join(MODEL_PATH, SLM_MODEL))
    qwen.redis_client = r
    _warmup_qwen(qwen)

    # 서비스 기동 시점 이후 항목만 소비 (백로그 무시)
    latest = r.xrevrange(RESULT_STREAM, count=1)
    last_id = latest[0][0] if latest else b"0-0"

    last_invoked_ms: int = 0
    _log(logging.INFO, "qwen_service_started", stream=RESULT_STREAM)

    while True:
        try:
            entries = r.xread({RESULT_STREAM: last_id}, count=5, block=2000)
            if not entries:
                continue

            for _, messages in entries:
                for msg_id, fields in messages:
                    last_id = msg_id

                    if fields.get(b"slm_needed") != b"True":
                        continue

                    now_ms = int(time.time() * 1000)
                    if now_ms - last_invoked_ms < SLM_MIN_INTERVAL_MS:
                        continue

                    snapshot = _json_loads(fields.get(b"data", b""))
                    expert_results = snapshot.get("experts")
                    if not expert_results:
                        continue

                    ts_ms = int(snapshot.get("ts_ms", now_ms))
                    context_window = _build_context_window(r, ts_ms)
                    try:
                        fused = qwen.evaluate(expert_results, context_window=context_window)
                        _write_emergency(r, snapshot, fused)
                        last_invoked_ms = now_ms
                        _log(logging.INFO, "qwen_invoked",
                             node_id=snapshot.get("node_id", 0),
                             risk_score=snapshot.get("risk_score", 0.0),
                             risk_level=fused.get("risk_level", "?"))
                    except Exception as exc:
                        _log(logging.ERROR, "qwen_failed", error=str(exc))

        except _redis.exceptions.ConnectionError as exc:
            _log(logging.WARNING, "redis_reconnecting", error=str(exc))
            time.sleep(2)
            r = _connect_redis()
            qwen.redis_client = r

        except Exception as exc:
            _log(logging.ERROR, "qwen_service_error", error=str(exc))


if __name__ == "__main__":
    run()
