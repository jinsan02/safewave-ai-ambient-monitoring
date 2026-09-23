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
from redis_client import connect_redis

try:
    import sounddevice as sd
except Exception as exc:  # pragma: no cover
    raise RuntimeError("sounddevice import failed. Check PortAudio runtime.") from exc


REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
AUDIO_STREAM = os.getenv("AUDIO_STREAM", "audio:events")
# 최대 6초 float32 이벤트 기준 raw waveform 약 44MB 이내로 제한한다.
AUDIO_STREAM_MAXLEN = int(os.getenv("AUDIO_STREAM_MAXLEN", "120"))

AUDIO_NODE_ID = int(os.getenv("AUDIO_NODE_ID", "1"))
AUDIO_SAMPLE_RATE = int(os.getenv("AUDIO_SAMPLE_RATE", "16000"))
AUDIO_CHANNELS = int(os.getenv("AUDIO_CHANNELS", "1"))
AUDIO_BLOCK_SIZE = int(os.getenv("AUDIO_BLOCK_SIZE", "1024"))

# VAD threshold: -45 dB 기본값 (값이 높을수록 민감도 낮아짐)
VAD_THRESHOLD_DB = float(os.getenv("VAD_THRESHOLD_DB", "-45.0"))
VAD_MIN_ACTIVE_MS = int(os.getenv("VAD_MIN_ACTIVE_MS", "300"))
VAD_HANGOVER_MS = int(os.getenv("VAD_HANGOVER_MS", "250"))
AUDIO_MAX_EVENT_SECONDS = float(os.getenv("AUDIO_MAX_EVENT_SECONDS", "6.0"))


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


def xadd_audio_event(r: redis.Redis, waveform: np.ndarray, peak_db: float, raw_peak: float = 0.0):
    ts_ms = int(time.time() * 1000)
    meta = {
        "sample_rate": AUDIO_SAMPLE_RATE,
        "channels": AUDIO_CHANNELS,
        "duration_ms": int(len(waveform) * 1000 / AUDIO_SAMPLE_RATE),
        "peak_db": round(float(peak_db), 2),
        # 게인 보정 '전' 원본 최대 진폭. M3 무음 게이트는 이 값으로 판정해야 한다
        # (보정 후 파형으로 재면 조용한 소리도 0.85로 올라가 게이트가 무력화된다).
        "raw_peak": round(float(raw_peak), 6),
    }

    r.xadd(
        AUDIO_STREAM,
        {
            "node": AUDIO_NODE_ID,
            "ts_ms": ts_ms,
            "data": json.dumps(meta, ensure_ascii=False),
            "waveform": waveform.astype(np.float32).tobytes(),
        },
        maxlen=AUDIO_STREAM_MAXLEN,
        approximate=True,
    )


def run_audio_loop(r: redis.Redis):
    audio_q: queue.Queue[np.ndarray] = queue.Queue(maxsize=128)
    last_redis_write_error_log = 0.0

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
                        raw_peak = float(np.max(np.abs(waveform)))  # 보정 전에 잰다
                        waveform = normalize_quiet_waveform(waveform)
                        xadd_audio_event(r, waveform, event_peak_db, raw_peak)
                        print(
                            f"[audio] event xadd samples={waveform.size} "
                            f"dur_ms={int(waveform.size * 1000 / AUDIO_SAMPLE_RATE)} peak_db={event_peak_db:.1f}",
                            flush=True,
                        )
                    except redis.exceptions.ConnectionError as exc:
                        print(f"[audio] Redis error: {exc}, reconnecting...", flush=True)
                        r = connect_redis(REDIS_HOST, REDIS_PORT, "audio")
                    except redis.exceptions.ResponseError as exc:
                        # noeviction 상한 도달 시 PortAudio 스트림을 재시작하지 않고 해당 이벤트만 폐기한다.
                        now = time.time()
                        if now - last_redis_write_error_log >= 5:
                            print(f"[audio] Redis write rejected: {exc}", flush=True)
                            last_redis_write_error_log = now

                active = False
                active_samples = 0
                silence_samples = 0
                event_buffers = []
                event_peak_db = -120.0


def main():
    r = connect_redis(REDIS_HOST, REDIS_PORT, "audio")
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
