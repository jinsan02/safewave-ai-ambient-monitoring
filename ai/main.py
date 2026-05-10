import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import numpy as np
import redis as _redis

from experts import m1_wifi_pose, m2_frenel_vital, m3_ast_base, m4_whisper_small
from logic.qwen_05b import QwenLogic
from utils import TurboQuant


RESULT_STREAM = "ai:result"
EMERGENCY_STREAM = "ai:emergency"
AUDIO_STREAM = "audio:events"
MINUTE_AGG_PREFIX = "agg:minute:"
RESULT_STREAM_MAXLEN = int(os.getenv("RESULT_STREAM_MAXLEN", "36000"))
EMERGENCY_STREAM_MAXLEN = int(os.getenv("EMERGENCY_STREAM_MAXLEN", "3600"))
CONTEXT_WINDOW_MINUTES = int(os.getenv("CONTEXT_WINDOW_MINUTES", "10"))
MINUTE_AGG_TTL_SECONDS = int(os.getenv("MINUTE_AGG_TTL_SECONDS", "3900"))


class AIEngine:
    def __init__(self):
        model_dir = os.getenv("MODEL_PATH", "/app/models")
        self.experts = {
            "fall": m1_wifi_pose.WifiPoseModel(
                os.path.join(model_dir, os.getenv("FALL_DETECTION_MODEL", "m1_wifi_pose_onnx"))
            ),
            "vital": m2_frenel_vital.FrenelVitalModel(
                os.path.join(model_dir, os.getenv("VITAL_SENSING_MODEL", "m2_frenel_vital_onnx"))
            ),
            "activity": m3_ast_base.ActivityClassificationModel(
                os.path.join(model_dir, os.getenv("ACTIVITY_MODEL", "ast_onnx"))
            ),
            "occupancy": m4_whisper_small.WhisperSmallModel(
                os.path.join(model_dir, os.getenv("OCCUPANCY_MODEL", "whisper_onnx"))
            ),
        }
        self.qwen_logic = QwenLogic(
            os.path.join(model_dir, os.getenv("SLM_MODEL", "qwen_05b.onnx"))
        )
        self.turbo_quant = TurboQuant()

    def _run_expert(self, name, data):
        return name, self.experts[name].infer(data)

    def process_data(self, data, context_window=None):
        optimized = self.turbo_quant.optimize(data)
        results = {}

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(self._run_expert, name, optimized) for name in self.experts]
            for future in futures:
                name, output = future.result()
                results[name] = output

        return self.qwen_logic.evaluate(results, context_window=context_window)


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


def _normalize_ts_ms(raw_ts_ms: int, stream_id: Any) -> int:
    if raw_ts_ms >= 1_000_000_000_000:
        return raw_ts_ms
    return _stream_id_ts_ms(stream_id)


def _load_settings(r) -> dict:
    """Redis에서 sys:settings 읽기 — 없으면 기본값 반환."""
    try:
        raw = r.get(b"sys:settings")
        if raw:
            return json.loads(raw.decode())
    except Exception:
        pass
    return {"risk_threshold": 0.6, "active_nodes": [1, 2, 3, 4, 5, 6], "ai_enabled": True}


def _apply_threshold(result: dict, threshold: float) -> dict:
    """동적 임계값으로 risk_level / emergency 재계산."""
    score = float(result.get("risk_score", 0.0))
    if score >= 0.85:
        result["risk_level"] = "critical"
        result["emergency"] = True
    elif score >= threshold:
        result["risk_level"] = "warning"
        result["emergency"] = False
    else:
        result["risk_level"] = "normal"
        result["emergency"] = False
    return result


