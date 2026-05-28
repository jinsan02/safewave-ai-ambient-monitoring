import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Any

import numpy as np
import redis as _redis

from experts import m1_wifi_pose, m2_frenel_vital, m3_ast_base, m4_whisper_small
from mqtt_helper import make_client, publish_json, topic
from logic.qwen_05b import QwenLogic
from utils import TurboQuant


RESULT_STREAM = "ai:result"
EMERGENCY_STREAM = "ai:emergency"
AUDIO_STREAM = "audio:events"
MINUTE_AGG_PREFIX = "agg:minute:"
EXPERT_LATEST_KEYS = {
    "fall": "ai:m1:latest",
    "vital": "ai:m2:latest",
    "env_sound": "ai:m3:latest",
    "speech_ko": "ai:m4:latest",
}
RESULT_STREAM_MAXLEN = int(os.getenv("RESULT_STREAM_MAXLEN", "36000"))
EMERGENCY_STREAM_MAXLEN = int(os.getenv("EMERGENCY_STREAM_MAXLEN", "3600"))
CONTEXT_WINDOW_MINUTES = int(os.getenv("CONTEXT_WINDOW_MINUTES", "10"))
MINUTE_AGG_TTL_SECONDS = int(os.getenv("MINUTE_AGG_TTL_SECONDS", "3600"))
EXPERT_LATEST_TTL_SECONDS = int(os.getenv("EXPERT_LATEST_TTL_SECONDS", "3600"))
M3_AUDIO_WINDOW_MS = int(os.getenv("M3_AUDIO_WINDOW_MS", "3000"))
M4_AUDIO_WINDOW_MS = int(os.getenv("M4_AUDIO_WINDOW_MS", "5000"))
SLM_MIN_INTERVAL_MS = int(os.getenv("SLM_MIN_INTERVAL_MS", "3000"))
STREAM_START_ID = os.getenv("CSI_STREAM_START_ID", "0-0")
EXPERT_INFER_TIMEOUT_MS = int(os.getenv("EXPERT_INFER_TIMEOUT_MS", "1000"))
MQTT_ENABLED = os.getenv("MQTT_ENABLED", "1").lower() not in {"0", "false", "no"}
MQTT_HOST = os.getenv("MQTT_HOST", "mqtt")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_CLIENT_ID = os.getenv("MQTT_CLIENT_ID", "rp5-ai")
MQTT_RESULT_TOPIC = topic("ai/result")
MQTT_EMERGENCY_TOPIC = topic("ai/emergency")
MQTT_FEEDBACK_TOPIC = topic("feedback")
MQTT_FEEDBACK_REDIS_KEY = os.getenv("MQTT_FEEDBACK_REDIS_KEY", "mqtt:feedback:last")


LOGGER = logging.getLogger("rp5.ai")
if not LOGGER.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(message)s"))
    LOGGER.addHandler(_handler)
LOGGER.setLevel(logging.INFO)
LOGGER.propagate = False


def _log(level: int, event: str, **fields):
    payload = {
        "service": "ai",
        "event": event,
        "ts_ms": int(time.time() * 1000),
    }
    payload.update(fields)
    LOGGER.log(level, json.dumps(payload, ensure_ascii=False))


