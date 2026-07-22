"""MQTT 기반 경량 TTS 워커.

두 가지 경로로 TTS를 처리한다:
1. MQTT safewave/ai/result 구독 → warning/critical 이벤트 시 자동 안내음 (쿨다운 120s)
2. Redis tts:speak:queue BLPOP → _alert_worker가 직접 요청한 응급 TTS (노드별 쿨다운 10s)

생성된 MP3는 volumes/logs/tts 에 저장하고 mpg123으로 즉시 재생한다.
재생 완료 후 user:voice_response:{node_id} 키를 설정해 _alert_worker가 응답 감지할 수 있게 한다.
"""

import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import edge_tts
import paho.mqtt.client as mqtt
import redis.asyncio as aioredis

# cp949 등 UTF-8이 아닌 콘솔에서 로그 문자가 UnicodeEncodeError를 일으켜
# 발화→voice_response 신호 플로우가 중단되지 않도록 치환 처리한다.
for _stream in (sys.stdout, sys.stderr):
    if _stream and hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")


MQTT_HOST       = os.getenv("MQTT_HOST", "mqtt")
MQTT_PORT       = int(os.getenv("MQTT_PORT", "1883"))
MQTT_CLIENT_ID  = os.getenv("MQTT_TTS_CLIENT_ID", "rp5-tts-worker")
MQTT_BASE_TOPIC = os.getenv("MQTT_BASE_TOPIC", "safewave")
REDIS_HOST      = os.getenv("REDIS_HOST", "db")
REDIS_PORT      = int(os.getenv("REDIS_PORT", "6379"))

RESULT_TOPIC     = f"{MQTT_BASE_TOPIC}/ai/result"
FEEDBACK_TOPIC   = f"{MQTT_BASE_TOPIC}/feedback"
TTS_STATUS_TOPIC = f"{MQTT_BASE_TOPIC}/tts/status"


def _make_client(client_id: str) -> mqtt.Client:
    return mqtt.Client(client_id=client_id, protocol=mqtt.MQTTv311)


def _publish_json(client: mqtt.Client, topic_str: str, payload: dict):
    try:
        client.publish(topic_str, json.dumps(payload, ensure_ascii=False))
    except Exception as exc:
        print(f"[tts] mqtt publish error: {exc}")

VOICE        = os.getenv("TTS_VOICE", "ko-KR-SunHiNeural")
COOLDOWN_SEC = int(os.getenv("TTS_COOLDOWN_SEC", "120"))
OUT_DIR      = Path(os.getenv("TTS_OUTPUT_DIR", "volumes/logs/tts"))

TTS_SPEAK_QUEUE      = "tts:speak:queue"
VOICE_RESP_PREFIX    = "user:voice_response:"
VOICE_RESP_TTL_SEC   = 5
TTS_QUEUE_COOLDOWN_SEC = int(os.getenv("TTS_QUEUE_COOLDOWN_SEC", "10"))

# 안내 문구 — 마이크가 재생음을 재녹음(에코)해도 api Phase 2 의도 분류
# 키워드(위험/응급/괜찮 등)에 걸리지 않도록 중립 단어만 사용한다.
NEUTRAL_PROMPT_TEXT = "이상이 감지되었습니다. 상태를 말씀해 주세요."


def should_speak(payload: dict) -> bool:
    level = str(payload.get("risk_level", "normal")).lower()
    return level in {"warning", "critical"}


async def synthesize(text: str) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = OUT_DIR / f"tts_{ts}.mp3"
    # 일시적 DNS/네트워크 실패 대비 3회 재시도 (응급 안내가 단발 실패로 누락되지 않도록)
    last_exc = None
    for attempt in range(3):
        try:
            communicate = edge_tts.Communicate(text=text, voice=VOICE)
            await communicate.save(str(out_path))
            return out_path
        except Exception as exc:
            last_exc = exc
            print(f"[tts] synth retry {attempt + 1}/3: {exc}")
            await asyncio.sleep(1.0 + attempt)
    raise last_exc


