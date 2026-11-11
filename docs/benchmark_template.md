# Raspberry Pi 5 benchmark template

> 이 문서는 **빈 측정 양식**이며 결과 보고서가 아니다. 빈 칸은 미측정을 뜻한다.
> 개발 PC·GPU·프록시 환경의 수치를 이 표에 옮기지 말고, 실제 Raspberry Pi 5에서
> 같은 커밋과 모델 아티팩트로 측정한 값만 기록한다.

## 실측 환경

- Device: Raspberry Pi 5 (RAM: )
- Storage / cooling / power supply:
- OS / kernel / architecture:
- Docker / Compose version:
- Git commit:
- Model file name and checksum:
- Environment variables (`AI_DOCKER_TARGET`, thread counts, timeouts):
- Ambient temperature:
- Warm-up / test duration:
- Input source: simulator / recorded replay (capture ID, original host and interval) / live ESP32 + microphone
- Replay injection path and clock alignment (if applicable):
- Audio arrival interval and processed / expired event counts:
- Measured by / date:

## Service resource snapshot

| Metric | Idle | Load | Samples / duration | Evidence |
|---|---:|---:|---:|---|
| CPU temperature (°C) |  |  |  | `vcgencmd measure_temp` log |
| CPU utilization (%) |  |  |  | per-container and system log |
| RAM used / available, swap used (MB) |  |  |  | `free -m`, process RSS (RPi5 memory cgroup may be disabled and `docker stats` may show 0B) |
| Redis `used_memory` (MB) |  |  |  | `INFO memory` |
| API p50 / p95 latency (ms) |  |  |  | `/status`, `/logs` raw samples |
| `audio:events` rate (entries/s) |  |  |  | stream delta |
| restart / OOM count |  |  |  | Docker event log |

## Model inference latency — Raspberry Pi 5 only

| Model | Backend / artifact | p50 (ms) | p95 (ms) | Samples | Input | Evidence file |
|---|---|---:|---:|---:|---|---|
| M1 hub inference (only if `M1_SOURCE=onnx`) | ONNX |  |  |  |  |  |
| M3 (environment sound) | v34_homepos ONNX / waveform fallback |  |  |  |  |  |
| M4 (Korean STT) | Whisper ONNX / fallback |  |  |  |  |  |
| M5 (risk decision) | Qwen 1.5B GGUF Q5 |  |  |  |  |  |

## End-to-end and stability

- M1 board score → hub vote → API latency, coverage and vote settings (`M1_SOURCE=board` only):
- ESP32 CSI → Redis → M1 hub ONNX → API latency (`M1_SOURCE=onnx` only):
- microphone or replay `audio:events` → M3/M4 → `audio:result` latency (stream-ID time minus payload `ts_ms`, p50/p95):
- urgent phrases detected / injected, and audio events processed / expired:
- warning/critical → M5 → `ai:emergency` latency:
- Phase 2 TTS/STT/FCM completion:
- thermal throttling:
- M3/M4 timeout breaches:
- 30-minute / 1-hour stability result:
- failures and reruns:

## Interpretation guardrail

- Latency/resource measurements do not establish fall-detection accuracy, vital-sign error,
  STT accuracy, clinical safety, or real-home reliability.
- Simulator results establish interface and pipeline behavior only.
- M2 is retired and off by default; omit it from current performance claims.
- Recorded replay on RPi5 establishes device pipeline timing for that input, not a fresh live sensing run or human-labelled model accuracy. Separate single-event and sustained-arrival measurements.
- Keep raw logs beside the completed report; do not fill the table from memory.
