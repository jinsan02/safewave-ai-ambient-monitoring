"""
sensing/audio_main.py
마이크 오디오를 캡처하여 VAD를 통과한 구간만 Redis Stream(audio:events)에 적재.
"""

import json
import os
import queue
import time

import numpy as np
import redis

try:
    import sounddevice as sd
except Exception as exc:  # pragma: no cover
    raise RuntimeError("sounddevice import failed. Check PortAudio runtime.") from exc


REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
AUDIO_STREAM = os.getenv("AUDIO_STREAM", "audio:events")
AUDIO_STREAM_MAXLEN = int(os.getenv("AUDIO_STREAM_MAXLEN", "3600"))

AUDIO_NODE_ID = int(os.getenv("AUDIO_NODE_ID", "1"))
AUDIO_SAMPLE_RATE = int(os.getenv("AUDIO_SAMPLE_RATE", "16000"))
AUDIO_CHANNELS = int(os.getenv("AUDIO_CHANNELS", "1"))
AUDIO_BLOCK_SIZE = int(os.getenv("AUDIO_BLOCK_SIZE", "1024"))

# VAD threshold: -45 dB 기본값 (값이 높을수록 민감도 낮아짐)
VAD_THRESHOLD_DB = float(os.getenv("VAD_THRESHOLD_DB", "-45.0"))
VAD_MIN_ACTIVE_MS = int(os.getenv("VAD_MIN_ACTIVE_MS", "300"))
VAD_HANGOVER_MS = int(os.getenv("VAD_HANGOVER_MS", "250"))
AUDIO_MAX_EVENT_SECONDS = float(os.getenv("AUDIO_MAX_EVENT_SECONDS", "6.0"))


def connect_redis() -> redis.Redis:
    while True:
        try:
            client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, socket_connect_timeout=3)
            client.ping()
            print(f"[audio] Redis connected: {REDIS_HOST}:{REDIS_PORT}", flush=True)
            return client
        except redis.exceptions.ConnectionError as exc:
            print(f"[audio] Redis not ready ({exc}), retry in 2s...", flush=True)
            time.sleep(2)


def normalize_quiet_waveform(waveform: np.ndarray) -> np.ndarray:
    """VAD 통과 후에도 작은 peak면 전송 전에 gain을 보정한다."""
    if os.getenv("AUDIO_GAIN_NORMALIZE", "1") == "0":
        return waveform

    x = np.asarray(waveform, dtype=np.float32).reshape(-1)
    if x.size == 0:
        return x

    peak = float(np.max(np.abs(x)))
    if peak <= 1e-9:
        return x

    target_peak = float(os.getenv("AUDIO_TARGET_PEAK", "0.85"))
    normalize_below = float(os.getenv("AUDIO_NORMALIZE_BELOW_PEAK", "0.12"))
    if peak >= normalize_below:
        return x

    scaled = x * (target_peak / peak)
    return np.clip(scaled, -1.0, 1.0).astype(np.float32)


def rms_dbfs(samples: np.ndarray) -> float:
    if samples.size == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(np.square(samples), dtype=np.float64)))
    if rms <= 1e-9:
        return -120.0
    return 20.0 * np.log10(rms)


def xadd_audio_event(r: redis.Redis, waveform: np.ndarray, peak_db: float):
    ts_ms = int(time.time() * 1000)
    payload = {
        "sample_rate": AUDIO_SAMPLE_RATE,
        "channels": AUDIO_CHANNELS,
        "duration_ms": int(len(waveform) * 1000 / AUDIO_SAMPLE_RATE),
        "peak_db": round(float(peak_db), 2),
        "waveform": waveform.astype(np.float32).tolist(),
    }

    r.xadd(
        AUDIO_STREAM,
        {
            "node": AUDIO_NODE_ID,
            "ts_ms": ts_ms,
            "data": json.dumps(payload, ensure_ascii=False),
        },
        maxlen=AUDIO_STREAM_MAXLEN,
        approximate=True,
    )


def run_audio_loop(r: redis.Redis):
    audio_q: queue.Queue[np.ndarray] = queue.Queue(maxsize=128)

    def _on_audio(indata, frames, _time_info, status):
        if status:
            print(f"[audio] stream status: {status}", flush=True)
        chunk = np.asarray(indata, dtype=np.float32)
        if chunk.ndim > 1:
            chunk = chunk[:, 0]
        try:
            audio_q.put_nowait(chunk.copy())
        except queue.Full:
            # 처리 지연 시 오래된 오디오를 버려 지연 누적을 방지
            pass

    min_active_samples = int(AUDIO_SAMPLE_RATE * (VAD_MIN_ACTIVE_MS / 1000.0))
    hangover_samples = int(AUDIO_SAMPLE_RATE * (VAD_HANGOVER_MS / 1000.0))
    max_event_samples = int(AUDIO_SAMPLE_RATE * AUDIO_MAX_EVENT_SECONDS)

    active = False
    active_samples = 0
    silence_samples = 0
    event_buffers: list[np.ndarray] = []
    event_peak_db = -120.0

    with sd.InputStream(
        samplerate=AUDIO_SAMPLE_RATE,
        channels=AUDIO_CHANNELS,
        blocksize=AUDIO_BLOCK_SIZE,
        dtype="float32",
        callback=_on_audio,
    ):
        print(
            f"[audio] mic capture started sr={AUDIO_SAMPLE_RATE}, block={AUDIO_BLOCK_SIZE}, "
            f"vad_db={VAD_THRESHOLD_DB}",
            flush=True,
        )

        while True:
            chunk = audio_q.get()
            level_db = rms_dbfs(chunk)

            if level_db >= VAD_THRESHOLD_DB:
                if not active:
                    active = True
                    active_samples = 0
                    silence_samples = 0
                    event_buffers = []
                    event_peak_db = level_db
                event_buffers.append(chunk)
                active_samples += chunk.size
                silence_samples = 0
                if level_db > event_peak_db:
                    event_peak_db = level_db
            elif active:
                event_buffers.append(chunk)
                active_samples += chunk.size
                silence_samples += chunk.size

            if not active:
                continue

            too_long = active_samples >= max_event_samples
            enough_voice = active_samples >= min_active_samples
            end_of_voice = silence_samples >= hangover_samples

            if too_long or (enough_voice and end_of_voice):
                waveform = np.concatenate(event_buffers) if event_buffers else np.zeros(0, dtype=np.float32)
                if enough_voice and waveform.size > 0:
                    try:
                        waveform = normalize_quiet_waveform(waveform)
                        xadd_audio_event(r, waveform, event_peak_db)
                        print(
                            f"[audio] event xadd samples={waveform.size} "
                            f"dur_ms={int(waveform.size * 1000 / AUDIO_SAMPLE_RATE)} peak_db={event_peak_db:.1f}",
                            flush=True,
                        )
                    except redis.exceptions.ConnectionError as exc:
                        print(f"[audio] Redis error: {exc}, reconnecting...", flush=True)
                        r = connect_redis()

                active = False
                active_samples = 0
                silence_samples = 0
                event_buffers = []
                event_peak_db = -120.0


def main():
    r = connect_redis()
    while True:
        try:
            run_audio_loop(r)
        except KeyboardInterrupt:
            print("[audio] stopped.", flush=True)
            return
        except Exception as exc:
            print(f"[audio] loop error: {exc}", flush=True)
            time.sleep(1)


if __name__ == "__main__":
    main()