def _load_recent_audio(r, node_id: int, ts_ms: int) -> dict | None:
    try:
        entries = r.xrevrange(AUDIO_STREAM, count=20)
    except Exception:
        return None

    for msg_id, fields in entries:
        try:
            event_ts_ms = int(fields.get(b"ts_ms", 0)) or _stream_id_ts_ms(msg_id)
        except Exception:
            event_ts_ms = _stream_id_ts_ms(msg_id)
        if abs(ts_ms - event_ts_ms) > 5000:
            continue

        try:
            event_node = int(fields.get(b"node", 0))
        except Exception:
            event_node = 0
        if node_id and event_node not in (0, node_id):
            continue

        payload = fields.get(b"data", b"")
        if payload:
            return _json_loads(payload)

    return None


def _build_context_window(r, ts_ms: int) -> dict:
    since_ms = ts_ms - (CONTEXT_WINDOW_MINUTES * 60 * 1000)
    warning_count = 0
    critical_count = 0
    recent_events: list[str] = []

    try:
        entries = r.xrevrange(EMERGENCY_STREAM, count=128)
    except Exception:
        entries = []

    for msg_id, fields in entries:
        event_ts_ms = _stream_id_ts_ms(msg_id)
        if event_ts_ms < since_ms:
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
        "window_minutes": CONTEXT_WINDOW_MINUTES,
        "recent_warning_count": warning_count,
        "recent_critical_count": critical_count,
        "recent_events": recent_events,
        "text": "; ".join(parts),
    }


def _build_snapshot(ts_ms: int, node_id: int, result: dict, audio_result: dict | None,
                    context_window: dict, ai_enabled: bool) -> dict:
    risk_score = float(result.get("risk_score", 0.0))
    risk_level = result.get("risk_level", "normal")
    emergency = bool(result.get("emergency", False))
    experts = result.get("experts", {})

    return {
        "ts_ms": int(ts_ms),
        "node_id": int(node_id),
        "experts": experts,
        "audio": audio_result,
        "risk": {
            "score": round(risk_score, 4),
            "level": risk_level,
            "emergency": emergency,
        },
        "risk_score": round(risk_score, 4),
        "risk_level": risk_level,
        "emergency": emergency,
        "ai_enabled": ai_enabled,
        "context_window": context_window,
    }


def _build_emergency_summary(snapshot: dict) -> dict:
    experts = snapshot.get("experts", {})
    vital = experts.get("vital", {})
    activity = experts.get("activity", {})

    summary_parts = [
        f"node {snapshot.get('node_id', 0)}",
        snapshot.get("risk_level", "normal"),
        f"score {snapshot.get('risk_score', 0.0):.2f}",
    ]
    if vital.get("heart_rate") is not None:
        summary_parts.append(f"hr {float(vital['heart_rate']):.1f}")
    if vital.get("breathing_rate") is not None:
        summary_parts.append(f"rr {float(vital['breathing_rate']):.1f}")
    if activity.get("activity"):
        summary_parts.append(f"activity {activity['activity']}")

    return {
        "ts_ms": snapshot.get("ts_ms"),
        "node_id": snapshot.get("node_id", 0),
        "risk_score": snapshot.get("risk_score", 0.0),
        "risk_level": snapshot.get("risk_level", "normal"),
        "emergency": snapshot.get("emergency", False),
        "summary": ", ".join(summary_parts),
    }


def _update_minute_aggregate(r, snapshot: dict):
    ts_ms = int(snapshot.get("ts_ms", int(time.time() * 1000)))
    minute_key = ts_ms // 60000
    key = f"{MINUTE_AGG_PREFIX}{minute_key}"

    experts = snapshot.get("experts", {})
    vital = experts.get("vital", {})
    pipe = r.pipeline()
    pipe.hset(key, "ts", int(minute_key * 60))
    pipe.hincrbyfloat(key, "risk_sum", float(snapshot.get("risk_score", 0.0)))
    pipe.hincrby(key, "risk_count", 1)
    if vital.get("heart_rate") is not None:
        pipe.hincrbyfloat(key, "heart_sum", float(vital["heart_rate"]))
        pipe.hincrby(key, "heart_count", 1)
    if vital.get("breathing_rate") is not None:
        pipe.hincrbyfloat(key, "breathing_sum", float(vital["breathing_rate"]))
        pipe.hincrby(key, "breathing_count", 1)
    pipe.expire(key, MINUTE_AGG_TTL_SECONDS)
    pipe.execute()


