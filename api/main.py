"""
api/main.py
갤럭시 플립 4 앱 연동 FastAPI 서버.
- GET  /status              : 최신 AI 판단 결과
- GET  /logs                : 최근 N건 이력
- GET  /history             : 응급/경고 이벤트 이력
- GET  /settings            : 시스템 설정 조회
- POST /settings            : 시스템 설정 변경 (민감도, 노드 ON/OFF, AI 활성화)
- GET  /nodes/health        : 노드별 생존 상태
- GET  /charts/minute       : 분 단위 평균 차트 데이터
- POST /auth/register-token : FCM 토큰 등록
- WS   /ws/monitor          : 실시간 ai:result 스트리밍
"""

import ast
import json
import os
import time

import redis.asyncio as aioredis
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
import uvicorn

from notifier import router as notify_router


# ── 설정 스키마 ──────────────────────────────────────────────
class SystemSettings(BaseModel):
    risk_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    active_nodes: list[int] = Field(default=[1, 2, 3, 4, 5, 6])
    ai_enabled: bool = True


class TokenRegistration(BaseModel):
    token: str
    device_id: str = "galaxy_flip4"


def _parse_result_payload(raw: str) -> dict:
    """ai:result의 문자열 payload를 dict로 안전하게 복원."""
    if not raw:
        return {}
    try:
        # 우선 표준 JSON 시도
        return json.loads(raw)
    except Exception:
        try:
            # AI가 str(dict) 형태로 저장한 경우 처리
            parsed = ast.literal_eval(raw)
            return parsed if isinstance(parsed, dict) else {"raw": raw}
        except Exception:
            return {"raw": raw}

REDIS_HOST  = os.getenv("REDIS_HOST", "db")
REDIS_PORT  = int(os.getenv("REDIS_PORT", 6379))
RESULT_STREAM = "ai:result"
SETTINGS_KEY  = "sys:settings"
TOKEN_KEY     = "fcm:tokens"

app = FastAPI(title="rp5 API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(notify_router, prefix="/notify")


# ── Redis 풀 ─────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    app.state.redis = aioredis.Redis(
        host=REDIS_HOST, port=REDIS_PORT,
        decode_responses=True, socket_connect_timeout=5,
    )


@app.on_event("shutdown")
async def shutdown():
    await app.state.redis.aclose()


# ── REST ─────────────────────────────────────────────────────
@app.get("/")
async def root():
    return {"service": "rp5-api", "status": "ok"}


@app.get("/status")
async def get_status():
    """ai:result 스트림에서 가장 최신 1건 반환."""
    entries = await app.state.redis.xrevrange(RESULT_STREAM, count=1)
    if not entries:
        return JSONResponse({"message": "no data yet"}, status_code=204)
    _msg_id, fields = entries[0]
    payload = _parse_result_payload(fields.get("data", ""))
    return payload


@app.get("/logs")
async def get_logs(n: int = 60):
    """최근 n건 ai:result 이력 반환 (기본 60건 ≈ 1분)."""
    n = max(1, min(n, 3600))
    entries = await app.state.redis.xrevrange(RESULT_STREAM, count=n)
    result = []
    for msg_id, fields in entries:
        payload = _parse_result_payload(fields.get("data", ""))
        payload["_id"] = msg_id
        result.append(payload)
    return result


# ── WebSocket ────────────────────────────────────────────────
@app.websocket("/ws/monitor")
async def ws_monitor(websocket: WebSocket):
    """
    ai:result 스트림을 실시간으로 앱에 푸시.
    새 데이터가 없으면 최대 1초 블로킹 후 재시도.
    """
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
                        payload["_id"] = msg_id
                        await websocket.send_json(payload)
                        last_id = msg_id
            else:
                # 데이터 없음 — keepalive ping
                await websocket.send_json({"ping": True})

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        await websocket.close(code=1011, reason=str(exc))


# ── 설정 관리 ─────────────────────────────────────────────────
@app.get("/settings")
async def get_settings():
    """현재 시스템 설정 반환 (기본값 포함)."""
    raw = await app.state.redis.get(SETTINGS_KEY)
    if raw:
        return json.loads(raw)
    return SystemSettings().model_dump()


@app.post("/settings")
async def update_settings(settings: SystemSettings):
    """앱에서 보낸 설정값을 Redis에 저장 — AI 엔진이 다음 루프에서 반영."""
    await app.state.redis.set(SETTINGS_KEY, json.dumps(settings.model_dump()))
    return {"status": "updated", "settings": settings.model_dump()}


