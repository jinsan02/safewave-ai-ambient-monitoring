import json
import logging
import os
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Any

import numpy as np
import redis as _redis

from experts import m1_wifi_pose, m2_frenel_vital, m3_ast_base, m4_whisper_small
from mqtt_helper import make_client, publish_json, topic
from logic.emergency_score import compute_emergency_score
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
RESULT_STREAM_MAXLEN = int(os.getenv("RESULT_STREAM_MAXLEN", "1_800_000"))  # 5노드 × 100Hz × 1hr
EMERGENCY_STREAM_MAXLEN = int(os.getenv("EMERGENCY_STREAM_MAXLEN", "3600"))
CONTEXT_WINDOW_MINUTES = int(os.getenv("CONTEXT_WINDOW_MINUTES", "10"))
MINUTE_AGG_TTL_SECONDS = int(os.getenv("MINUTE_AGG_TTL_SECONDS", "3600"))
EXPERT_LATEST_TTL_SECONDS = int(os.getenv("EXPERT_LATEST_TTL_SECONDS", "3600"))
M3_AUDIO_WINDOW_MS = int(os.getenv("M3_AUDIO_WINDOW_MS", "3000"))
M4_AUDIO_WINDOW_MS = int(os.getenv("M4_AUDIO_WINDOW_MS", "5000"))
SLM_MIN_INTERVAL_MS = int(os.getenv("SLM_MIN_INTERVAL_MS", "3000"))
STREAM_START_ID = os.getenv("CSI_STREAM_START_ID", "0-0")
M2_CSI_WINDOW_FRAMES = int(os.getenv("M2_CSI_WINDOW_FRAMES", "300"))
# 300프레임 = 3초 @ 100Hz; 호흡(0.1Hz) 감지 최소 10초 필요하나 실측 튜닝 전 초기값
CSI_BACKLOG_SKIP_STREAK = int(os.getenv("CSI_BACKLOG_SKIP_STREAK", "5"))
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
                os.getenv("M3_ENV_SOUND_MODEL", os.getenv("ACTIVITY_MODEL", "ast_onnx")),
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
        self.turbo_quant = TurboQuant()
        self._executor = ThreadPoolExecutor(max_workers=4)
        # CUDA JIT 첫 추론 워밍업 (수 초 소요, 이후 infer는 <1ms)
        _log(logging.INFO, "engine_init_step", step="gpu_warmup")
        _warmup = np.sin(np.linspace(0.0, 8.0 * np.pi, 512)).astype(np.float32)
        _warmup_per_expert = {
            "fall":      _warmup,
            "vital":     {"resp": _warmup, "heart": _warmup},  # M2 런타임 포맷과 일치
            "env_sound": _warmup,
            "speech_ko": _warmup,
        }
        for _name, _expert in self.experts.items():
            try:
                _expert.infer(_warmup_per_expert[_name])
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
                "env_sound_label": "silence",    # 하위 호환
                "label": "silence",
                "env_sound_confidence": 0.0,     # 하위 호환
                "confidence": 0.0,
                "env_sound_source": "no-audio",  # 하위 호환
                "source": "no-audio",
                "activity": "silence",
                "activity_confidence": 0.0,
                "infer_confidence": 0.0,
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
                "infer_confidence": 0.0,
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
        # speech_ko(Whisper)는 decoder autoregressive 생성으로 다른 모델보다 느림 — 5s 허용
        _timeouts = {name: max(0.1, EXPERT_INFER_TIMEOUT_MS / 1000.0) for name in futures}
        _timeouts["speech_ko"] = 10.0
        for name, future in futures.items():
            timeout_sec = _timeouts[name]
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


_settings_cache: dict = {}
_settings_cache_ts: float = 0.0
_SETTINGS_CACHE_TTL_S: float = 1.0


