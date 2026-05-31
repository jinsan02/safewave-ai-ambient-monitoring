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

if not firebase_admin._apps:
    if os.path.exists(_KEY_PATH):
        cred = credentials.Certificate(_KEY_PATH)
        firebase_admin.initialize_app(cred)
    else:
        # 키 파일 없으면 애플리케이션 기본 자격증명 시도 (개발 환경)
        try:
            firebase_admin.initialize_app()
        except Exception:
            pass  # FCM 기능은 키 없이는 동작 안 함 — 나머지 API는 정상 제공

_RISK_THRESHOLD = float(os.getenv("FCM_RISK_THRESHOLD", "0.6"))


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


def build_risk_message(risk_score: float, risk_level: str, emergency: bool) -> tuple[str, str]:
    if risk_level == "critical" or emergency or risk_score >= 0.85:
        return (
            "응급 상황 감지",
            f"위험 점수 {risk_score:.2f} - 즉각 확인이 필요합니다.",
        )

    return (
        "이상 징후 감지",
        f"위험 점수 {risk_score:.2f} - 상태를 확인하세요.",
    )


def send_risk_notification(token: str, risk_score: float, risk_level: str,
                           emergency: bool = False, extra: dict[str, Any] | None = None) -> str:
    title, body = build_risk_message(risk_score, risk_level, emergency)
    payload = {"risk_score": risk_score, "risk_level": risk_level, "emergency": emergency}
    if extra:
        payload.update(extra)
    is_critical = risk_level == "critical" or emergency or risk_score >= 0.85
    return _send_fcm(token, title, body, payload, critical=is_critical)


# ── 공통 전송 함수 ────────────────────────────────────────────
def _send_fcm(token: str, title: str, body: str, extra: dict[str, Any] | None = None,
              critical: bool = False) -> str:
    if extra is None:
        extra = {}
    if not firebase_admin._apps:
        raise RuntimeError("Firebase not initialized — key file missing")

    # Android: emergency_alarm 채널 + 잠금화면 노출 + 최고 우선순위
    android_notif = messaging.AndroidNotification(
        channel_id="emergency_alarm" if critical else "safety_alert",
        priority="max" if critical else "high",
        visibility="public",
        notification_count=1,
    )
    # iOS: critical=True는 무음/방해금지 우회 (앱 entitlement 필요)
    apns_sound = messaging.CriticalSound(name="alarm.wav", critical=True, volume=1.0) if critical \
        else messaging.CriticalSound(name="default")
    apns_payload = messaging.APNSPayload(
        aps=messaging.Aps(sound=apns_sound, badge=1)
    )

    msg = messaging.Message(
        notification=messaging.Notification(title=title, body=body),
        data={k: str(v) for k, v in extra.items()},
        token=token,
        android=messaging.AndroidConfig(
            priority="high",
            notification=android_notif,
        ),
        apns=messaging.APNSConfig(payload=apns_payload),
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
