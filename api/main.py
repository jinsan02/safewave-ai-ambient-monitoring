"""
api/main.py
갤럭시 플립 4 앱 연동 FastAPI 서버.
- GET  /status              : 최신 AI 통합 스냅샷
- GET  /logs                : 최근 N건 이력
- GET  /history             : 응급/경고 요약 이력
- GET  /settings            : 시스템 설정 조회
- POST /settings            : 시스템 설정 변경 (민감도, 노드 ON/OFF, AI 활성화)
- GET  /nodes/health        : 노드별 생존 상태
- GET  /charts/minute       : 분 단위 평균 차트 데이터
- POST /auth/register-token : FCM 토큰 등록
- WS   /ws/monitor          : 실시간 ai:result 스트리밍
"""

import ast
import asyncio
from contextlib import suppress
import json
import logging
import math
import os
import time
from typing import Any

import redis.asyncio as aioredis
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
import uvicorn

from notifier import load_risk_threshold, router as notify_router, send_risk_notification


class SystemSettings(BaseModel):
    risk_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    active_nodes: list[int] = Field(default=[1, 2, 3, 4, 5, 6])
    ai_enabled: bool = True


class RiskState(BaseModel):
    score: float = 0.0
    level: str = "normal"
    emergency: bool = False


class UnifiedSnapshot(BaseModel):
    ts_ms: int
    node_id: int = 0
    experts: dict[str, Any] = Field(default_factory=dict)
    audio: dict[str, Any] | None = None
    risk: RiskState = Field(default_factory=RiskState)
    risk_score: float = 0.0
    risk_level: str = "normal"
    emergency: bool = False
    ai_enabled: bool = True
    context_window: dict[str, Any] = Field(default_factory=dict)
    slm_invoked: bool = False
    is_outlier: bool = False
    correlated_with_history: bool = False
    qwen_reason: str | None = None
    slm_mode: str | None = None


class EmergencySummary(BaseModel):
    ts_ms: int
    node_id: int = 0
    risk_score: float = 0.0
    risk_level: str = "normal"
    emergency: bool = False
    summary: str = ""


class TokenRegistration(BaseModel):
    token: str
    device_id: str = "galaxy_flip4"


class AudioEventIn(BaseModel):
    node_id: int = Field(default=1, ge=0, le=255)
    text_ko: str | None = None
    waveform: list[float] = Field(default_factory=list)
    sample_rate: int = Field(default=16000, ge=8000, le=48000)
    trigger_ai: bool = True


def _parse_result_payload(raw: str) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        try:
            parsed = ast.literal_eval(raw)
            return parsed if isinstance(parsed, dict) else {"raw": raw}
        except Exception:
            return {"raw": raw}


def _stream_id_ts_ms(msg_id: str) -> int:
    try:
        return int(msg_id.split("-")[0])
    except Exception:
        return int(time.time() * 1000)


def _normalize_snapshot(payload: dict, msg_id: str | None = None) -> dict:
    normalized = dict(payload or {})
    ts_ms = int(normalized.get("ts_ms") or (_stream_id_ts_ms(msg_id) if msg_id else int(time.time() * 1000)))
    risk_source = normalized.get("risk", {}) if isinstance(normalized.get("risk"), dict) else {}
    risk_score = float(normalized.get("risk_score", risk_source.get("score", 0.0)))
    risk_level = normalized.get("risk_level", risk_source.get("level", "normal"))
    emergency = bool(normalized.get("emergency", risk_source.get("emergency", False)))

    normalized["ts_ms"] = ts_ms
    normalized["node_id"] = int(normalized.get("node_id", 0))
    normalized["risk_score"] = risk_score
    normalized["risk_level"] = risk_level
    normalized["emergency"] = emergency
    normalized["risk"] = {
        "score": risk_score,
        "level": risk_level,
        "emergency": emergency,
    }
    normalized.setdefault("experts", {})
    normalized.setdefault("audio", None)
    normalized.setdefault("ai_enabled", True)
    normalized.setdefault("context_window", {})
    normalized.setdefault("slm_invoked", False)
    normalized.setdefault("is_outlier", False)
    normalized.setdefault("correlated_with_history", False)
    normalized.setdefault("qwen_reason", None)
    normalized.setdefault("slm_mode", None)

    data = UnifiedSnapshot.model_validate(normalized).model_dump()
    if msg_id:
        data["_id"] = msg_id
        data["_ts"] = _stream_id_ts_ms(msg_id) / 1000
    return data