def _init_mqtt(redis_client):
    if not MQTT_ENABLED:
        return None

    client = make_client(MQTT_CLIENT_ID)

    def on_connect(_client, _userdata, _flags, rc):
        _log(logging.INFO, "mqtt_connected", rc=rc, host=MQTT_HOST, port=MQTT_PORT)
        _client.subscribe(MQTT_FEEDBACK_TOPIC)

    def on_message(_client, _userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8", errors="ignore"))
        except Exception:
            return
        if msg.topic == MQTT_FEEDBACK_TOPIC:
            try:
                redis_client.set(MQTT_FEEDBACK_REDIS_KEY, json.dumps(payload, ensure_ascii=False), ex=3600)
            except Exception as exc:
                _log(logging.WARNING, "mqtt_feedback_store_failed", error=str(exc))

    client.on_connect = on_connect
    client.on_message = on_message
    try:
        client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
        client.loop_start()
        return client
    except Exception as exc:
        _log(logging.WARNING, "mqtt_connect_failed", error=str(exc), host=MQTT_HOST, port=MQTT_PORT)
        return None


def _publish_result_mqtt(client, snapshot: dict):
    if client is None:
        return

    payload = {
        "ts_ms": snapshot.get("ts_ms"),
        "node_id": snapshot.get("node_id"),
        "risk_level": snapshot.get("risk_level"),
        "risk_score": snapshot.get("risk_score"),
        "slm_invoked": snapshot.get("slm_invoked", False),
        "reason": snapshot.get("qwen_reason"),
        "is_outlier": snapshot.get("is_outlier", False),
        "correlated_with_history": snapshot.get("correlated_with_history", False),
    }
    publish_json(client, MQTT_RESULT_TOPIC, payload)
    if snapshot.get("risk_level") in {"warning", "critical"}:
        publish_json(client, MQTT_EMERGENCY_TOPIC, payload)


class AIEngine:
    def __init__(self):
        model_dir = os.getenv("MODEL_PATH", "/app/models")
        _log(logging.INFO, "engine_init_started", model_dir=model_dir)
        _log(logging.INFO, "engine_init_step", step="m1_fall")
        fall_model = m1_wifi_pose.WifiPoseModel(
            os.path.join(model_dir, os.getenv("FALL_DETECTION_MODEL", "m1_wifi_pose_onnx"))
        )
        _log(logging.INFO, "engine_init_step", step="m2_vital")
        vital_model = m2_frenel_vital.FrenelVitalModel(
            os.path.join(model_dir, os.getenv("VITAL_SENSING_MODEL", "m2_frenel_vital_onnx"))
        )
        _log(logging.INFO, "engine_init_step", step="m3_env_sound")
        env_sound_model = m3_ast_base.EnvSoundAnalysisModel(
            os.path.join(
                model_dir,
                os.getenv("M3_ENV_SOUND_MODEL", os.getenv("ACTIVITY_MODEL", "ast_hf")),
            )
        )
        _log(logging.INFO, "engine_init_step", step="m4_speech_ko")
        speech_model = m4_whisper_small.WhisperSmallModel(
            os.path.join(
                model_dir,
                os.getenv("M4_KO_STT_MODEL", os.getenv("OCCUPANCY_MODEL", "whisper_onnx")),
            )
        )

        self.experts = {
            "fall": fall_model,
            "vital": vital_model,
            "env_sound": env_sound_model,
            "speech_ko": speech_model,
        }
        _log(logging.INFO, "engine_init_step", step="qwen_logic")
        self.qwen_logic = QwenLogic(
            os.path.join(model_dir, os.getenv("SLM_MODEL", "qwen_05b.onnx"))
        )
        self.turbo_quant = TurboQuant()
        self._executor = ThreadPoolExecutor(max_workers=4)
        # CUDA JIT 첫 추론 워밍업 (수 초 소요, 이후 infer는 <1ms)
        _log(logging.INFO, "engine_init_step", step="gpu_warmup")
        _warmup = np.sin(np.linspace(0.0, 8.0 * np.pi, 512)).astype(np.float32)
        for _name, _expert in self.experts.items():
            try:
                _expert.infer(_warmup)
            except Exception as _e:
                _log(logging.WARNING, "warmup_failed", expert=_name, error=str(_e))
        _log(logging.INFO, "engine_init_completed")

    def _extract_audio_signal(self, audio_data):
        if not isinstance(audio_data, dict):
            return audio_data
        for key in ("waveform", "samples", "audio", "pcm"):
            value = audio_data.get(key)
            if value is not None:
                return value
        return None

    def _empty_output(self, name):
        if name == "env_sound":
            return {
                "env_sound_label": "silence",
                "env_sound_confidence": 0.0,
                "env_sound_source": "no-audio",
                "activity": "silence",
                "activity_confidence": 0.0,
            }
        if name == "speech_ko":
            return {
                "transcript_ko": "",
                "speech_detected": False,
                "stt_confidence": 0.0,
                "stt_source": "no-audio",
                "language": "ko",
                "keywords": [],
                "occupied": False,
                "occupancy_score": 0.0,
            }
        return {}

    def _run_expert(self, name, data, expert_inputs=None):
        started = time.perf_counter()
        expert_input = expert_inputs.get(name, data) if expert_inputs else data
        if expert_input is None:
            if name in {"env_sound", "speech_ko"}:
                output = self._empty_output(name)
                return name, output, (time.perf_counter() - started) * 1000.0
            expert_input = data
        if name == "env_sound" and isinstance(expert_input, dict):
            extracted = self._extract_audio_signal(expert_input)
            if extracted is not None:
                expert_input = extracted
        output = self.experts[name].infer(expert_input)
        return name, output, (time.perf_counter() - started) * 1000.0

    def process_experts(self, data, expert_inputs=None):
        optimized = self.turbo_quant.optimize(data)
        results = {}
        latency_ms: dict[str, float] = {}

        futures = {
            name: self._executor.submit(self._run_expert, name, optimized, expert_inputs)
            for name in self.experts
        }
        timeout_sec = max(0.1, EXPERT_INFER_TIMEOUT_MS / 1000.0)
        for name, future in futures.items():
            try:
                result_name, output, elapsed_ms = future.result(timeout=timeout_sec)
                results[result_name] = output
                latency_ms[result_name] = round(float(elapsed_ms), 2)
            except FutureTimeoutError:
                results[name] = self._empty_output(name)
                latency_ms[name] = float(EXPERT_INFER_TIMEOUT_MS)
                _log(logging.WARNING, "expert_timeout", expert=name, timeout_ms=EXPERT_INFER_TIMEOUT_MS)
            except Exception as exc:
                results[name] = self._empty_output(name)
                latency_ms[name] = 0.0
                _log(logging.ERROR, "expert_failure", expert=name, error=str(exc))

        return results, latency_ms

    def fuse_results(self, expert_results, context_window=None):
        fused = self.qwen_logic.evaluate(expert_results, context_window=context_window)
        return fused

    def process_data(self, data, expert_inputs=None, context_window=None):
        results, latency_ms = self.process_experts(data, expert_inputs=expert_inputs)
        fused = self.fuse_results(results, context_window=context_window)
        fused["expert_latency_ms"] = latency_ms
        return fused


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
    stream_ts_ms = _stream_id_ts_ms(stream_id)
    if raw_ts_ms < 1_000_000_000_000:
        return stream_ts_ms

    # Raw ts가 현재 stream 시각과 지나치게 벌어지면 stream ts를 사용한다.
    if abs(raw_ts_ms - stream_ts_ms) > 15_000:
        return stream_ts_ms
    return raw_ts_ms


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if isinstance(value, dict):
            for key in ("value", "score", "mean", "avg"):
                if key in value:
                    return float(value[key])
            return default
        if isinstance(value, (list, tuple)):
            if not value:
                return default
            return float(value[0])
        return float(value)
    except Exception:
        return default


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
    score = _safe_float(result.get("risk_score", 0.0), 0.0)
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


def _load_recent_audio_events(r, node_id: int, ts_ms: int, window_ms: int) -> list[dict]:
    try:
        entries = r.xrevrange(AUDIO_STREAM, count=64)
    except Exception:
        return []

    since_ms = ts_ms - window_ms
    matched: list[dict] = []

    for msg_id, fields in entries:
        event_ts_ms = _stream_id_ts_ms(msg_id)
        if event_ts_ms < since_ms:
            break

        try:
            event_node = int(fields.get(b"node", 0))
        except Exception:
            event_node = 0
        if node_id and event_node not in (0, node_id):
            continue

        payload = _json_loads(fields.get(b"data", b""))
        if payload:
            payload["ts_ms"] = event_ts_ms
            matched.append(payload)

    matched.reverse()
    return matched


def _merge_audio_window(events: list[dict], window_ms: int) -> dict | None:
    if not events:
        return None

    last_event = events[-1]
    sample_rate = int(last_event.get("sample_rate", 16000) or 16000)
    channels = int(last_event.get("channels", 1) or 1)
    max_samples = int(sample_rate * (window_ms / 1000.0))
    peak_db = -120.0
    waveforms = []

    for event in events:
        peak_db = max(peak_db, float(event.get("peak_db", -120.0) or -120.0))
        waveform = event.get("waveform")
        if waveform is None:
            continue
        array = np.asarray(waveform, dtype=np.float32).reshape(-1)
        if array.size:
            waveforms.append(array)

    if not waveforms:
        return None

    merged = np.concatenate(waveforms)
    if max_samples > 0 and merged.size > max_samples:
        merged = merged[-max_samples:]

    return {
        "sample_rate": sample_rate,
        "channels": channels,
        "duration_ms": int(merged.size * 1000 / sample_rate),
        "peak_db": round(float(peak_db), 2),
        "waveform": merged,
        "window_ms": window_ms,
        "ts_ms": int(last_event.get("ts_ms", int(time.time() * 1000))),
    }


def _build_expert_inputs(default_data, audio_events: list[dict]) -> tuple[dict, dict | None]:
    latest_audio = audio_events[-1] if audio_events else None
    expert_inputs = {
        "env_sound": _merge_audio_window(audio_events, M3_AUDIO_WINDOW_MS),
        "speech_ko": _merge_audio_window(audio_events, M4_AUDIO_WINDOW_MS) or latest_audio,
    }
    return expert_inputs, latest_audio


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
    risk_score = _safe_float(result.get("risk_score", 0.0), 0.0)
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
        "slm_invoked": bool(result.get("slm_invoked", False)),
        "is_outlier": bool(result.get("is_outlier", False)),
        "correlated_with_history": bool(result.get("correlated_with_history", False)),
        "qwen_reason": result.get("qwen_reason") or result.get("slm_skip_reason"),
        "slm_mode": result.get("slm_mode"),
        "model_latency_ms": {
            "qwen": result.get("qwen_infer_ms")
        },
        "expert_latency_ms": result.get("expert_latency_ms", {}),
    }