# ── 노드 상태 ─────────────────────────────────────────────────
@app.get("/nodes/health")
async def get_node_health():
    """각 노드의 마지막 수신 시간 기준 온라인/오프라인 판정 (5초 기준)."""
    now = time.time()
    health = {}
    for i in range(1, 7):
        last_seen = await app.state.redis.get(f"node:{i}:last_seen")
        if last_seen and (now - float(last_seen)) < 5:
            health[f"node_{i}"] = {"status": "online", "age_s": round(now - float(last_seen), 2)}
        else:
            health[f"node_{i}"] = {"status": "offline", "age_s": None}
    return health


# ── 응급 이력 ─────────────────────────────────────────────────
@app.get("/history")
async def get_history(n: int = 100, level: str = "warning"):
    """
    응급/경고 이벤트만 필터링한 이력 반환.
    level: 'warning'(경고 이상) 또는 'critical'(응급만)
    """
    n = max(1, min(n, 3600))
    if level not in ("warning", "critical"):
        return JSONResponse({"error": "level must be 'warning' or 'critical'"}, status_code=400)
    entries = await app.state.redis.xrevrange(RESULT_STREAM, count=3600)
    result = []
    for msg_id, fields in entries:
        payload = _parse_result_payload(fields.get("data", ""))
        if "raw" in payload:
            continue
        rl = payload.get("risk_level", "normal")
        if level == "critical" and rl != "critical":
            continue
        if level == "warning" and rl not in ("warning", "critical"):
            continue
        payload["_id"] = msg_id
        # Redis 스트림 ID에서 Unix 타임스탬프 추출 (ms-seq 형식)
        try:
            payload["_ts"] = int(msg_id.split("-")[0]) / 1000
        except Exception:
            pass
        result.append(payload)
        if len(result) >= n:
            break
    return result


# ── 차트 데이터 (분 단위 평균) ────────────────────────────────
@app.get("/charts/minute")
async def get_minute_charts(minutes: int = 10):
    """
    최근 N분 간 1분 단위 평균값 반환.
    앱에서 심박수/위험도 시계열 차트를 그릴 때 사용.
    """
    minutes = max(1, min(minutes, 60))
    entries = await app.state.redis.xrevrange(RESULT_STREAM, count=minutes * 120)

    buckets: dict = {}
    for msg_id, fields in entries:
        try:
            ts_ms = int(msg_id.split("-")[0])
            minute_key = ts_ms // 60000  # 분 단위 버킷
            payload = _parse_result_payload(fields.get("data", ""))
            if "raw" in payload:
                continue
        except Exception:
            continue

        ex = payload.get("experts", {})
        vital = ex.get("vital", {})
        bucket = buckets.setdefault(minute_key, {
            "ts": minute_key * 60,
            "risk_scores": [], "heart_rates": [], "breathing_rates": [],
        })
        if payload.get("risk_score") is not None:
            bucket["risk_scores"].append(float(payload["risk_score"]))
        if vital.get("heart_rate") is not None:
            bucket["heart_rates"].append(float(vital["heart_rate"]))
        if vital.get("breathing_rate") is not None:
            bucket["breathing_rates"].append(float(vital["breathing_rate"]))

    def _avg(lst): return round(sum(lst) / len(lst), 2) if lst else None

    chart = []
    for key in sorted(buckets.keys(), reverse=True)[:minutes]:
        b = buckets[key]
        chart.append({
            "ts":             b["ts"],
            "risk_score_avg": _avg(b["risk_scores"]),
            "heart_rate_avg": _avg(b["heart_rates"]),
            "breathing_rate_avg": _avg(b["breathing_rates"]),
            "samples":        len(b["risk_scores"]),
        })
    return sorted(chart, key=lambda x: x["ts"])


# ── FCM 토큰 등록 ─────────────────────────────────────────────
@app.post("/auth/register-token")
async def register_fcm_token(body: TokenRegistration):
    """앱 최초 설치 시 플립 4의 FCM 토큰을 서버에 등록."""
    await app.state.redis.hset(TOKEN_KEY, body.device_id, body.token)
    return {"status": "registered", "device_id": body.device_id}


@app.get("/auth/tokens")
async def list_fcm_tokens():
    """등록된 FCM 토큰 목록 (device_id만 노출)."""
    tokens = await app.state.redis.hgetall(TOKEN_KEY)
    return {"devices": list(tokens.keys()), "count": len(tokens)}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
