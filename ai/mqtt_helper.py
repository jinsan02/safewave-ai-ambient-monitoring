import json
import os
from typing import Any

import paho.mqtt.client as mqtt


MQTT_HOST = os.getenv("MQTT_HOST", "mqtt")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_BASE_TOPIC = os.getenv("MQTT_BASE_TOPIC", "safewave")


def make_client(client_id: str, on_message=None, on_connect=None) -> mqtt.Client:
    # paho-mqtt 2.x는 콜백 API 버전을 명시해야 한다. 기존 콜백 시그니처는 VERSION1.
    api_version = getattr(mqtt, "CallbackAPIVersion", None)
    if api_version is not None:
        client = mqtt.Client(api_version.VERSION1, client_id=client_id, protocol=mqtt.MQTTv311)
    else:
        client = mqtt.Client(client_id=client_id, protocol=mqtt.MQTTv311)
    if on_message is not None:
        client.on_message = on_message
    if on_connect is not None:
        client.on_connect = on_connect
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    return client


def publish_json(client: mqtt.Client, topic: str, payload: dict[str, Any], qos: int = 0, retain: bool = False):
    message = json.dumps(payload, ensure_ascii=False)
    return client.publish(topic, message, qos=qos, retain=retain)


def topic(name: str) -> str:
    return f"{MQTT_BASE_TOPIC}/{name.lstrip('/')}"