def _build_emergency_summary(snapshot: dict) -> dict:
    experts = snapshot.get("experts", {})
    vital = experts.get("vital", {})
    env_sound = experts.get("env_sound", {})
    speech_ko = experts.get("speech_ko", {})

    summary_parts = [
        f"node {snapshot.get('node_id', 0)}",
        snapshot.get("risk_level", "normal"),
        f"score {snapshot.get('risk_score', 0.0):.2f}",
    ]
    heart_rate = _safe_float(vital.get("heart_rate"), default=-1.0)
    breathing_rate = _safe_float(vital.get("breathing_rate"), default=-1.0)
    if heart_rate >= 0.0:
        summary_parts.append(f"hr {heart_rate:.1f}")
    if breathing_rate >= 0.0:
        summary_parts.append(f"rr {breathing_rate:.1f}")
    if env_sound.get("env_sound_label"):
        summary_parts.append(f"sound {env_sound['env_sound_label']}")
    transcript = speech_ko.get("transcript_ko", "")
    if transcript:
        summary_parts.append(f"speech {transcript[:24]}")

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
    pipe.hincrbyfloat(key, "risk_sum", _safe_float(snapshot.get("risk_score", 0.0), 0.0))
    pipe.hincrby(key, "risk_count", 1)
    if snapshot.get("slm_invoked"):
        pipe.hincrby(key, "slm_invoked_count", 1)
    heart_rate = _safe_float(vital.get("heart_rate"), default=-1.0)
    breathing_rate = _safe_float(vital.get("breathing_rate"), default=-1.0)
    if heart_rate >= 0.0:
        pipe.hincrbyfloat(key, "heart_sum", heart_rate)
        pipe.hincrby(key, "heart_count", 1)
    if breathing_rate >= 0.0:
        pipe.hincrbyfloat(key, "breathing_sum", breathing_rate)
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