def _normalize_emergency(payload: dict, msg_id: str | None = None) -> dict:
    normalized = dict(payload or {})
    normalized["ts_ms"] = int(normalized.get("ts_ms") or (_stream_id_ts_ms(msg_id) if msg_id else int(time.time() * 1000)))
    normalized["node_id"] = int(normalized.get("node_id", 0))
    normalized["risk_score"] = float(normalized.get("risk_score", 0.0))
    normalized["risk_level"] = normalized.get("risk_level", "normal")
    normalized["emergency"] = bool(normalized.get("emergency", False))
    normalized["summary"] = str(normalized.get("summary", ""))

    data = EmergencySummary.model_validate(normalized).model_dump()
    if msg_id:
        data["_id"] = msg_id
        data["_ts"] = _stream_id_ts_ms(msg_id) / 1000
    return data


REDIS_HOST = os.getenv("REDIS_HOST", "db")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
RESULT_STREAM = "ai:result"
EMERGENCY_STREAM = "ai:emergency"
AUDIO_STREAM = "audio:events"
CSI_STREAM = "csi:raw"
SETTINGS_KEY = "sys:settings"
TOKEN_KEY_PREFIX = "fcm:token:"
MINUTE_AGG_PREFIX = "agg:minute:"
TOKEN_TTL_SECONDS = int(os.getenv("TOKEN_TTL_SECONDS", "3600"))
SETTINGS_TTL_SECONDS = int(os.getenv("SETTINGS_TTL_SECONDS", "3600"))
ALERT_DEDUP_TTL_SECONDS = int(os.getenv("ALERT_DEDUP_TTL_SECONDS", "3600"))
AUDIO_STREAM_MAXLEN = int(os.getenv("AUDIO_STREAM_MAXLEN", "3600"))
TTS_SPEAK_QUEUE     = "tts:speak:queue"
VOICE_RESP_PREFIX   = "user:voice_response:"
PHASE2_TIMEOUT_SEC  = int(os.getenv("VOICE_RESPONSE_TIMEOUT_SEC", "15"))
AUDIO_CLIP_KEY_PREFIX = "ai:clip:"
AUDIO_CLIP_TTL_SECONDS = int(os.getenv("AUDIO_CLIP_TTL_SECONDS", "3600"))
AUDIO_CLIP_POST_WAIT_MS = int(os.getenv("AUDIO_CLIP_POST_WAIT_MS", "15000"))
CSI_STREAM_MAXLEN = int(os.getenv("CSI_STREAM_MAXLEN", "36000"))
REDIS_MEMORY_WARN_BYTES = int(os.getenv("REDIS_MEMORY_WARN_BYTES", str(512 * 1024 * 1024)))

LOGGER = logging.getLogger("rp5.api")
if not LOGGER.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(message)s"))
    LOGGER.addHandler(_handler)
LOGGER.setLevel(logging.INFO)
LOGGER.propagate = False


def _log(level: int, event: str, **fields):
    payload = {
        "service": "api",
        "event": event,
        "ts_ms": int(time.time() * 1000),
    }
    payload.update(fields)
    LOGGER.log(level, json.dumps(payload, ensure_ascii=False))

app = FastAPI(title="rp5 API")


async def _reconnect_redis():
    old_client = getattr(app.state, "redis", None)
    if old_client is not None:
        with suppress(Exception):
            await old_client.aclose()

    app.state.redis = aioredis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        decode_responses=True,
        socket_connect_timeout=5,
    )
    await app.state.redis.ping()
    return app.state.redis


async def _ensure_redis():
    redis_client = getattr(app.state, "redis", None)
    if redis_client is None:
        return await _reconnect_redis()
    try:
        await redis_client.ping()
        return redis_client
    except Exception as exc:
        _log(logging.WARNING, "redis_reconnect", reason=str(exc))
        return await _reconnect_redis()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(notify_router, prefix="/notify")