def _write_snapshot(r, snapshot: dict):
    payload = json.dumps(snapshot, ensure_ascii=False)
    r.xadd(RESULT_STREAM, {"data": payload}, maxlen=RESULT_STREAM_MAXLEN, approximate=True)
    _update_minute_aggregate(r, snapshot)

    if snapshot.get("risk_level") in {"warning", "critical"}:
        summary = _build_emergency_summary(snapshot)
        r.xadd(
            EMERGENCY_STREAM,
            {"data": json.dumps(summary, ensure_ascii=False)},
            maxlen=EMERGENCY_STREAM_MAXLEN,
            approximate=True,
        )


def _connect_redis(redis_host: str, redis_port: int):
    while True:
        try:
            rc = _redis.Redis(host=redis_host, port=redis_port, decode_responses=False)
            rc.ping()
            print(f"[ai] connected to Redis at {redis_host}:{redis_port}", flush=True)
            return rc
        except _redis.exceptions.ConnectionError as exc:
            print(f"[ai] Redis not ready ({exc}), retry in 2s...", flush=True)
            time.sleep(2)


if __name__ == "__main__":
    redis_host = os.getenv("REDIS_HOST", "db")
    redis_port = int(os.getenv("REDIS_PORT", 6379))
    r = _connect_redis(redis_host, redis_port)
    ai_engine = AIEngine()
    print("[ai] starting inference loop — waiting for CSI data on stream 'csi:raw'", flush=True)

    last_id = "$"
    while True:
        try:
            settings = _load_settings(r)
            active_nodes = set(settings.get("active_nodes", [1, 2, 3, 4, 5, 6]))
            threshold = float(settings.get("risk_threshold", 0.6))
            ai_enabled = bool(settings.get("ai_enabled", True))

            entries = r.xread({"csi:raw": last_id}, count=10, block=1000)
            if not entries:
                continue

            for _stream, messages in entries:
                for msg_id, fields in messages:
                    try:
                        node_id = int(fields.get(b"node", 0))
                    except Exception:
                        node_id = 0
                    try:
                        ts_ms = _normalize_ts_ms(int(fields.get(b"ts_ms", 0)), msg_id)
                    except Exception:
                        ts_ms = _stream_id_ts_ms(msg_id)

                    if active_nodes and node_id not in active_nodes and node_id != 0:
                        last_id = msg_id
                        continue

                    context_window = _build_context_window(r, ts_ms)
                    audio_result = _load_recent_audio(r, node_id, ts_ms)

                    if not ai_enabled:
                        result = {
                            "risk_level": "normal",
                            "risk_score": 0.0,
                            "emergency": False,
                            "experts": {},
                        }
                        snapshot = _build_snapshot(ts_ms, node_id, result, audio_result, context_window, False)
                        _write_snapshot(r, snapshot)
                        last_id = msg_id
                        continue

                    raw = fields.get(b"data", b"")
                    if raw:
                        input_data = np.frombuffer(raw, dtype=np.float32)
                    else:
                        input_data = np.sin(np.linspace(0.0, 8.0 * np.pi, 512)).astype(np.float32)

                    result = ai_engine.process_data(input_data, context_window=context_window)
                    result = _apply_threshold(result, threshold)

                    snapshot = _build_snapshot(ts_ms, node_id, result, audio_result, context_window, True)
                    _write_snapshot(r, snapshot)
                    print(snapshot, flush=True)
                    last_id = msg_id

        except _redis.exceptions.ConnectionError as exc:
            print(f"[ai] Redis lost: {exc} — reconnecting...", flush=True)
            r = _connect_redis(redis_host, redis_port)
        except Exception as exc:
            print(f"[ai] error: {exc}", flush=True)
            time.sleep(1)