"""
api/notifier.py
FCM 푸시 알림 + 위험 점수 임계값 기반 자동 발송.
- POST /notify/send   : 직접 알림 전송
- POST /notify/check  : risk_score 기준 조건부 전송
"""

import json
import os
from typing import Any

import firebase_admin
from firebase_admin import credentials, messaging
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

router = APIRouter()
SETTINGS_KEY = "sys:settings"

# ── Firebase 초기화 ──────────────────────────────────────────
_KEY_PATH = os.getenv("FIREBASE_KEY_PATH", "/app/auth/firebase_key.json")

# 키 파일이 없으면 FCM은 비활성이다. 기본 자격증명으로 반쯤 초기화하면 발송 때마다
# "Project ID is required" 오류만 나므로 초기화하지 않고, API 시작 로그로 알린다.
FCM_READY = False
if not firebase_admin._apps and os.path.exists(_KEY_PATH):
    firebase_admin.initialize_app(credentials.Certificate(_KEY_PATH))
FCM_READY = bool(firebase_admin._apps)

_RISK_THRESHOLD = float(os.getenv("FCM_RISK_THRESHOLD", "0.6"))
# true: data 전용 고우선순위 메시지. 앱이 꺼져 있어도 onMessageReceived가 호출돼
# 전체 화면 경보·반복 사이렌을 앱이 직접 만든다. 보호자 앱 배포 전에는 false(시스템 알림 표시).
FCM_DATA_ONLY = os.getenv("FCM_DATA_ONLY", "false").lower() in ("1", "true", "yes")
FCM_TTL_SECONDS = int(os.getenv("FCM_TTL_SECONDS", "600"))


# ── 스키마 ───────────────────────────────────────────────────
class NotifyRequest(BaseModel):
    token: str
    title: str
    body: str
    data: dict[str, Any] = Field(default_factory=dict)


class RiskPayload(BaseModel):
    token: str
    risk_score: float
    risk_level: str = "normal"
    emergency: bool = False


async def load_risk_threshold(redis_client) -> float:
    if redis_client is None:
        return _RISK_THRESHOLD

    try:
        raw = await redis_client.get(SETTINGS_KEY)
        if raw:
            return float(json.loads(raw).get("risk_threshold", _RISK_THRESHOLD))
    except Exception:
        pass
    return _RISK_THRESHOLD


def build_risk_message(risk_score: float, risk_level: str, emergency: bool,
                       summary: str | None = None, node_id: Any = None) -> tuple[str, str]:
    where = f"[노드 {node_id}] " if node_id not in (None, "", 0) else ""
    reason = f"{summary} " if summary else ""
    if risk_level == "critical" or emergency or risk_score >= 0.85:
        return (
            "응급 상황 감지",
            f"{where}{reason}(위험 점수 {risk_score:.2f}) - 즉시 연락해 확인하세요.",
        )

    return (
        "이상 징후 감지",
        f"{where}{reason}(위험 점수 {risk_score:.2f}) - 상태를 확인하세요.",
    )


def send_risk_notification(token: str, risk_score: float, risk_level: str,
                           emergency: bool = False, extra: dict[str, Any] | None = None) -> str:
    extra = extra or {}
    title, body = build_risk_message(
        risk_score, risk_level, emergency, extra.get("summary"), extra.get("node_id")
    )
    is_critical = risk_level == "critical" or emergency or risk_score >= 0.85
    payload = {"type": "emergency" if is_critical else "warning",
               "risk_score": risk_score, "risk_level": risk_level, "emergency": emergency}
    payload.update({k: v for k, v in extra.items() if v is not None})
    return _send_fcm(token, title, body, payload, critical=is_critical)


def send_voice_ok_notification(token: str, node_id: Any, ts_ms: int, transcript: str | None,
                               msg_id: str | None = None) -> str:
    """응급 알림 뒤 대상자가 괜찮다고 답한 경우의 후속 알림. 응급 채널을 쓰지 않는다."""
    said = f" (\"{transcript[:30]}\")" if transcript else ""
    body = f"[노드 {node_id}] 대상자가 음성으로 괜찮다고 응답했습니다{said}. 가능하면 전화로 한 번 더 확인하세요."
    payload = {"type": "voice_ok", "node_id": node_id, "ts_ms": ts_ms, "emergency": False}
    if msg_id:
        payload["msg_id"] = msg_id
    return _send_fcm(token, "대상자 응답 확인", body, payload, critical=False)