async def _list_registered_tokens(redis_client) -> list[tuple[str, str]]:
    tokens = []
    async for key in redis_client.scan_iter(match=f"{TOKEN_KEY_PREFIX}*"):
        device_id = key.replace(TOKEN_KEY_PREFIX, "", 1)
        token = await redis_client.get(key)
        if token:
            tokens.append((device_id, token))
    return tokens


async def _capture_audio_clip(redis_client, ts_ms: int, node_id: int):
    """
    응급 이벤트 발생 시 전후 오디오 이벤트 메타데이터를 캡처해 Redis에 저장한다.
    전 15초: 즉시 수집 / 후 15초: AUDIO_CLIP_POST_WAIT_MS 대기 후 수집
    저장키: ai:clip:{ts_ms}  TTL: AUDIO_CLIP_TTL_SECONDS
    """
    pre_window_ms = 15000
    post_window_ms = AUDIO_CLIP_POST_WAIT_MS

    def _collect_events(entries, since_ms, until_ms):
        results = []
        for msg_id, fields in entries:
            try:
                event_ts = int(fields.get("ts_ms") or _stream_id_ts_ms(msg_id))
            except Exception:
                continue
            if not (since_ms <= event_ts <= until_ms):
                continue
            try:
                node = int(fields.get("node", 0))
            except Exception:
                node = 0
            if node_id and node not in (0, node_id):
                continue
            payload = _parse_result_payload(fields.get("data", ""))
            results.append({
                "ts_ms": event_ts,
                "peak_db": float(payload.get("peak_db", -120.0)),
                "sample_count": len(payload.get("waveform") or []),
                "sample_rate": int(payload.get("sample_rate", 16000)),
                "duration_ms": int(payload.get("duration_ms", 0)),
            })
        return results

    try:
        # 전 15초 수집
        pre_entries = await redis_client.xrevrange(AUDIO_STREAM, count=128)
        pre_events = _collect_events(pre_entries, ts_ms - pre_window_ms, ts_ms)
        pre_events.sort(key=lambda e: e["ts_ms"])

        # 후 15초 대기
        await asyncio.sleep(post_window_ms / 1000.0)

        # 후 15초 수집
        post_entries = await redis_client.xrange(
            AUDIO_STREAM,
            min=str(ts_ms),
            max=str(ts_ms + post_window_ms),
            count=128,
        )
        post_events = _collect_events(post_entries, ts_ms, ts_ms + post_window_ms)

        clip = {
            "ts_ms": ts_ms,
            "node_id": node_id,
            "pre_window_ms": pre_window_ms,
            "post_window_ms": post_window_ms,
            "pre_events": pre_events,
            "post_events": post_events,
            "total_events": len(pre_events) + len(post_events),
            "captured_at_ms": int(time.time() * 1000),
        }
        clip_key = f"{AUDIO_CLIP_KEY_PREFIX}{ts_ms}"
        await redis_client.set(clip_key, json.dumps(clip, ensure_ascii=False),
                               ex=AUDIO_CLIP_TTL_SECONDS)
        _log(logging.INFO, "audio_clip_captured", ts_ms=ts_ms, node_id=node_id,
             pre=len(pre_events), post=len(post_events))
    except Exception as exc:
        _log(logging.WARNING, "audio_clip_failed", ts_ms=ts_ms, error=str(exc))


def _build_emergency_tts(payload: dict) -> str:
    reason = (payload.get("summary") or "").strip()
    if reason:
        return f"위험 상황이 감지됐습니다. {reason}. 괜찮으시면 말씀해 주세요."
    return "위험 상황이 감지됐습니다. 괜찮으시면 말씀해 주세요."


