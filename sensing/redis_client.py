"""sensing 프로세스들이 공유하는 Redis 연결 재시도."""

import time

import redis


def connect_redis(host: str, port: int, service: str) -> redis.Redis:
    while True:
        try:
            client = redis.Redis(host=host, port=port, socket_connect_timeout=3)
            client.ping()
            print(f"[{service}] Redis connected: {host}:{port}", flush=True)
            return client
        except redis.exceptions.ConnectionError as exc:
            print(f"[{service}] Redis not ready ({exc}), retry in 2s...", flush=True)
            time.sleep(2)