def send_heartbeat_notification(token: str, nodes_online: int, nodes_expected: int) -> str:
    """정기 정상 동작 신호. 앱은 이 신호가 끊기면 시스템 정지를 의심한다."""
    body = f"SafeWave가 정상 동작 중입니다 (센서 {nodes_online}/{nodes_expected})."
    payload = {"type": "heartbeat", "nodes_online": nodes_online, "nodes_expected": nodes_expected}
    return _send_fcm(token, "SafeWave 정상 동작", body, payload, critical=False)


def build_fcm_fields(title: str, body: str, extra: dict[str, Any] | None, critical: bool,
                     data_only: bool, ttl_seconds: int = 600) -> dict[str, Any]:
    """FCM 메시지 구성 값을 만든다(Firebase 객체 생성과 분리한 순수 함수).

    data 값은 FCM 규격상 모두 문자열이다. data 전용이면 제목·본문도 data에 넣어 앱이 알림을 만든다.
    """
    data = {k: str(v) for k, v in (extra or {}).items() if v is not None}
    fields: dict[str, Any] = {
        "data": data,
        "ttl_seconds": ttl_seconds,
        "channel_id": "emergency_alarm" if critical else "safety_alert",
    }
    if data_only:
        data["title"] = title
        data["body"] = body
        fields["notification"] = None
    else:
        fields["notification"] = (title, body)
    return fields


# ── 공통 전송 함수 ────────────────────────────────────────────
def _send_fcm(token: str, title: str, body: str, extra: dict[str, Any] | None = None,
              critical: bool = False) -> str:
    if not firebase_admin._apps:
        raise RuntimeError("Firebase not initialized — key file missing")

    fields = build_fcm_fields(title, body, extra, critical, FCM_DATA_ONLY, FCM_TTL_SECONDS)
    if fields["notification"] is None:
        # data 전용: 알림 표시는 보호자 앱이 채널·전체 화면 경보로 직접 한다.
        msg = messaging.Message(
            data=fields["data"],
            token=token,
            android=messaging.AndroidConfig(priority="high", ttl=fields["ttl_seconds"]),
        )
        return messaging.send(msg)

    # 시스템 알림 방식(앱 배포 전 호환): emergency_alarm 채널 + 잠금화면 노출 + 최고 우선순위
    android_notif = messaging.AndroidNotification(
        channel_id=fields["channel_id"],
        priority="max" if critical else "high",
        visibility="public",
        notification_count=1,
    )
    msg = messaging.Message(
        notification=messaging.Notification(title=title, body=body),
        data=fields["data"],
        token=token,
        android=messaging.AndroidConfig(
            priority="high",
            ttl=fields["ttl_seconds"],
            notification=android_notif,
        ),
    )
    return messaging.send(msg)


# ── 엔드포인트 ────────────────────────────────────────────────
@router.post("/send")
async def send_notification(req: NotifyRequest):
    """FCM 직접 전송."""
    try:
        msg_id = _send_fcm(req.token, req.title, req.body, req.data)
        return {"ok": True, "message_id": msg_id}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@router.post("/check")
async def check_and_notify(payload: RiskPayload, request: Request):
    """
    risk_score 가 임계값(기본 0.6) 이상일 때만 FCM 발송.
    - warning  (0.6 ≤ score < 0.85): "주의" 알림
    - critical (score ≥ 0.85)       : "응급" 알림
    """
    threshold = await load_risk_threshold(request.app.state.redis)
    if payload.risk_score < threshold:
        return {"ok": True, "sent": False, "reason": "below_threshold"}

    try:
        msg_id = send_risk_notification(
            payload.token,
            payload.risk_score,
            payload.risk_level,
            payload.emergency,
        )
        return {"ok": True, "sent": True, "message_id": msg_id}
    except Exception as exc:
        return JSONResponse({"ok": False, "sent": False, "error": str(exc)}, status_code=500)


class TestNotifyRequest(BaseModel):
    token: str


@router.post("/test")
async def test_notification(req: TestNotifyRequest):
    """앱에서 FCM 수신 연결을 확인하는 테스트 알림 발송."""
    try:
        msg_id = _send_fcm(
            req.token,
            title="✅ RP5 알림 테스트",
            body="갤럭시 플립 4와 RP5 서버가 정상 연결됐습니다.",
            extra={"type": "test"},
        )
        return {"ok": True, "message_id": msg_id}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
