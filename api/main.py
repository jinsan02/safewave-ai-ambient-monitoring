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
SETTINGS_KEY = "sys:settings"
TOKEN_KEY_PREFIX = "fcm:token:"
MINUTE_AGG_PREFIX = "agg:minute:"
TOKEN_TTL_SECONDS = int(os.getenv("TOKEN_TTL_SECONDS", "3600"))
SETTINGS_TTL_SECONDS = int(os.getenv("SETTINGS_TTL_SECONDS", "3600"))
ALERT_DEDUP_TTL_SECONDS = int(os.getenv("ALERT_DEDUP_TTL_SECONDS", "3600"))

app = FastAPI(title="rp5 API")

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


async def _alert_worker(redis_client):
    last_id = "$"
    while True:
        try:
            entries = await redis_client.xread({EMERGENCY_STREAM: last_id}, count=10, block=1000)
            if not entries:
                continue

            for _stream, messages in entries:
                for msg_id, fields in messages:
                    payload = _normalize_emergency(_parse_result_payload(fields.get("data", "")), msg_id)
                    last_id = msg_id

                    if payload["risk_level"] != "critical":
                        continue

                    for device_id, token in await _list_registered_tokens(redis_client):
                        dedupe_key = f"notify:sent:{msg_id}:{device_id}"
                        claimed = await redis_client.set(dedupe_key, "1", ex=ALERT_DEDUP_TTL_SECONDS, nx=True)
                        if not claimed:
                            continue
                        try:
                            await asyncio.to_thread(
                                send_risk_notification,
                                token,
                                payload["risk_score"],
                                payload["risk_level"],
                                True,
                                {"summary": payload["summary"], "ts_ms": payload["ts_ms"]},
                            )
                        except Exception as exc:
                            print(f"[api] FCM send failed for {device_id}: {exc}", flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[api] alert worker error: {exc}", flush=True)
            await asyncio.sleep(1)


@app.on_event("startup")
async def startup():
    app.state.redis = aioredis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        decode_responses=True,
        socket_connect_timeout=5,
    )
    app.state.alert_worker = asyncio.create_task(_alert_worker(app.state.redis))


@app.on_event("shutdown")
async def shutdown():
    worker = getattr(app.state, "alert_worker", None)
    if worker is not None:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker
    await app.state.redis.aclose()


@app.get("/")
async def root():
    return {"service": "rp5-api", "status": "ok"}


@app.get("/status")
async def get_status():
    entries = await app.state.redis.xrevrange(RESULT_STREAM, count=1)
    if not entries:
        # 204 응답은 body를 포함할 수 없어 h11 프로토콜 오류가 발생하므로 200으로 반환
        return JSONResponse({"message": "no data yet"}, status_code=200)
    msg_id, fields = entries[0]
    payload = _parse_result_payload(fields.get("data", ""))
    return _normalize_snapshot(payload, msg_id)


@app.get("/logs")
async def get_logs(n: int = 60):
    n = max(1, min(n, 36000))
    entries = await app.state.redis.xrevrange(RESULT_STREAM, count=n)
    result = []
    for msg_id, fields in entries:
        payload = _parse_result_payload(fields.get("data", ""))
        result.append(_normalize_snapshot(payload, msg_id))
    return result


@app.websocket("/ws/monitor")
async def ws_monitor(websocket: WebSocket):
    await websocket.accept()
    r = app.state.redis
    last_id = "$"

    try:
        while True:
            entries = await r.xread({RESULT_STREAM: last_id}, count=10, block=1000)
            if entries:
                for _stream, messages in entries:
                    for msg_id, fields in messages:
                        payload = _parse_result_payload(fields.get("data", ""))
                        await websocket.send_json(_normalize_snapshot(payload, msg_id))
                        last_id = msg_id
            else:
                await websocket.send_json({"ping": True})

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        await websocket.close(code=1011, reason=str(exc))


@app.get("/settings")
async def get_settings():
    raw = await app.state.redis.get(SETTINGS_KEY)
    if raw:
        return json.loads(raw)
    return SystemSettings().model_dump()


@app.post("/settings")
async def update_settings(settings: SystemSettings):
    await app.state.redis.set(SETTINGS_KEY, json.dumps(settings.model_dump()), ex=SETTINGS_TTL_SECONDS)
    return {"status": "updated", "settings": settings.model_dump()}


@app.get("/nodes/health")
async def get_node_health():
    now = time.time()
    health = {}
    for i in range(1, 7):
        last_seen = await app.state.redis.get(f"node:{i}:last_seen")
        if last_seen and (now - float(last_seen)) < 5:
            health[f"node_{i}"] = {"status": "online", "age_s": round(now - float(last_seen), 2)}
        else:
            health[f"node_{i}"] = {"status": "offline", "age_s": None}
    return health


@app.get("/history")
async def get_history(n: int = 100, level: str = "warning"):
    n = max(1, min(n, 3600))
    if level not in ("warning", "critical"):
        return JSONResponse({"error": "level must be 'warning' or 'critical'"}, status_code=400)

    entries = await app.state.redis.xrevrange(EMERGENCY_STREAM, count=3600)
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
    chart = []
    current_minute = int(time.time() // 60)

    for minute_key in range(current_minute - minutes + 1, current_minute + 1):
        raw_bucket = await app.state.redis.hgetall(f"{MINUTE_AGG_PREFIX}{minute_key}")
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
            "samples": risk_count,
        })

    return sorted(chart, key=lambda item: item["ts"])


@app.post("/auth/register-token")
async def register_fcm_token(body: TokenRegistration):
    await app.state.redis.set(f"{TOKEN_KEY_PREFIX}{body.device_id}", body.token, ex=TOKEN_TTL_SECONDS)
    return {"status": "registered", "device_id": body.device_id, "ttl_seconds": TOKEN_TTL_SECONDS}


@app.get("/auth/tokens")
async def list_fcm_tokens():
    tokens = await _list_registered_tokens(app.state.redis)
    return {"devices": [device_id for device_id, _token in tokens], "count": len(tokens)}


@app.get("/notify/threshold")
async def get_notify_threshold():
    threshold = await load_risk_threshold(app.state.redis)
    return {"risk_threshold": threshold}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
