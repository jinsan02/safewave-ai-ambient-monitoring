"""MQTT 기반 경량 TTS 워커.

ai/result를 구독해서 warning/critical 이벤트에 대해 한국어 안내 음성을 생성한다.
생성된 파일은 volumes/logs/tts 아래에 저장되고, 상태는 MQTT로 다시 발행한다.
"""

import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import edge_tts
import paho.mqtt.client as mqtt

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ai"))

from mqtt_helper import make_client, publish_json, topic


MQTT_HOST = os.getenv("MQTT_HOST", "mqtt")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_CLIENT_ID = os.getenv("MQTT_TTS_CLIENT_ID", "rp5-tts-worker")
RESULT_TOPIC = topic("ai/result")
FEEDBACK_TOPIC = topic("feedback")
TTS_STATUS_TOPIC = topic("tts/status")
VOICE = os.getenv("TTS_VOICE", "ko-KR-SunHiNeural")
COOLDOWN_SEC = int(os.getenv("TTS_COOLDOWN_SEC", "120"))
OUT_DIR = Path(os.getenv("TTS_OUTPUT_DIR", "volumes/logs/tts"))


def should_speak(payload: dict) -> bool:
    level = str(payload.get("risk_level", "normal")).lower()
    return level in {"warning", "critical"}


async def synthesize(text: str) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = OUT_DIR / f"tts_{ts}.mp3"
    communicate = edge_tts.Communicate(text=text, voice=VOICE)
    await communicate.save(str(out_path))
    return out_path


async def speak_warning(client: mqtt.Client, payload: dict):
    node_id = int(payload.get("node_id", 0) or 0)
    text = "괜찮으세요? 도움이 필요하시면 말씀해 주세요."
    out_path = await synthesize(text)
    publish_json(
        client,
        TTS_STATUS_TOPIC,
        {
            "node_id": node_id,
            "text": text,
            "file": str(out_path),
            "risk_level": payload.get("risk_level"),
            "risk_score": payload.get("risk_score"),
        },
    )


async def main():
    client = make_client(MQTT_CLIENT_ID)
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[dict] = asyncio.Queue()
    last_spoke_at: dict[int, float] = {}

    def on_connect(_client, _userdata, _flags, rc):
        print(f"[tts] MQTT connected rc={rc}")
        _client.subscribe(RESULT_TOPIC)
        _client.subscribe(FEEDBACK_TOPIC)

    def on_message(_client, _userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8", errors="ignore"))
        except Exception:
            return
        loop.call_soon_threadsafe(queue.put_nowait, {"topic": msg.topic, "payload": payload})

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
    client.loop_start()

    print("[tts] worker started")
    while True:
        item = await queue.get()
        topic_name = item["topic"]
        payload = item["payload"]

        if topic_name == FEEDBACK_TOPIC:
            print(f"[tts] feedback received: {payload}")
            continue

        if not should_speak(payload):
            continue

        node_id = int(payload.get("node_id", 0) or 0)
        now = asyncio.get_running_loop().time()
        if now - last_spoke_at.get(node_id, 0.0) < COOLDOWN_SEC:
            continue

        last_spoke_at[node_id] = now
        try:
            await speak_warning(client, payload)
        except Exception as exc:
            publish_json(client, TTS_STATUS_TOPIC, {"error": str(exc), "node_id": node_id})
            print(f"[tts] synth failed: {exc}")


if __name__ == "__main__":
    asyncio.run(main())