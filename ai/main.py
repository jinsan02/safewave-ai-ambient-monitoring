import os
import json
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import redis as _redis

from experts import m1_fall, m2_vital, m3_activity, m4_occupancy
from logic.qwen_05b import QwenLogic
from utils import TurboQuant


class AIEngine:
    def __init__(self):
        model_dir = os.getenv("MODEL_PATH", "/app/models")
        self.experts = {
            "fall": m1_fall.FallDetectionModel(
                os.path.join(model_dir, os.getenv("FALL_DETECTION_MODEL", "m1_fall.onnx"))
            ),
            "vital": m2_vital.VitalSignsModel(
                os.path.join(model_dir, os.getenv("VITAL_SENSING_MODEL", "m2_vital.onnx"))
            ),
            "activity": m3_activity.ActivityClassificationModel(
                os.path.join(model_dir, os.getenv("ACTIVITY_MODEL", "m3_activity.onnx"))
            ),
            "occupancy": m4_occupancy.OccupancyModel(
                os.path.join(model_dir, os.getenv("OCCUPANCY_MODEL", "m4_occupancy.onnx"))
            ),
        }
        self.qwen_logic = QwenLogic(
            os.path.join(model_dir, os.getenv("SLM_MODEL", "qwen_05b.onnx"))
        )
        self.turbo_quant = TurboQuant()

    def _run_expert(self, name, data):
        return name, self.experts[name].infer(data)

    def process_data(self, data):
        optimized = self.turbo_quant.optimize(data)
        results = {}

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(self._run_expert, name, optimized) for name in self.experts]
            for future in futures:
                name, output = future.result()
                results[name] = output

        return self.qwen_logic.evaluate(results)


def _load_settings(r) -> dict:
    """Redis에서 sys:settings 읽기 — 없으면 기본값 반환."""
    try:
        raw = r.get(b"sys:settings")
        if raw:
            return json.loads(raw.decode())
    except Exception:
        pass
    return {"risk_threshold": 0.6, "active_nodes": [1, 2, 3, 4, 5, 6], "ai_enabled": True}


def _apply_threshold(result: dict, threshold: float) -> dict:
    """동적 임계값으로 risk_level / emergency 재계산."""
    score = result.get("risk_score", 0.0)
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


def _connect_redis(redis_host: str, redis_port: int):
    while True:
        try:
            rc = _redis.Redis(host=redis_host, port=redis_port, decode_responses=False)
            rc.ping()
            print(f"[ai] connected to Redis at {redis_host}:{redis_port}", flush=True)
            return rc
        except _redis.exceptions.ConnectionError as exc:
            print(f"[ai] Redis not ready ({exc}), retry in 2s...", flush=True)
            time.sleep(2)


if __name__ == "__main__":
    redis_host = os.getenv("REDIS_HOST", "db")
    redis_port = int(os.getenv("REDIS_PORT", 6379))
    r = _connect_redis(redis_host, redis_port)
    ai_engine = AIEngine()
    print("[ai] starting inference loop — waiting for CSI data on stream 'csi:raw'", flush=True)

    last_id = "$"
    while True:
        try:
            # 매 루프마다 동적 설정 로드
            settings = _load_settings(r)
            active_nodes = set(settings.get("active_nodes", [1, 2, 3, 4, 5, 6]))
            threshold = float(settings.get("risk_threshold", 0.6))
            ai_enabled = bool(settings.get("ai_enabled", True))

            entries = r.xread({"csi:raw": last_id}, count=10, block=1000)
            if not entries:
                continue

            for _stream, messages in entries:
                for msg_id, fields in messages:
                    # 비활성 노드 스킵
                    try:
                        node_id = int(fields.get(b"node", 0))
                    except Exception:
                        node_id = 0
                    if active_nodes and node_id not in active_nodes and node_id != 0:
                        last_id = msg_id
                        continue

                    # AI 비활성화 시 기본 결과 저장
                    if not ai_enabled:
                        result = {"risk_level": "normal", "risk_score": 0.0,
                                  "emergency": False, "ai_enabled": False}
                        r.xadd("ai:result", {"data": json.dumps(result, ensure_ascii=False)}, maxlen=3600, approximate=True)
                        last_id = msg_id
                        continue

                    raw = fields.get(b"data", b"")
                    if raw:
                        input_data = np.frombuffer(raw, dtype=np.float32)
                    else:
                        input_data = np.sin(np.linspace(0.0, 8.0 * np.pi, 512)).astype(np.float32)

                    result = ai_engine.process_data(input_data)
                    result = _apply_threshold(result, threshold)

                    r.xadd("ai:result", {"data": json.dumps(result, ensure_ascii=False)}, maxlen=3600, approximate=True)
                    print(result, flush=True)
                    last_id = msg_id

        except _redis.exceptions.ConnectionError as exc:
            print(f"[ai] Redis lost: {exc} — reconnecting...", flush=True)
            r = _connect_redis(redis_host, redis_port)
        except Exception as exc:
            print(f"[ai] error: {exc}", flush=True)
            time.sleep(1)