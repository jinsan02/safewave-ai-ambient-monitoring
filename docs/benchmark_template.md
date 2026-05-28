# Raspberry Pi 5 Benchmark Template

실측 환경
- Device: Raspberry Pi 5 (8GB)
- OS: 
- Docker version: 
- Ambient temperature: 
- Test duration: 

## Service Resource Snapshot

| Metric | Idle | Load | Note |
|---|---:|---:|---|
| CPU temp (C) |  |  | vcgencmd measure_temp |
| RAM used (MB) |  |  | free -m |
| Redis used_memory (MB) |  |  | INFO memory |
| API p95 latency (ms) |  |  | /status, /logs |

## Model Inference Latency

| Model | p50 (ms) | p95 (ms) | Samples |
|---|---:|---:|---:|
| M1 (fall) |  |  |  |
| M2 (vital) |  |  |  |
| M3 (env_sound) |  |  |  |
| M4 (speech_ko) |  |  |  |

## Stability Notes

- OOM/Restart events:
- Redis near-limit warnings:
- Thermal throttling observed:
