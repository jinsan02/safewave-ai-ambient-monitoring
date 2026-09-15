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
- Input source: simulator / recorded data / live ESP32 + microphone
- Measured by / date:

## Service resource snapshot

| Metric | Idle | Load | Samples / duration | Evidence |
|---|---:|---:|---:|---|
| CPU temperature (°C) |  |  |  | `vcgencmd measure_temp` log |
| CPU utilization (%) |  |  |  | per-container and system log |
| RAM used (MB) |  |  |  | `free -m`, container stats |
| Redis `used_memory` (MB) |  |  |  | `INFO memory` |
| API p50 / p95 latency (ms) |  |  |  | `/status`, `/logs` raw samples |
| `audio:events` rate (entries/s) |  |  |  | stream delta |
| restart / OOM count |  |  |  | Docker event log |

## Model inference latency — Raspberry Pi 5 only

| Model | Backend / artifact | p50 (ms) | p95 (ms) | Samples | Input | Evidence file |
|---|---|---:|---:|---:|---|---|
| M1 (fall interface) | ONNX / heuristic |  |  |  |  |  |
| M2 (vital interface) | ONNX / FFT |  |  |  |  |  |
| M3 (environment sound) | AST ONNX + heuristic label |  |  |  |  |  |
| M4 (Korean STT) | Whisper ONNX / fallback |  |  |  |  |  |
| M5 (risk decision) | Qwen 1.5B GGUF Q5 |  |  |  |  |  |

## End-to-end and stability

- ESP32 CSI → Redis → M1/M2 → API latency:
- microphone → M3/M4 → API latency:
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
- Keep raw logs beside the completed report; do not fill the table from memory.