async def play_audio(path: Path):
    """mpg123으로 MP3 재생 (RPi5). Windows 개발 호스트는 PowerShell MediaPlayer 폴백."""
    try:
        if sys.platform == "win32":
            ps = (
                "Add-Type -AssemblyName presentationCore; "
                f"$p = New-Object System.Windows.Media.MediaPlayer; $p.Open('{path}'); $p.Play(); "
                "Start-Sleep -Seconds 1; "
                "while ($p.NaturalDuration.HasTimeSpan -and $p.Position -lt $p.NaturalDuration.TimeSpan) "
                "{ Start-Sleep -Milliseconds 200 }; $p.Close()"
            )
            proc = await asyncio.create_subprocess_exec(
                "powershell", "-NoProfile", "-Command", ps,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            return
        proc = await asyncio.create_subprocess_exec(
            "mpg123", "-q", str(path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
    except FileNotFoundError:
        print("[tts] mpg123 not found — audio playback skipped")
    except Exception as exc:
        print(f"[tts] playback error: {exc}")


async def speak_and_signal(
    r: aioredis.Redis,
    client: mqtt.Client,
    text: str,
    node_id: int,
    payload: dict | None = None,
):
    """TTS 합성 → 재생 → 응답 대기 신호 설정."""
    out_path = await synthesize(text)
    await play_audio(out_path)

    resp_key = f"{VOICE_RESP_PREFIX}{node_id}"
    await r.lpush(resp_key, "1")
    await r.expire(resp_key, VOICE_RESP_TTL_SEC)

    _publish_json(
        client,
        TTS_STATUS_TOPIC,
        {
            "node_id": node_id,
            "text": text,
            "file": str(out_path),
            "risk_level": (payload or {}).get("risk_level"),
            "risk_score": (payload or {}).get("risk_score"),
        },
    )


async def _mqtt_loop(client: mqtt.Client, r: aioredis.Redis):
    """MQTT safewave/ai/result → 쿨다운 적용 자동 TTS."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[dict] = asyncio.Queue()
    last_spoke_at: dict[int, float] = {}

    def on_connect(_c, _u, _f, rc):
        print(f"[tts] MQTT connected rc={rc}")
        _c.subscribe(RESULT_TOPIC)
        _c.subscribe(FEEDBACK_TOPIC)

    def on_message(_c, _u, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8", errors="ignore"))
        except Exception:
            return
        loop.call_soon_threadsafe(queue.put_nowait, {"topic": msg.topic, "payload": payload})

    client.on_connect = on_connect
    client.on_message = on_message
    try:
        client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
    except Exception as exc:
        print(f"[tts] MQTT connect failed: {exc} — MQTT loop disabled")
        return
    client.loop_start()

    while True:
        item = await queue.get()
        if item["topic"] == FEEDBACK_TOPIC:
            continue
        payload = item["payload"]
        if not should_speak(payload):
            continue
        node_id = int(payload.get("node_id", 0) or 0)
        now = loop.time()
        if now - last_spoke_at.get(node_id, 0.0) < COOLDOWN_SEC:
            continue
        last_spoke_at[node_id] = now
        text = NEUTRAL_PROMPT_TEXT
        try:
            await speak_and_signal(r, client, text, node_id, payload)
        except Exception as exc:
            _publish_json(client, TTS_STATUS_TOPIC, {"error": str(exc), "node_id": node_id})
            print(f"[tts] mqtt synth failed: {exc}")


async def _redis_speak_loop(client: mqtt.Client, r: aioredis.Redis):
    """Redis tts:speak:queue → 응급 직접 TTS (노드별 짧은 쿨다운)."""
    print("[tts] redis speak loop started")
    loop = asyncio.get_running_loop()
    last_spoke_at: dict[int, float] = {}
    while True:
        try:
            item = await r.blpop(TTS_SPEAK_QUEUE, timeout=5)
            if not item:
                continue
            _, raw = item
            data = json.loads(raw)
            text    = data.get("text", NEUTRAL_PROMPT_TEXT)
            node_id = int(data.get("node_id", 0))
            now = loop.time()
            if now - last_spoke_at.get(node_id, 0.0) < TTS_QUEUE_COOLDOWN_SEC:
                print(f"[tts] queue cooldown skip node={node_id}")
                continue
            last_spoke_at[node_id] = now
            try:
                await speak_and_signal(r, client, text, node_id)
                print(f"[tts] emergency spoke node={node_id}")
            except Exception as exc:
                print(f"[tts] redis speak failed: {exc}")
        except Exception as exc:
            print(f"[tts] redis loop error: {exc}")
            await asyncio.sleep(2)


async def _connect_redis() -> aioredis.Redis:
    while True:
        try:
            r = aioredis.Redis(host=REDIS_HOST, port=REDIS_PORT,
                               decode_responses=True, socket_connect_timeout=3)
            await r.ping()
            print(f"[tts] Redis connected: {REDIS_HOST}:{REDIS_PORT}")
            return r
        except Exception as exc:
            print(f"[tts] Redis not ready ({exc}), retry in 2s...")
            await asyncio.sleep(2)


async def main():
    r = await _connect_redis()
    client = _make_client(MQTT_CLIENT_ID)
    await asyncio.gather(
        _mqtt_loop(client, r),
        _redis_speak_loop(client, r),
    )


if __name__ == "__main__":
    asyncio.run(main())