def _load_cached_settings(r) -> dict:
    """1초 TTL 캐시 — xread 배치마다 Redis GET을 초당 1회로 줄임."""
    global _settings_cache, _settings_cache_ts
    if time.time() - _settings_cache_ts < _SETTINGS_CACHE_TTL_S:
        return _settings_cache
    _settings_cache = _load_settings(r)
    _settings_cache_ts = time.time()
    return _settings_cache


_ctx_cache: dict = {}
_ctx_cache_ts: float = 0.0
_CTX_CACHE_TTL_S: float = 1.0


def _load_cached_context_window(r, ts_ms: int) -> dict:
    """1초 TTL 캐시 — 100Hz 루프에서 XREVRANGE를 초당 1회로 줄임."""
    global _ctx_cache, _ctx_cache_ts
    if time.time() - _ctx_cache_ts < _CTX_CACHE_TTL_S:
        return _ctx_cache
    _ctx_cache = _build_context_window(r, ts_ms)
    _ctx_cache_ts = time.time()
    return _ctx_cache


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


def _build_expert_inputs(raw_data, resp_data, heart_data,
                         audio_events: list[dict]) -> tuple[dict, dict | None]:
    latest_audio = audio_events[-1] if audio_events else None
    expert_inputs = {
        "fall":      raw_data,                                                       # M1 입력
        "vital":     {"resp": resp_data, "heart": heart_data},                      # M2 입력
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
        "slm_needed": bool(result.get("slm_needed", False)),
        "is_outlier": bool(result.get("is_outlier", False)),
        "correlated_with_history": bool(result.get("correlated_with_history", False)),
        "qwen_reason": result.get("qwen_reason") or result.get("slm_skip_reason"),
        "slm_mode": result.get("slm_mode"),
        "model_latency_ms": {
            "qwen": result.get("qwen_infer_ms")
        },
        "expert_latency_ms": result.get("expert_latency_ms", {}),
        "emergency_breakdown": result.get("emergency_breakdown"),
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
    r.xadd(
        RESULT_STREAM,
        {
            "data":       payload,
            "slm_needed": "True" if snapshot.get("slm_needed") else "False",
        },
        maxlen=RESULT_STREAM_MAXLEN,
        approximate=True,
    )
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




def _decode_csi_field(raw: Any, expected: int = 64) -> np.ndarray:
    """csi:raw 단일 블록 필드(기본 64 float32) 디코딩."""
    if not raw or not isinstance(raw, (bytes, bytearray, memoryview)):
        return np.zeros(expected, dtype=np.float32)
    raw_bytes = bytes(raw)
    usable = (len(raw_bytes) // 4) * 4
    if usable <= 0:
        return np.zeros(expected, dtype=np.float32)
    arr = np.frombuffer(raw_bytes[:usable], dtype=np.float32)
    if arr.size < expected:
        return np.pad(arr, (0, expected - arr.size)).astype(np.float32)
    return arr[:expected].copy()


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
    _backlog_streak = 0
    _node_resp_buf:  dict[int, deque] = {}
    _node_heart_buf: dict[int, deque] = {}
    while True:
        try:
            settings = _load_cached_settings(r)
            active_nodes = set(settings.get("active_nodes", [1, 2, 3, 4, 5, 6]))
            threshold = float(settings.get("risk_threshold", 0.6))
            ai_enabled = bool(settings.get("ai_enabled", True))

            entries = r.xread({"csi:raw": last_id}, count=10, block=1000)
            if not entries:
                _backlog_streak = 0
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
                        context_window = _load_cached_context_window(r, ts_ms)
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

                    # 3-필드 읽기. 구형 단일 data 필드는 지원하지 않음.
                    raw_data   = _decode_csi_field(fields.get(b"data_raw"))
                    resp_data  = _decode_csi_field(fields.get(b"data_resp"))
                    heart_data = _decode_csi_field(fields.get(b"data_heart"))
                    if b"data_raw" not in fields:
                        _log(logging.WARNING, "csi_missing_fields",
                             node_id=node_id, msg_id=str(msg_id),
                             present=list(k.decode() for k in fields if k != b""))

                    # M2 시간축 누적: per-node deque, 64채널 평균 → (N,) 시간 시리즈
                    _node_resp_buf.setdefault(node_id, deque(maxlen=M2_CSI_WINDOW_FRAMES)).append(resp_data)
                    _node_heart_buf.setdefault(node_id, deque(maxlen=M2_CSI_WINDOW_FRAMES)).append(heart_data)
                    resp_series  = np.mean(np.stack(_node_resp_buf[node_id]),  axis=1)
                    heart_series = np.mean(np.stack(_node_heart_buf[node_id]), axis=1)

                    context_window = _load_cached_context_window(r, ts_ms)
                    audio_events = _load_recent_audio_events(r, node_id, ts_ms, M4_AUDIO_WINDOW_MS)
                    expert_inputs, audio_result = _build_expert_inputs(
                        raw_data, resp_series, heart_series, audio_events
                    )
                    expert_results, expert_latency_ms = ai_engine.process_experts(
                        raw_data,
                        expert_inputs=expert_inputs,
                    )
                    for expert_name, output in expert_results.items():
                        write_output = output
                        if expert_name == "env_sound":
                            audio_in  = expert_inputs.get("env_sound") or {}
                            audio_ts  = audio_in.get("ts_ms")
                            audio_dur = audio_in.get("duration_ms")
                            write_output = {
                                **output,
                                "audio_ts_ms": audio_ts,
                                "audio_ts_start_ms": (audio_ts - audio_dur)
                                    if (audio_ts is not None and audio_dur is not None) else None,
                                "audio_duration_ms": audio_dur,
                                "audio_window_ms": audio_in.get("window_ms"),
                            }
                        _write_expert_latest(
                            r,
                            expert_name,
                            node_id,
                            ts_ms,
                            write_output,
                            expert_latency_ms.get(expert_name, 0.0),
                        )

                    emg_score, emg_breakdown = compute_emergency_score(expert_results)
                    invoke_slm = emg_score >= threshold
                    now_ms = int(time.time() * 1000)
                    if invoke_slm and (now_ms - last_slm_invoked_at_ms) < SLM_MIN_INTERVAL_MS:
                        invoke_slm = False
                    if invoke_slm:
                        last_slm_invoked_at_ms = now_ms

                    skip_reason = None if invoke_slm else (
                        "below_threshold" if emg_score < threshold else "cooldown"
                    )
                    result = {
                        "risk_score":          round(float(emg_score), 4),
                        "risk_level":          "normal",   # _apply_threshold가 덮어씀
                        "emergency":           False,       # _apply_threshold가 덮어씀
                        "experts":             expert_results,
                        "context_used":        False,
                        "qwen_infer_ms":       None,
                        "slm_invoked":         False,       # ai-qwen 컨테이너가 채움
                        "slm_needed":          invoke_slm,  # ai-qwen 트리거 신호
                        "slm_skip_reason":     skip_reason,
                        "emergency_breakdown": emg_breakdown,
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

            # 백로그 스킵: full batch가 연속되면 최신으로 점프
            total_batch = sum(len(m) for _, m in entries)
            if total_batch >= 10:
                _backlog_streak += 1
                if _backlog_streak >= CSI_BACKLOG_SKIP_STREAK:
                    tail = r.xrevrange("csi:raw", count=1)
                    if tail:
                        last_id = tail[0][0]
                        _log(logging.WARNING, "csi_backlog_skipped",
                             jumped_to=str(last_id), streak=_backlog_streak)
                    _backlog_streak = 0
            else:
                _backlog_streak = 0

        except _redis.exceptions.ConnectionError as exc:
            _log(logging.ERROR, "redis_lost", error=str(exc), action="reconnect")
            r = _connect_redis(redis_host, redis_port)
        except Exception as exc:
            _log(logging.ERROR, "inference_loop_error", error=str(exc))
            time.sleep(1)