def _write_expert_latest(r, expert_name: str, node_id: int, ts_ms: int, output: dict, latency_ms: float):
    key = EXPERT_LATEST_KEYS.get(expert_name)
    if not key:
        return

    payload = {
        "ts_ms": int(ts_ms),
        "node_id": int(node_id),
        "expert": expert_name,
        "latency_ms": round(float(latency_ms), 2),
        "data": output,
    }
    r.set(key, json.dumps(payload, ensure_ascii=False), ex=EXPERT_LATEST_TTL_SECONDS)


def _collect_expert_risk_scores(expert_results: dict) -> list[float]:
    scores: list[float] = []
    for output in expert_results.values():
        if not isinstance(output, dict):
            continue
        for key in (
            "risk_score",
            "fall_score",
            "env_sound_confidence",
            "stt_confidence",
            "occupancy_score",
            "activity_confidence",
        ):
            if key in output:
                val = _safe_float(output.get(key), default=-1.0)
                if val >= 0.0:
                    scores.append(float(np.clip(val, 0.0, 1.0)))
    return scores


def _aggregate_expert_risk(expert_results: dict) -> float:
    scores = _collect_expert_risk_scores(expert_results)
    if not scores:
        return 0.0
    return float(np.clip(max(scores), 0.0, 1.0))


def _should_invoke_slm(expert_results: dict, threshold: float) -> tuple[bool, float]:
    max_expert_score = _aggregate_expert_risk(expert_results)
    return max_expert_score >= threshold, max_expert_score


