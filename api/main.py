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
- GET  /system/resources    : 호스트 CPU·온도·메모리·디스크 (조회 시 계산, 저장 없음)
- POST /auth/register-token : FCM 토큰 등록
- DELETE /auth/register-token/{device_id} : FCM 토큰 삭제
- POST /alerts/{msg_id}/feedback : 보호자 오탐·미탐 신고
- GET  /app/summary        : 보호자 앱 홈 요약
- WS   /ws/monitor          : 실시간 ai:result 스트리밍
"""

import ast
import asyncio
from contextlib import suppress
import json
import logging
import math
import os
import shutil
import time
from typing import Any

import redis.asyncio as aioredis
from redis.exceptions import RedisError
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
import uvicorn

from notifier import (
    FCM_READY,
    load_risk_threshold,
    router as notify_router,
    send_risk_notification,
    send_heartbeat_notification,
    send_voice_ok_notification,
)


class SystemSettings(BaseModel):
    risk_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    active_nodes: list[int] = Field(default_factory=lambda: [1, 2, 3, 4, 5, 6])
    ai_enabled: bool = True
    # 모델별 on/off — m1(낙상)/m2(바이탈)/m3(환경음)/m4(STT)/m5(Qwen)
    models: dict[str, bool] = Field(
        default_factory=lambda: {
            "m1": True,
            "m2": False,
            "m3": True,
            "m4": True,
            "m5": True,
        }
    )


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
    models: dict[str, Any] = Field(default_factory=dict)
    context_window: dict[str, Any] = Field(default_factory=dict)
    slm_invoked: bool = False
    is_outlier: bool = False
    correlated_with_history: bool = False
    qwen_reason: str | None = None
    slm_mode: str | None = None
    slm_needed: bool = False
    rule_alert: bool = False
    emergency_breakdown: dict[str, Any] | None = None
    expert_latency_ms: dict[str, Any] = Field(default_factory=dict)
    model_latency_ms: dict[str, Any] = Field(default_factory=dict)


class EmergencySummary(BaseModel):
    ts_ms: int
    node_id: int = 0
    risk_score: float = 0.0
    risk_level: str = "normal"
    emergency: bool = False
    summary: str = ""
    slm_mode: str | None = None
    qwen_reason: str | None = None


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
    normalized.setdefault("models", {})
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
SETTINGS_KEY = "sys:settings"
TOKEN_KEY_PREFIX = "fcm:token:"
MINUTE_AGG_PREFIX = "agg:minute:"
TOKEN_TTL_SECONDS = int(os.getenv("TOKEN_TTL_SECONDS", "3600"))
SETTINGS_TTL_SECONDS = int(os.getenv("SETTINGS_TTL_SECONDS", "3600"))
ALERT_DEDUP_TTL_SECONDS = int(os.getenv("ALERT_DEDUP_TTL_SECONDS", "3600"))
AUDIO_STREAM_MAXLEN = int(os.getenv("AUDIO_STREAM_MAXLEN", "120"))
TTS_SPEAK_QUEUE     = "tts:speak:queue"
TTS_QUEUE_MAXLEN    = int(os.getenv("TTS_QUEUE_MAXLEN", "32"))
TTS_QUEUE_TTL_SECONDS = int(os.getenv("TTS_QUEUE_TTL_SECONDS", "3600"))
VOICE_RESP_PREFIX   = "user:voice_response:"
PHASE2_TIMEOUT_SEC  = int(os.getenv("VOICE_RESPONSE_TIMEOUT_SEC", "15"))
PHASE2_LOCK_SEC     = int(os.getenv("PHASE2_LOCK_SEC", "90"))
TTS_WAIT_SEC        = int(os.getenv("TTS_WAIT_SEC", "15"))
# 음성 확인(TTS·STT) 서비스가 떠 있을 때만 Phase 2를 진행한다. 기본 Core 구성에는 없다.
VOICE_ENABLED       = os.getenv("VOICE_ENABLED", "false").lower() in ("1", "true", "yes")
# alert worker 시작·재시작 시 되짚어 읽는 구간. 중복 발송은 notify:sent 키가 막는다.
ALERT_REPLAY_MS     = int(os.getenv("ALERT_REPLAY_MS", "30000"))
# 단일 마이크 구성: 응답 transcript를 이 노드의 오디오에서 찾는다(빈 값이면 경보 노드와 같은 노드).
VOICE_NODE_ID       = int(os.getenv("VOICE_NODE_ID", "0") or 0)
# FCM 토큰·설정은 TTL 1시간을 지키되, API가 살아 있는 동안 주기적으로 연장한다.
TTL_REFRESH_SEC     = int(os.getenv("TTL_REFRESH_SEC", "600"))
# 보호자 앱 정상 동작 신호 주기(초). 0이면 보내지 않는다(예: 86400 = 하루 1회).
HEARTBEAT_INTERVAL_SEC = int(os.getenv("HEARTBEAT_INTERVAL_SEC", "0"))
# 앱 홈 요약에서 "설치된 센서"로 보는 노드(M1 필수 노드와 같게 둔다).
APP_EXPECTED_NODES = tuple(
    int(v) for v in os.getenv("APP_EXPECTED_NODES", "1,2,3").split(",") if v.strip()
)
# 보호자 신고를 M5 보정에 쓰는 기존 키(ai-experts MQTT 피드백과 같은 키·형식, TTL 3600).
FEEDBACK_REDIS_KEY = os.getenv("MQTT_FEEDBACK_REDIS_KEY", "mqtt:feedback:last")
AUDIO_CLIP_KEY_PREFIX = "ai:clip:"
AUDIO_CLIP_TTL_SECONDS = int(os.getenv("AUDIO_CLIP_TTL_SECONDS", "3600"))
AUDIO_CLIP_POST_WAIT_MS = int(os.getenv("AUDIO_CLIP_POST_WAIT_MS", "15000"))
REDIS_MEMORY_WARN_BYTES = int(os.getenv("REDIS_MEMORY_WARN_BYTES", str(256 * 1024 * 1024)))
REDIS_MEMORY_CRITICAL_BYTES = int(os.getenv("REDIS_MEMORY_CRITICAL_BYTES", str(384 * 1024 * 1024)))

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
_phase2_tasks: set[asyncio.Task] = set()


def _spawn_phase2_task(coro, *, node_id: int, msg_id: str) -> asyncio.Task:
    """Phase 2 작업을 추적하고 백그라운드 예외를 구조화 로그로 회수한다."""
    task = asyncio.create_task(coro, name=f"phase2:{node_id}:{msg_id}")
    _phase2_tasks.add(task)

    def _on_done(completed: asyncio.Task):
        _phase2_tasks.discard(completed)
        if completed.cancelled():
            return
        exc = completed.exception()
        if exc is not None:
            _log(
                logging.ERROR,
                "phase2_task_failed",
                node_id=node_id,
                msg_id=msg_id,
                error=str(exc),
            )

    task.add_done_callback(_on_done)
    return task


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


async def _reconnect_redis_raw():
    # audio:events의 waveform 필드는 raw float32 bytes라 decode_responses=True 클라이언트로
    # 읽으면 UnicodeDecodeError가 난다. 스트림 바이너리 읽기 전용 클라이언트.
    old_client = getattr(app.state, "redis_raw", None)
    if old_client is not None:
        with suppress(Exception):
            await old_client.aclose()

    app.state.redis_raw = aioredis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        decode_responses=False,
        socket_connect_timeout=5,
    )
    await app.state.redis_raw.ping()
    return app.state.redis_raw


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


async def _ensure_redis_raw():
    redis_client = getattr(app.state, "redis_raw", None)
    if redis_client is None:
        return await _reconnect_redis_raw()
    try:
        await redis_client.ping()
        return redis_client
    except Exception as exc:
        _log(logging.WARNING, "redis_raw_reconnect", reason=str(exc))
        return await _reconnect_redis_raw()

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
            if isinstance(msg_id, bytes):
                msg_id = msg_id.decode("utf-8", errors="ignore")
            try:
                event_ts = int(fields.get(b"ts_ms") or _stream_id_ts_ms(msg_id))
            except Exception:
                continue
            if not (since_ms <= event_ts <= until_ms):
                continue
            try:
                node = int(fields.get(b"node", 0))
            except Exception:
                node = 0
            if node_id and node not in (0, node_id):
                continue
            raw_data = fields.get(b"data", b"")
            payload = _parse_result_payload(raw_data.decode("utf-8", errors="ignore"))
            # 신 포맷: waveform이 별도 raw float32 bytes 필드 / 구 포맷: data JSON 안 리스트
            raw_wav = fields.get(b"waveform", b"")
            if raw_wav:
                sample_count = len(raw_wav) // 4
            else:
                sample_count = len(payload.get("waveform") or [])
            results.append({
                "ts_ms": event_ts,
                "peak_db": float(payload.get("peak_db", -120.0)),
                "sample_count": sample_count,
                "sample_rate": int(payload.get("sample_rate", 16000)),
                "duration_ms": int(payload.get("duration_ms", 0)),
            })
        return results

    try:
        redis_raw = await _ensure_redis_raw()

        # 전 15초 수집
        pre_entries = await redis_raw.xrevrange(AUDIO_STREAM, count=128)
        pre_events = _collect_events(pre_entries, ts_ms - pre_window_ms, ts_ms)
        pre_events.sort(key=lambda e: e["ts_ms"])

        # 후 15초 대기
        await asyncio.sleep(post_window_ms / 1000.0)

        # 후 15초 수집
        post_entries = await redis_raw.xrange(
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


# Phase 2 TTS 안내 문구 — 마이크가 재생음을 재녹음(에코)해도 오분류되지 않도록
# _classify_phase2의 긴급/안전 키워드와 겹치는 단어를 포함하면 안 된다.
EMERGENCY_TTS_TEXT = "이상이 감지되었습니다. 상태를 말씀해 주세요."


def _fresh_transcript(data: dict, since_ms: int) -> str | None:
    """since_ms 이후에 녹음된 오디오의 transcript만 돌려준다.

    ai-experts는 최근 오디오 결과를 최대 30초간 스냅샷마다 다시 싣기 때문에,
    경보 전 발화나 TTS 에코가 응답으로 잡히지 않도록 오디오 시각으로 거른다.
    """
    try:
        audio_ts = int((data.get("audio") or {}).get("ts_ms"))
    except (TypeError, ValueError):
        return None
    if audio_ts < since_ms:
        return None
    speech = (data.get("experts") or {}).get("speech_ko") or {}
    if not speech.get("speech_detected"):
        return None
    return str(speech.get("transcript_ko", "")).strip() or None


async def _get_phase2_transcript(
    redis_client, after_id: str, timeout: int, node_id: int, since_ms: int = 0
) -> str | None:
    """같은 노드의 ai:result에서 since_ms 이후 녹음된 첫 STT transcript를 반환한다."""
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
                    try:
                        result_node_id = int(data.get("node_id", 0) or 0)
                    except (TypeError, ValueError):
                        result_node_id = 0
                    if node_id and result_node_id != node_id:
                        continue
                    transcript = _fresh_transcript(data, since_ms)
                    if transcript:
                        return transcript
                    # 오래된 오디오·빈 STT 항목은 무시하고 타임아웃까지 계속 대기
        except Exception:
            await asyncio.sleep(0.5)
    return None


def _classify_phase2(transcript: str | None) -> str:
    """transcript 기반 의도 분류. 긴급 키워드 우선, 무응답 → call_emergency."""
    if not transcript:
        return "call_emergency"
    # 부정형 안전 표현("안 괜찮아")이 safe_kw "괜찮"에 매칭되어 cancel_alarm으로
    # 오분류되는 것을 방지 — 긴급 키워드보다도 먼저 검사한다.
    # "일어날 수가 없어", "힘이 없어", "아니, 못 일어나겠어"는 도움 요청이다.
    # 그래서 "없어"·"아니"는 안전 키워드로 쓰지 않고, 거동 불가 표현을 긴급으로 먼저 본다.
    neg_unsafe = ("안 괜찮", "괜찮지 않", "못 일어", "못 움직", "수가 없", "수 없어", "힘이 없",
                  "힘없", "숨이", "숨을 못", "어지러")
    emg_kw = {"아파", "도와", "살려", "119", "응급", "위험", "불러"}
    safe_kw = {"괜찮", "안 다쳤", "멀쩡", "안 아파", "안아파"}
    if any(kw in transcript for kw in neg_unsafe):
        return "call_emergency"
    # "안 아파"의 "아파"가 긴급 키워드로 잡히지 않게 부정 표현을 뺀 뒤 검사한다.
    without_negated = transcript.replace("안 아파", "").replace("안아파", "")
    if any(kw in without_negated for kw in emg_kw):
        return "call_emergency"
    if any(kw in transcript for kw in safe_kw):
        return "cancel_alarm"
    return "call_emergency"


async def _notify_all(redis_client, dedupe_prefix: str | None, send_fn, *send_args) -> int:
    """등록된 모든 기기에 FCM을 보낸다. 중복 방지 키 쓰기가 실패해도 발송은 한다.

    dedupe_prefix가 None이면 중복 방지 키를 쓰지 않는다(정기 신호 등).
    """
    try:
        tokens = await _list_registered_tokens(redis_client)
    except RedisError as exc:
        _log(logging.ERROR, "fcm_token_list_failed", error=str(exc))
        return 0
    sent = 0
    for device_id, token in tokens:
        claimed = True
        if dedupe_prefix is not None:
            try:
                claimed = await redis_client.set(
                    f"{dedupe_prefix}:{device_id}", "1", ex=ALERT_DEDUP_TTL_SECONDS, nx=True
                )
            except RedisError as exc:
                _log(logging.WARNING, "notify_dedupe_failed", device_id=device_id, error=str(exc))
                claimed = True
        if not claimed:
            continue
        try:
            await asyncio.to_thread(send_fn, token, *send_args)
            sent += 1
        except Exception as exc:
            _log(logging.ERROR, "fcm_send_failed", device_id=device_id, error=str(exc))
    return sent


async def _run_phase2(redis_client, payload: dict, node_id: int) -> tuple[str, str | None]:
    """TTS로 안부를 묻고 음성 응답을 분류한다. 반환값은 (의도, transcript)."""
    resp_key = f"{VOICE_RESP_PREFIX}{node_id}"
    tts_payload = json.dumps(
        {"text": EMERGENCY_TTS_TEXT, "node_id": node_id, "ts_ms": payload["ts_ms"]},
        ensure_ascii=False,
    )
    await redis_client.delete(resp_key)
    pipe = redis_client.pipeline()
    pipe.lpush(TTS_SPEAK_QUEUE, tts_payload)
    pipe.ltrim(TTS_SPEAK_QUEUE, 0, max(0, TTS_QUEUE_MAXLEN - 1))
    pipe.expire(TTS_SPEAK_QUEUE, TTS_QUEUE_TTL_SECONDS)
    await pipe.execute()
    _log(logging.INFO, "tts_queued", node_id=node_id, text=EMERGENCY_TTS_TEXT)

    tts_signal = await redis_client.blpop(resp_key, timeout=TTS_WAIT_SEC)
    if tts_signal is None:
        _log(logging.WARNING, "tts_signal_timeout", node_id=node_id)
    # 재생이 끝난 뒤 녹음된 오디오만 응답으로 인정한다.
    since_ms = int(time.time() * 1000)

    after_entries = await redis_client.xrevrange(RESULT_STREAM, count=1)
    after_id = after_entries[0][0] if after_entries else "$"
    transcript = await _get_phase2_transcript(
        redis_client, after_id, timeout=PHASE2_TIMEOUT_SEC,
        node_id=VOICE_NODE_ID or node_id, since_ms=since_ms,
    )
    intent = _classify_phase2(transcript)
    # 대화 내용은 로그에 남기지 않는다(개인정보). 길이와 분류 결과만 기록한다.
    _log(logging.INFO, "phase2_result", node_id=node_id,
         transcript_len=len(transcript or ""), intent=intent)
    return intent, transcript


async def _handle_single_emergency(redis_client, msg_id: str, payload: dict):
    node_id = payload.get("node_id", 0)

    # 노드별 Phase 2 중복 실행 방지 락. critical 지속 시 ai:emergency에 엔트리가 이어서
    # 쌓여 핸들러가 중첩 스폰되는 것을 차단한다. 락은 EX 자연 만료 — 재알림 쿨다운을 겸한다.
    # Redis 쓰기가 실패하면(noeviction 등) 경보를 우선해 락 없이 진행한다.
    lock_key = f"phase2:active:{node_id}"
    lock_failed = False
    try:
        # redis-py는 NX 실패 시 None을 반환한다.
        acquired = bool(await redis_client.set(lock_key, "active", ex=PHASE2_LOCK_SEC, nx=True))
    except RedisError as exc:
        _log(logging.ERROR, "phase2_lock_failed", node_id=node_id, error=str(exc))
        acquired, lock_failed = False, True
    if not acquired and not lock_failed:
        _log(logging.INFO, "phase2_skipped_active", node_id=node_id)
        return

    _spawn_phase2_task(
        _capture_audio_clip(redis_client, payload["ts_ms"], node_id),
        node_id=node_id,
        msg_id=f"{msg_id}:clip",
    )

    # 1차 알림은 음성 확인을 기다리지 않고 즉시 보낸다.
    sent = await _notify_all(
        redis_client, f"notify:sent:{msg_id}", send_risk_notification,
        payload["risk_score"], payload["risk_level"], True,
        {"summary": payload["summary"], "ts_ms": payload["ts_ms"], "node_id": node_id,
         "msg_id": msg_id, "slm_mode": payload.get("slm_mode")},
    )
    _log(logging.WARNING, "alert_sent", node_id=node_id, msg_id=msg_id, devices=sent,
         voice_enabled=VOICE_ENABLED)

    if VOICE_ENABLED:
        try:
            intent, transcript = await _run_phase2(redis_client, payload, node_id)
        except RedisError as exc:
            _log(logging.ERROR, "phase2_redis_failed", node_id=node_id, error=str(exc))
            intent, transcript = "call_emergency", None
        if intent == "cancel_alarm":
            # 응급 채널이 아닌 일반 알림으로 "괜찮다고 응답"을 따로 알린다.
            await _notify_all(
                redis_client, f"notify:followup:{msg_id}", send_voice_ok_notification,
                node_id, payload["ts_ms"], transcript, msg_id,
            )

    # 키는 재알림 쿨다운을 위해 TTL까지 유지하되, 추론 억제는 실제 Phase 2 동안만 적용한다.
    if acquired:
        try:
            await redis_client.set(lock_key, "cooldown", xx=True, keepttl=True)
        except RedisError as exc:
            _log(logging.WARNING, "phase2_lock_update_failed", node_id=node_id, error=str(exc))


async def _heartbeat_worker():
    """HEARTBEAT_INTERVAL_SEC마다 보호자 앱에 정상 동작 신호를 보낸다(첫 발송은 한 주기 뒤)."""
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_SEC)
        try:
            redis_client = await _ensure_redis()
            nodes = await get_node_health()
            online = sum(
                1 for node_id in APP_EXPECTED_NODES
                if (nodes.get(f"node_{node_id}") or {}).get("status") == "online"
            )
            sent = await _notify_all(redis_client, None, send_heartbeat_notification,
                                     online, len(APP_EXPECTED_NODES))
            _log(logging.INFO, "heartbeat_sent", devices=sent, nodes_online=online)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log(logging.WARNING, "heartbeat_failed", error=str(exc))


async def _ttl_refresh_worker():
    """등록 토큰과 설정의 TTL(≤3600s)을 주기적으로 연장한다.

    운영 중에는 1시간 뒤 푸시가 끊기거나 설정이 기본값으로 돌아가지 않고,
    시스템이 1시간 넘게 멈추면 규칙대로 만료된다.
    """
    while True:
        try:
            redis_client = await _ensure_redis()
            refreshed = 0
            async for key in redis_client.scan_iter(match=f"{TOKEN_KEY_PREFIX}*"):
                refreshed += int(bool(await redis_client.expire(key, TOKEN_TTL_SECONDS)))
            await redis_client.expire(SETTINGS_KEY, SETTINGS_TTL_SECONDS)
            _log(logging.DEBUG, "ttl_refreshed", tokens=refreshed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log(logging.WARNING, "ttl_refresh_failed", error=str(exc))
        await asyncio.sleep(TTL_REFRESH_SEC)


async def _alert_worker():
    # 재시작 사이에 기록된 경보를 놓치지 않도록 최근 구간부터 읽는다.
    last_id = f"{max(0, int(time.time() * 1000) - ALERT_REPLAY_MS)}-0"
    while True:
        try:
            redis_client = await _ensure_redis()
            entries = await redis_client.xread({EMERGENCY_STREAM: last_id}, count=10, block=1000)
            if not entries:
                continue

            for _stream, messages in entries:
                for msg_id, fields in messages:
                    # 형식이 깨진 항목 하나가 이후 경보를 모두 막지 않도록 먼저 전진한다.
                    last_id = msg_id
                    try:
                        payload = _normalize_emergency(
                            _parse_result_payload(fields.get("data", "")), msg_id
                        )
                    except Exception as exc:
                        _log(logging.ERROR, "alert_entry_invalid", msg_id=msg_id, error=str(exc))
                        continue

                    if payload["risk_level"] != "critical":
                        continue

                    _spawn_phase2_task(
                        _handle_single_emergency(redis_client, msg_id, payload),
                        node_id=payload["node_id"],
                        msg_id=msg_id,
                    )
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
    app.state.ttl_refresh = asyncio.create_task(_ttl_refresh_worker())
    if HEARTBEAT_INTERVAL_SEC > 0:
        app.state.heartbeat = asyncio.create_task(_heartbeat_worker())
    _log(logging.INFO, "startup_completed", redis_host=REDIS_HOST, redis_port=REDIS_PORT,
         fcm_ready=FCM_READY, voice_enabled=VOICE_ENABLED)
    if not FCM_READY:
        _log(logging.ERROR, "fcm_unavailable",
             detail="Firebase 키 파일이 없어 휴대폰 알림을 보낼 수 없습니다 (FIREBASE_KEY_PATH)")


@app.on_event("shutdown")
async def shutdown():
    for name in ("alert_worker", "ttl_refresh", "heartbeat"):
        worker = getattr(app.state, name, None)
        if worker is not None:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
    pending_phase2 = list(_phase2_tasks)
    for task in pending_phase2:
        task.cancel()
    if pending_phase2:
        await asyncio.gather(*pending_phase2, return_exceptions=True)
    redis_client = getattr(app.state, "redis", None)
    if redis_client is not None:
        await redis_client.aclose()
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
    last_ping = time.time()
    WS_MIN_INTERVAL = 0.25  # 4Hz — 브라우저 DOM 포화 방지
    # 노드별 최신 스냅샷을 모아 보낸다. 위험 수준이 바뀌거나 critical이면 즉시 보낸다
    # (다른 노드의 normal 스냅샷에 가려 경보 화면이 누락되지 않게).
    pending: dict[int, tuple[str, dict]] = {}
    sent_level: dict[int, str] = {}

    try:
        while True:
            entries = await r.xread({RESULT_STREAM: last_id}, count=100, block=250)
            for _stream, messages in entries or []:
                for msg_id, fields in messages:
                    last_id = msg_id
                    payload = _parse_result_payload(fields.get("data", ""))
                    try:
                        node = int(payload.get("node_id", 0) or 0)
                    except (TypeError, ValueError):
                        node = 0
                    level = payload.get("risk_level", "normal")
                    if level == "critical" or level != sent_level.get(node, "normal"):
                        await websocket.send_json(_normalize_snapshot(payload, msg_id))
                        sent_level[node] = level
                        pending.pop(node, None)
                    else:
                        pending[node] = (msg_id, payload)
            now = time.time()
            if pending and now - last_sent >= WS_MIN_INTERVAL:
                for msg_id, payload in pending.values():
                    await websocket.send_json(_normalize_snapshot(payload, msg_id))
                pending.clear()
                last_sent = last_ping = now
            elif not entries and now - last_ping >= 1.0:
                await websocket.send_json({"ping": True})
                last_ping = now

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
            rssi = int(float(bucket["rssi"])) if bucket.get("rssi") not in (None, "") else None
            if last_seen and (now - last_seen) < 5:
                health[f"node_{i}"] = {
                    "status": "online",
                    "age_s": round(now - last_seen, 2),
                    "loss_rate": round(loss_rate, 4),
                    "rx": rx,
                    "lost": lost,
                    "rssi": rssi,
                }
            else:
                health[f"node_{i}"] = {
                    "status": "offline",
                    "age_s": None,
                    "loss_rate": round(loss_rate, 4),
                    "rx": rx,
                    "lost": lost,
                    "rssi": rssi,
                }
            continue

        last_seen = await redis_client.get(f"node:{i}:last_seen")
        if last_seen and (now - float(last_seen)) < 5:
            health[f"node_{i}"] = {"status": "online", "age_s": round(now - float(last_seen), 2), "loss_rate": 0.0, "rx": 0, "lost": 0}
        else:
            health[f"node_{i}"] = {"status": "offline", "age_s": None, "loss_rate": 0.0, "rx": 0, "lost": 0}
    return health


@app.get("/history")
async def get_history(n: int = 100, level: str = "warning", before: str | None = None):
    """경보 이력(최신순). before=스트림 ID면 그보다 오래된 항목부터(페이지네이션)."""
    n = max(1, min(n, 3600))
    if level not in ("warning", "critical"):
        return JSONResponse({"error": "level must be 'warning' or 'critical'"}, status_code=400)

    redis_client = await _ensure_redis()
    result = []
    upper = f"({before}" if before else "+"
    scanned = 0
    # 필요한 만큼만 200건씩 거꾸로 읽는다(최대 3,600건 = 스트림 한도).
    while len(result) < n and scanned < 3600:
        entries = await redis_client.xrevrange(EMERGENCY_STREAM, max=upper, count=200)
        if not entries:
            break
        scanned += len(entries)
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
        upper = f"({entries[-1][0]}"
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


@app.delete("/auth/register-token/{device_id}")
async def unregister_fcm_token(device_id: str):
    redis_client = await _ensure_redis()
    removed = await redis_client.delete(f"{TOKEN_KEY_PREFIX}{device_id}")
    return {"status": "removed" if removed else "not_found", "device_id": device_id}


class AlertFeedback(BaseModel):
    device_id: str
    feedback: str = Field(pattern="^(false_alarm|missed_alert)$")


@app.post("/alerts/{msg_id}/feedback")
async def post_alert_feedback(msg_id: str, body: AlertFeedback):
    """보호자 오탐·미탐 신고. 기존 M5 피드백 키에 기록해 다음 1시간 M5 점수를 ±0.08 보정한다.

    규칙 경보(slm_mode=rule)는 M5를 거치지 않으므로 이 보정의 영향을 받지 않는다.
    """
    redis_client = await _ensure_redis()
    entry = {
        "feedback": body.feedback,
        "source": "guardian_app",
        "msg_id": msg_id,
        "device_id": body.device_id,
        "ts_ms": int(time.time() * 1000),
    }
    await redis_client.set(FEEDBACK_REDIS_KEY, json.dumps(entry, ensure_ascii=False), ex=3600)
    _log(logging.INFO, "alert_feedback", msg_id=msg_id, device_id=body.device_id, feedback=body.feedback)
    return {"ok": True, "msg_id": msg_id, "feedback": body.feedback}


def _compose_app_summary(latest: dict | None, latest_id: str | None, nodes: dict, settings: dict,
                         last_alert: dict | None, server_status: str, now_ms: int) -> dict:
    """보호자 앱 홈 요약. 음성 전사·생체신호는 싣지 않는다."""
    online = sum(
        1 for node_id in APP_EXPECTED_NODES
        if (nodes.get(f"node_{node_id}") or {}).get("status") == "online"
    )
    models = settings.get("models") or {}
    summary = {
        "risk_level": None,
        "risk_score": None,
        "data_age_s": None,
        "nodes_online": online,
        "nodes_expected": len(APP_EXPECTED_NODES),
        "server": server_status,
        "fcm_ready": FCM_READY,
        "last_alert": last_alert,
        "monitoring": {
            "ai_enabled": bool(settings.get("ai_enabled", True)),
            "m1": bool(models.get("m1", True)),
        },
        "ts_ms": now_ms,
    }
    if latest is not None and latest_id:
        summary["risk_level"] = latest.get("risk_level", "normal")
        summary["risk_score"] = latest.get("risk_score")
        summary["data_age_s"] = round(max(0, now_ms - _stream_id_ts_ms(latest_id)) / 1000, 1)
    return summary


@app.get("/app/summary")
async def get_app_summary():
    redis_client = await _ensure_redis()
    now_ms = int(time.time() * 1000)
    latest, latest_id = None, None
    entries = await redis_client.xrevrange(RESULT_STREAM, count=1)
    if entries:
        latest_id, fields = entries[0]
        latest = _parse_result_payload(fields.get("data", ""))

    last_alert = None
    for msg_id, fields in await redis_client.xrevrange(EMERGENCY_STREAM, count=50):
        alert = _normalize_emergency(_parse_result_payload(fields.get("data", "")), msg_id)
        if alert["risk_level"] == "critical":
            last_alert = {k: alert.get(k) for k in ("ts_ms", "node_id", "summary", "slm_mode")}
            last_alert["msg_id"] = msg_id
            break

    raw_settings = await redis_client.get(SETTINGS_KEY)
    settings = json.loads(raw_settings) if raw_settings else SystemSettings().model_dump()
    try:
        info = await redis_client.info("memory")
        server_status = "degraded" if _redis_memory_summary(info)["critical"] else "ok"
    except RedisError:
        server_status = "degraded"
    return _compose_app_summary(latest, latest_id, await get_node_health(), settings,
                                last_alert, server_status, now_ms)


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


def _redis_memory_summary(info: dict) -> dict:
    used = int(info.get("used_memory", 0) or 0)
    used_peak = int(info.get("used_memory_peak", 0) or 0)
    maxmemory = int(info.get("maxmemory", 0) or 0)
    ratio = (used / maxmemory) if maxmemory > 0 else 0.0
    return {
        "used_memory": used,
        "used_memory_human": info.get("used_memory_human", str(used)),
        "used_memory_peak": used_peak,
        "used_memory_peak_human": info.get("used_memory_peak_human", str(used_peak)),
        "maxmemory": maxmemory,
        "warn_limit": REDIS_MEMORY_WARN_BYTES,
        "critical_limit": REDIS_MEMORY_CRITICAL_BYTES,
        "usage_ratio": round(ratio, 4),
        "warning": used >= REDIS_MEMORY_WARN_BYTES,
        "critical": used >= REDIS_MEMORY_CRITICAL_BYTES,
    }


@app.get("/system/redis-memory")
async def get_redis_memory():
    redis_client = await _ensure_redis()
    info = await redis_client.info("memory")
    summary = _redis_memory_summary(info)
    if summary["warning"]:
        _log(logging.WARNING, "redis_memory_warning",
             used=summary["used_memory"],
             warn_limit=summary["warn_limit"],
             critical=summary["critical"])
    return summary


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

    memory = _redis_memory_summary(info)

    return {
        "status": "ok" if pong and not memory["critical"] else "degraded",
        "redis": {
            "connected": bool(pong),
            **memory,
        },
        "ts_ms": int(time.time() * 1000),
    }


def _read_cpu_times() -> list[tuple[int, int]]:
    """/proc/stat → [(idle, total)] (전체, cpu0, cpu1, ...). 컨테이너에서도 호스트 전체 값."""
    rows = []
    with open("/proc/stat") as f:
        for line in f:
            if not line.startswith("cpu"):
                break
            vals = [int(v) for v in line.split()[1:]]
            rows.append((vals[3] + vals[4], sum(vals)))  # idle + iowait
    return rows


@app.get("/system/resources")
async def get_system_resources():
    """RPi5 호스트 CPU·온도·메모리·디스크. 조회 시점 계산만 하고 어디에도 저장하지 않는다."""
    t0 = _read_cpu_times()
    await asyncio.sleep(0.5)
    t1 = _read_cpu_times()
    usage = [
        round(100.0 * (1 - (i1 - i0) / (s1 - s0)), 1) if s1 > s0 else 0.0
        for (i0, s0), (i1, s1) in zip(t0, t1)
    ]

    mem = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, val = line.split(":", 1)
            mem[key] = int(val.split()[0]) * 1024

    temp_c = None
    with suppress(OSError, ValueError):
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            temp_c = round(int(f.read().strip()) / 1000.0, 1)

    disk = shutil.disk_usage("/")
    return {
        "cpu": {"percent": usage[0], "per_core": usage[1:], "load_avg": [round(v, 2) for v in os.getloadavg()]},
        "temp_c": temp_c,
        "memory": {"total": mem.get("MemTotal", 0), "available": mem.get("MemAvailable", 0)},
        "disk": {"total": disk.total, "used": disk.used, "free": disk.free},
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

    # 오디오 워커는 audio:events를 독립 구독한다. 빈 CSI를 추가하면 실제 노드의
    # 100프레임 창과 packet-gap 판정을 오염하므로 trigger_ai는 하위 호환 입력으로만 받는다.
    csi_id = None

    return {
        "status": "ok",
        "audio_event_id": audio_id,
        "csi_event_id": csi_id,
        "trigger_ai_ignored": bool(body.trigger_ai),
        "node_id": body.node_id,
        "sample_rate": body.sample_rate,
        "text_len": len(text_ko),
        "waveform_samples": len(clean_waveform),
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
