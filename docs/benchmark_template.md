# Raspberry Pi 5 Benchmark Template

실측 환경
- Device: Raspberry Pi 5 (8GB)
- OS: 
- Docker version: 
- Ambient temperature: 
- Test duration: 
- Branch/Version: 

## Service Resource Snapshot

| Metric | Idle | Load | Note |
|---|---:|---:|---|
| CPU temp (C) |  |  | vcgencmd measure_temp |
| RAM used (MB) |  |  | free -m |
| Redis used_memory (MB) |  |  | INFO memory |
| API p95 latency (ms) |  |  | /status, /logs |
| audio:events xps (entries/s) |  |  | XLEN 10s delta @ 0.5Hz VAD |

## Model Inference Latency

> x86 개발 PC 참고 실측 (CPU): M1 ~10ms, M2 ~5ms, M3 ~850ms, M4 ~1600ms.  
> RPi5 ARM NEON에서는 다를 수 있으므로 아래 표에 실측값을 기입하세요.

| Model | p50 (ms) | p95 (ms) | Samples | Note |
|---|---:|---:|---:|---|
| M1 (fall) |  |  |  | ONNX |
| M2 (vital) |  |  |  | ONNX |
| M3 (env_sound) |  |  |  | ONNX AST |
| M4 (speech_ko) |  |  |  | Whisper ONNX |
| M5 (SLM) |  |  |  | Qwen ONNX / rule-based fallback |

## Stability Notes

- OOM/Restart events:
- Redis near-limit warnings:
- Thermal throttling observed:
- M3/M4 timeout breaches (EXPERT_INFER_TIMEOUT_MS=1000):