def _decode_csi_payload(raw: Any) -> np.ndarray:
    if not raw:
        return np.sin(np.linspace(0.0, 8.0 * np.pi, 512)).astype(np.float32)
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        return np.sin(np.linspace(0.0, 8.0 * np.pi, 512)).astype(np.float32)
    raw_bytes = bytes(raw)
    usable = (len(raw_bytes) // 4) * 4
    if usable <= 0:
        return np.sin(np.linspace(0.0, 8.0 * np.pi, 512)).astype(np.float32)
    data = np.frombuffer(raw_bytes[:usable], dtype=np.float32)
    # 비정상적으로 짧은 트리거 payload는 전문가 입력으로 부적합하므로 기본 파형으로 대체.
    if data.size < 64:
        return np.sin(np.linspace(0.0, 8.0 * np.pi, 512)).astype(np.float32)
    return data


def _connect_redis(redis_host: str, redis_port: int):
    while True:
        try:
            rc = _redis.Redis(host=redis_host, port=redis_port, decode_responses=False)
            rc.ping()
            _log(logging.INFO, "redis_connected", host=redis_host, port=redis_port)
            return rc
        except _redis.exceptions.ConnectionError as exc:
            _log(logging.WARNING, "redis_not_ready", error=str(exc), retry_in_sec=2)
            time.sleep(2)


if __name__ == "__main__":
    redis_host = os.getenv("REDIS_HOST", "db")
    redis_port = int(os.getenv("REDIS_PORT", 6379))
    r = _connect_redis(redis_host, redis_port)
    mqtt_client = _init_mqtt(r)
    ai_engine = AIEngine()
    _log(logging.INFO, "inference_loop_started", stream="csi:raw")

    last_id = STREAM_START_ID
    last_slm_invoked_at_ms = 0
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

                    if not ai_enabled:
                        context_window = _build_context_window(r, ts_ms)
                        audio_result = _load_recent_audio(r, node_id, ts_ms)
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
                    input_data = _decode_csi_payload(raw)

                    context_window = _build_context_window(r, ts_ms)
                    audio_events = _load_recent_audio_events(r, node_id, ts_ms, M4_AUDIO_WINDOW_MS)
                    expert_inputs, audio_result = _build_expert_inputs(input_data, audio_events)
                    expert_results, expert_latency_ms = ai_engine.process_experts(
                        input_data,
                        expert_inputs=expert_inputs,
                    )
                    for expert_name, output in expert_results.items():
                        _write_expert_latest(
                            r,
                            expert_name,
                            node_id,
                            ts_ms,
                            output,
                            expert_latency_ms.get(expert_name, 0.0),
                        )

                    invoke_slm, max_expert_score = _should_invoke_slm(expert_results, threshold)
                    now_ms = int(time.time() * 1000)
                    if invoke_slm and (now_ms - last_slm_invoked_at_ms) < SLM_MIN_INTERVAL_MS:
                        invoke_slm = False

                    if invoke_slm:
                        result = ai_engine.fuse_results(expert_results, context_window=context_window)
                        result["slm_invoked"] = True
                        last_slm_invoked_at_ms = now_ms
                    else:
                        skip_reason = "below_threshold"
                        if max_expert_score >= threshold:
                            skip_reason = "cooldown"
                        result = {
                            "emergency": False,
                            "risk_level": "normal",
                            "risk_score": round(float(max_expert_score), 4),
                            "experts": expert_results,
                            "context_used": False,
                            "qwen_infer_ms": None,
                            "slm_invoked": False,
                            "slm_skip_reason": skip_reason,
                        }

                    result["expert_latency_ms"] = expert_latency_ms
                    result = _apply_threshold(result, threshold)

                    snapshot = _build_snapshot(ts_ms, node_id, result, audio_result, context_window, True)
                    _write_snapshot(r, snapshot)
                    _publish_result_mqtt(mqtt_client, snapshot)
                    _log(
                        logging.INFO,
                        "snapshot_written",
                        node_id=node_id,
                        slm_invoked=result.get("slm_invoked", False),
                        risk_level=snapshot.get("risk_level"),
                        risk_score=snapshot.get("risk_score"),
                        qwen_latency_ms=result.get("qwen_infer_ms"),
                        expert_latency_ms=result.get("expert_latency_ms", {}),
                    )
                    last_id = msg_id

        except _redis.exceptions.ConnectionError as exc:
            _log(logging.ERROR, "redis_lost", error=str(exc), action="reconnect")
            r = _connect_redis(redis_host, redis_port)
        except Exception as exc:
            _log(logging.ERROR, "inference_loop_error", error=str(exc))
            time.sleep(1)