async def _get_phase2_transcript(redis_client, after_id: str, timeout: int) -> str | None:
    """ai:result 스트림에서 speech_ko가 있는 첫 항목의 transcript를 반환. timeout초 내 없으면 None."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last_id = after_id
    while loop.time() < deadline:
        remaining = deadline - loop.time()
        block_ms = min(int(remaining * 1000), 2000)
        if block_ms <= 0:
            break
        try:
            entries = await redis_client.xread(
                {RESULT_STREAM: last_id}, count=5, block=block_ms
            )
            if not entries:
                continue
            for _, msgs in entries:
                for msg_id, fields in msgs:
                    last_id = msg_id
                    data = _parse_result_payload(fields.get("data", ""))
                    speech = (data.get("experts") or {}).get("speech_ko") or {}
                    if speech.get("speech_detected"):
                        return str(speech.get("transcript_ko", "")).strip()
        except Exception:
            await asyncio.sleep(0.5)
    return None


def _classify_phase2(transcript: str | None) -> str:
    """transcript 기반 의도 분류. 긴급 키워드 우선, 무응답 → call_emergency."""
    if not transcript:
        return "call_emergency"
    emg_kw = {"아파", "도와", "살려", "119", "응급", "위험", "불러"}
    safe_kw = {"괜찮", "아니야", "아니", "안 다쳤", "없어", "멀쩡"}
    if any(kw in transcript for kw in emg_kw):
        return "call_emergency"
    if any(kw in transcript for kw in safe_kw):
        return "cancel_alarm"
    return "call_emergency"


async def _handle_single_emergency(redis_client, msg_id: str, payload: dict):
    node_id = payload.get("node_id", 0)

    asyncio.create_task(
        _capture_audio_clip(redis_client, payload["ts_ms"], node_id)
    )

    tts_text = _build_emergency_tts(payload)
    resp_key = f"{VOICE_RESP_PREFIX}{node_id}"
    await redis_client.delete(resp_key)
    await redis_client.lpush(
        TTS_SPEAK_QUEUE,
        json.dumps({"text": tts_text, "node_id": node_id,
                    "ts_ms": payload["ts_ms"]}, ensure_ascii=False),
    )
    _log(logging.INFO, "tts_queued", node_id=node_id, text=tts_text)

    await redis_client.blpop(resp_key, timeout=15)

    # Phase 2: TTS 재생 후 ai:result 스트림에서 STT transcript 추출 → 의도 분류
    after_entries = await redis_client.xrevrange(RESULT_STREAM, count=1)
    after_id = after_entries[0][0] if after_entries else "$"
    transcript = await _get_phase2_transcript(redis_client, after_id, timeout=PHASE2_TIMEOUT_SEC)
    intent = _classify_phase2(transcript)
    _log(logging.INFO, "phase2_result", node_id=node_id, transcript=transcript, intent=intent)

    for device_id, token in await _list_registered_tokens(redis_client):
        dedupe_key = f"notify:sent:{msg_id}:{device_id}"
        claimed = await redis_client.set(dedupe_key, "1", ex=ALERT_DEDUP_TTL_SECONDS, nx=True)
        if not claimed:
            continue
        try:
            if intent == "cancel_alarm":
                await asyncio.to_thread(
                    send_risk_notification,
                    token, payload["risk_score"], "warning", False,
                    {"summary": "대상자 음성 응답 확인됨", "ts_ms": payload["ts_ms"]},
                )
            else:
                await asyncio.to_thread(
                    send_risk_notification,
                    token, payload["risk_score"], payload["risk_level"], True,
                    {"summary": payload["summary"], "ts_ms": payload["ts_ms"]},
                )
        except Exception as exc:
            _log(logging.ERROR, "fcm_send_failed", device_id=device_id, error=str(exc))


async def _alert_worker():
    last_id = "$"
    while True:
        try:
            redis_client = await _ensure_redis()
            entries = await redis_client.xread({EMERGENCY_STREAM: last_id}, count=10, block=1000)
            if not entries:
                continue

            for _stream, messages in entries:
                for msg_id, fields in messages:
                    payload = _normalize_emergency(_parse_result_payload(fields.get("data", "")), msg_id)
                    last_id = msg_id

                    if payload["risk_level"] != "critical":
                        continue

                    asyncio.create_task(_handle_single_emergency(redis_client, msg_id, payload))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log(logging.ERROR, "alert_worker_error", error=str(exc))
            await asyncio.sleep(1)


@app.on_event("startup")
async def startup():
    while True:
        try:
            app.state.redis = await _reconnect_redis()
            break
        except Exception as exc:
            _log(logging.WARNING, "startup_redis_retry", error=str(exc), retry_in_sec=1)
            await asyncio.sleep(1)
    def _restart_alert_worker(task: asyncio.Task):
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            _log(logging.ERROR, "alert_worker_crashed", error=str(exc), action="restarting")
        new_task = asyncio.create_task(_alert_worker())
        new_task.add_done_callback(_restart_alert_worker)
        app.state.alert_worker = new_task

    task = asyncio.create_task(_alert_worker())
    task.add_done_callback(_restart_alert_worker)
    app.state.alert_worker = task
    _log(logging.INFO, "startup_completed", redis_host=REDIS_HOST, redis_port=REDIS_PORT)


@app.on_event("shutdown")
async def shutdown():
    worker = getattr(app.state, "alert_worker", None)
    if worker is not None:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker
    await app.state.redis.aclose()
    _log(logging.INFO, "shutdown_completed")


@app.get("/")
async def root():
    return {"service": "rp5-api", "status": "ok"}


@app.get("/status")
async def get_status():
    redis_client = await _ensure_redis()
    entries = await redis_client.xrevrange(RESULT_STREAM, count=1)
    if not entries:
        # 204 응답은 body를 포함할 수 없어 h11 프로토콜 오류가 발생하므로 200으로 반환
        return JSONResponse({"message": "no data yet"}, status_code=200)
    msg_id, fields = entries[0]
    payload = _parse_result_payload(fields.get("data", ""))
    return _normalize_snapshot(payload, msg_id)


@app.get("/logs")
async def get_logs(n: int = 60):
    n = max(1, min(n, 36000))
    redis_client = await _ensure_redis()
    entries = await redis_client.xrevrange(RESULT_STREAM, count=n)
    result = []
    for msg_id, fields in entries:
        payload = _parse_result_payload(fields.get("data", ""))
        result.append(_normalize_snapshot(payload, msg_id))
    return result


@app.websocket("/ws/monitor")
async def ws_monitor(websocket: WebSocket):
    await websocket.accept()
    r = await _ensure_redis()
    last_id = "$"
    last_sent = 0.0
    WS_MIN_INTERVAL = 0.25  # 4Hz — 브라우저 DOM 포화 방지

    try:
        while True:
            entries = await r.xread({RESULT_STREAM: last_id}, count=50, block=1000)
            if entries:
                latest_msg_id = last_id
                latest_payload = None
                for _stream, messages in entries:
                    for msg_id, fields in messages:
                        latest_payload = _parse_result_payload(fields.get("data", ""))
                        latest_msg_id = msg_id
                last_id = latest_msg_id
                now = time.time()
                if latest_payload and (now - last_sent) >= WS_MIN_INTERVAL:
                    await websocket.send_json(_normalize_snapshot(latest_payload, latest_msg_id))
                    last_sent = now
            else:
                await websocket.send_json({"ping": True})

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        await websocket.close(code=1011, reason=str(exc))


@app.get("/settings")
async def get_settings():
    redis_client = await _ensure_redis()
    raw = await redis_client.get(SETTINGS_KEY)
    if raw:
        return json.loads(raw)
    return SystemSettings().model_dump()


@app.post("/settings")
async def update_settings(settings: SystemSettings):
    redis_client = await _ensure_redis()
    await redis_client.set(SETTINGS_KEY, json.dumps(settings.model_dump()), ex=SETTINGS_TTL_SECONDS)
    return {"status": "updated", "settings": settings.model_dump()}


@app.get("/nodes/health")
async def get_node_health():
    now = time.time()
    redis_client = await _ensure_redis()
    health = {}
    for i in range(1, 7):
        bucket = await redis_client.hgetall(f"node:{i}:health")
        if bucket:
            last_seen = float(bucket.get("last_seen", 0.0) or 0.0)
            loss_rate = float(bucket.get("loss_rate", 0.0) or 0.0)
            rx = int(bucket.get("rx", 0) or 0)
            lost = int(bucket.get("lost", 0) or 0)
            if last_seen and (now - last_seen) < 5:
                health[f"node_{i}"] = {
                    "status": "online",
                    "age_s": round(now - last_seen, 2),
                    "loss_rate": round(loss_rate, 4),
                    "rx": rx,
                    "lost": lost,
                }
            else:
                health[f"node_{i}"] = {
                    "status": "offline",
                    "age_s": None,
                    "loss_rate": round(loss_rate, 4),
                    "rx": rx,
                    "lost": lost,
                }
            continue

        last_seen = await redis_client.get(f"node:{i}:last_seen")
        if last_seen and (now - float(last_seen)) < 5:
            health[f"node_{i}"] = {"status": "online", "age_s": round(now - float(last_seen), 2), "loss_rate": 0.0, "rx": 0, "lost": 0}
        else:
            health[f"node_{i}"] = {"status": "offline", "age_s": None, "loss_rate": 0.0, "rx": 0, "lost": 0}
    return health


@app.get("/history")
async def get_history(n: int = 100, level: str = "warning"):
    n = max(1, min(n, 3600))
    if level not in ("warning", "critical"):
        return JSONResponse({"error": "level must be 'warning' or 'critical'"}, status_code=400)

    redis_client = await _ensure_redis()
    entries = await redis_client.xrevrange(EMERGENCY_STREAM, count=3600)
    result = []
    for msg_id, fields in entries:
        payload = _normalize_emergency(_parse_result_payload(fields.get("data", "")), msg_id)
        rl = payload.get("risk_level", "normal")
        if level == "critical" and rl != "critical":
            continue
        if level == "warning" and rl not in ("warning", "critical"):
            continue
        result.append(payload)
        if len(result) >= n:
            break
    return result


@app.get("/charts/minute")
async def get_minute_charts(minutes: int = 10):
    minutes = max(1, min(minutes, 60))
    redis_client = await _ensure_redis()
    chart = []
    current_minute = int(time.time() // 60)

    for minute_key in range(current_minute - minutes + 1, current_minute + 1):
        raw_bucket = await redis_client.hgetall(f"{MINUTE_AGG_PREFIX}{minute_key}")
        if not raw_bucket:
            continue

        risk_count = int(raw_bucket.get("risk_count", 0) or 0)
        heart_count = int(raw_bucket.get("heart_count", 0) or 0)
        breathing_count = int(raw_bucket.get("breathing_count", 0) or 0)

        def _avg(sum_key: str, count: int):
            if count <= 0:
                return None
            return round(float(raw_bucket.get(sum_key, 0.0)) / count, 2)

        chart.append({
            "ts": int(raw_bucket.get("ts", minute_key * 60)),
            "risk_score_avg": _avg("risk_sum", risk_count),
            "heart_rate_avg": _avg("heart_sum", heart_count),
            "breathing_rate_avg": _avg("breathing_sum", breathing_count),
            "slm_invoked_count": int(raw_bucket.get("slm_invoked_count", 0) or 0),
            "samples": risk_count,
        })

    return sorted(chart, key=lambda item: item["ts"])


@app.post("/auth/register-token")
async def register_fcm_token(body: TokenRegistration):
    redis_client = await _ensure_redis()
    await redis_client.set(f"{TOKEN_KEY_PREFIX}{body.device_id}", body.token, ex=TOKEN_TTL_SECONDS)
    return {"status": "registered", "device_id": body.device_id, "ttl_seconds": TOKEN_TTL_SECONDS}


@app.get("/auth/tokens")
async def list_fcm_tokens():
    redis_client = await _ensure_redis()
    tokens = await _list_registered_tokens(redis_client)
    return {"devices": [device_id for device_id, _token in tokens], "count": len(tokens)}


@app.get("/notify/threshold")
async def get_notify_threshold():
    redis_client = await _ensure_redis()
    threshold = await load_risk_threshold(redis_client)
    return {"risk_threshold": threshold}


@app.get("/system/redis-memory")
async def get_redis_memory():
    redis_client = await _ensure_redis()
    info = await redis_client.info("memory")
    used = int(info.get("used_memory", 0) or 0)
    used_peak = int(info.get("used_memory_peak", 0) or 0)
    maxmemory = int(info.get("maxmemory", 0) or 0)

    limit = maxmemory if maxmemory > 0 else REDIS_MEMORY_WARN_BYTES
    ratio = (used / limit) if limit > 0 else 0.0
    warning = ratio >= 0.85

    if warning:
        _log(logging.WARNING, "redis_memory_warning", used=used, limit=limit, ratio=round(ratio, 4))

    return {
        "used_memory": used,
        "used_memory_human": info.get("used_memory_human", str(used)),
        "used_memory_peak": used_peak,
        "used_memory_peak_human": info.get("used_memory_peak_human", str(used_peak)),
        "maxmemory": maxmemory,
        "warn_limit": limit,
        "usage_ratio": round(ratio, 4),
        "warning": warning,
    }


@app.get("/system/health")
async def get_system_health():
    try:
        redis_client = await _ensure_redis()
        pong = await redis_client.ping()
        info = await redis_client.info("memory")
    except Exception as exc:
        return JSONResponse(
            {
                "status": "degraded",
                "redis": {"connected": False, "error": str(exc)},
            },
            status_code=503,
        )

    used = int(info.get("used_memory", 0) or 0)
    maxmemory = int(info.get("maxmemory", 0) or 0)
    limit = maxmemory if maxmemory > 0 else REDIS_MEMORY_WARN_BYTES
    usage_ratio = (used / limit) if limit > 0 else 0.0

    return {
        "status": "ok" if pong else "degraded",
        "redis": {
            "connected": bool(pong),
            "used_memory": used,
            "used_memory_human": info.get("used_memory_human", str(used)),
            "maxmemory": maxmemory,
            "warn_limit": limit,
            "usage_ratio": round(usage_ratio, 4),
            "warning": usage_ratio >= 0.85,
        },
        "ts_ms": int(time.time() * 1000),
    }


@app.get("/emergency/clip/{ts_ms}")
async def get_emergency_clip(ts_ms: int):
    """응급 이벤트 오디오 클립 메타데이터 조회. ai:clip:{ts_ms} → JSON."""
    redis_client = await _ensure_redis()
    raw = await redis_client.get(f"{AUDIO_CLIP_KEY_PREFIX}{ts_ms}")
    if not raw:
        return JSONResponse({"error": "clip not found", "ts_ms": ts_ms}, status_code=404)
    return json.loads(raw)


@app.post("/audio/events")
async def ingest_audio_event(body: AudioEventIn):
    text_ko = (body.text_ko or "").strip()
    if not text_ko and not body.waveform:
        return JSONResponse({"error": "text_ko or waveform is required"}, status_code=400)

    clean_waveform = []
    if body.waveform:
        for sample in body.waveform[:16000 * 8]:
            try:
                value = float(sample)
            except Exception:
                continue
            if not math.isfinite(value):
                continue
            clean_waveform.append(max(-1.0, min(1.0, value)))

    payload: dict[str, Any] = {"sample_rate": body.sample_rate}
    if text_ko:
        payload["text_ko"] = text_ko
    if clean_waveform:
        payload["waveform"] = clean_waveform

    redis_client = await _ensure_redis()
    ts_ms = int(time.time() * 1000)
    audio_id = await redis_client.xadd(
        AUDIO_STREAM,
        {
            "node": body.node_id,
            "ts_ms": ts_ms,
            "data": json.dumps(payload, ensure_ascii=False),
        },
        maxlen=AUDIO_STREAM_MAXLEN,
        approximate=True,
    )

    csi_id = None
    if body.trigger_ai:
        # AI 루프를 깨우기 위한 최소 CSI 트리거 이벤트
        csi_id = await redis_client.xadd(
            CSI_STREAM,
            {
                "node": body.node_id,
                "ts_ms": ts_ms,
                "data": "",
            },
            maxlen=CSI_STREAM_MAXLEN,
            approximate=True,
        )

    return {
        "status": "ok",
        "audio_event_id": audio_id,
        "csi_event_id": csi_id,
        "node_id": body.node_id,
        "sample_rate": body.sample_rate,
        "text_len": len(text_ko),
        "waveform_samples": len(clean_waveform),
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
