# SafeWave-AI

독거인 안전 모니터링 시스템 — Raspberry Pi 5 + Docker Compose 기반

**현재 릴리즈: ver.0.0.3**

---

## 목차

- [시스템 개요](#시스템-개요)
- [서비스 구성](#서비스-구성)
- [모델 라인업](#모델-라인업)
- [문서](#문서)
- [Redis 키 맵](#redis-키-맵)
- [MQTT 토픽 구조](#mqtt-토픽-구조)
- [API 엔드포인트](#api-엔드포인트)
- [대시보드 (monitor.html)](#대시보드-monitorhtml)
- [시작하기](#시작하기)
- [유용한 명령어](#유용한-명령어)
- [프로젝트 구조](#프로젝트-구조)
- [문제 해결](#문제-해결)
- [변경 이력](#변경-이력)

---

## 시스템 개요

WiFi CSI와 마이크 음향을 동시에 수집해 AI로 낙상·생체신호·환경음·한국어 음성을 실시간 분석하고, MQTT와 FCM 푸시 알림으로 결과를 전달합니다.

```
ESP32-S3 (CSI) ──UDP:5005──▶ sensing ──▶ Redis csi:raw ──▶ ai
마이크 (오디오) ─────────────▶ audio-sensing ──▶ Redis audio:events ──▶ ai
                                                              │
                                        ai:result ◀──────────┤
                                        ai:emergency ◀────────┤
                                              │               │
                                        api (FastAPI) ◀───────┘
                                        MQTT (Mosquitto) ◀────┘
                                        Home Assistant ◀──MQTT──┘
```

---

## 서비스 구성

| 컨테이너 | 이미지 / 빌드 | 포트 | 역할 |
|---|---|---|---|
| `rp5-db` | `./db` (Redis) | 6379 | 스트림 허브 |
| `rp5-sensing` | `./sensing` | 5005/UDP | CSI UDP 수신 + 전처리 |
| `rp5-audio-sensing` | `./sensing` (audio_main.py) | — | 마이크 VAD 수집 (프로파일: `audio`) |
| `rp5-mqtt` | eclipse-mosquitto:2 | 1883 | MQTT 브로커 |
| `rp5-ai` | `./ai` (cpu-runtime / gpu-runtime) | — | M1~M5 추론 |
| `rp5-api` | `./api` | 8000 | FastAPI REST + WebSocket |
| `rp5-tts-worker` | `./api` (tts_worker.py) | — | TTS 음성 알림 생성 (프로파일: `audio`) |
| `rp5-ha` | home-assistant:stable | 8123 | Home Assistant 대시보드 |

### AI 서비스 빌드 타깃

`ai/Dockerfile`은 멀티 스테이지로 구성됩니다:

| 타깃 | 베이스 이미지 | 사용 환경 |
|---|---|---|
| `cpu-runtime` (기본) | python:3.12-slim-bookworm | Raspberry Pi 5, CPU 전용 |
| `gpu-runtime` | nvidia/cuda:11.8.0-cudnn8-devel-ubuntu22.04 | 개발 머신 (RTX GPU) |

---

## 모델 라인업

| ID | 파일 | 입력 | 출력 |
|---|---|---|---|
| M1 | `experts/m1_wifi_pose.py` | CSI (1×1×192×100) | 낙상 위험 점수 (0–1) |
| M2 | `experts/m2_frenel_vital.py` | CSI | 생체신호 점수 (HR, RR) |
| M3 | `experts/m3_ast_base.py` | 오디오 스펙트로그램 | 환경음 7종 분류 |
| M4 | `experts/m4_whisper_small.py` | 오디오 PCM (최근 5s) | 한국어 STT |
| M5 | `logic/qwen_05b.py` | 컨텍스트 JSON | 통합 위험도 판단 |

### M3 환경음 라벨 (7종)

| 라벨 | 의미 | 실내 예시 |
|---|---|---|
| `silence` | 무음 | 조용함, 충격 후 침묵 |
| `speech` | 사람 음성 | 대화, 비명, 신음, "도와줘" |
| `music` | 음악 | TV 음악, 라디오 |
| `impact` | 충격음 | 낙상, 물건 낙하 |
| `noise` | 잡음 | 가전, 환경 배경음 |
| `alarm` | 경보음 | 화재경보, 비프, 사이렌 |
| `unknown` | 알 수 없음 | 위 6종에 해당하지 않는 소리 |

출력 키: `env_sound_label`, `env_sound_confidence`, `env_sound_source` (`onnx` / `heuristic` / `no-audio`).  
AudioSet 상위 결과는 `ast_top_class`, `ast_top_confidence`로 함께 반환됩니다.

**타이밍 필드 (백엔드 연동):**

| 필드 | 위치 | 의미 |
|------|------|------|
| `ts_ms` | 스냅샷 최상위 | **CSI 트리거 시각** (Unix ms) |
| `audio_ts_ms` | `experts.env_sound` | **M3 분석 오디오 구간 끝 시각** |
| `audio_ts_start_ms` | `experts.env_sound` | 분석 구간 시작 추정 (`audio_ts_ms - duration`) |
| `audio_duration_ms` | `experts.env_sound` | 병합 waveform 길이 (ms) |
| `audio_window_ms` | `experts.env_sound` | M3 윈도우 설정 (`M3_AUDIO_WINDOW_MS`, 기본 3000) |

> "몇 시에 소리 났는지"는 `experts.env_sound.audio_ts_ms` 또는 `audio_ts_start_ms`를 사용하세요.  
> 최상위 `ts_ms`는 CSI 기준으로 수백 ms~수 초 차이날 수 있습니다.

**오디오 파이프라인 요약:**

```
마이크/VAD 또는 POST /audio/events
  → Redis audio:events
  → CSI 트리거 시 최근 이벤트 병합 (M3_AUDIO_WINDOW_MS)
  → ONNX AST 추론 → 7종 라벨
  → ai:result / ai:m3:latest / monitor.html
```

---

## 문서

| 문서 | 대상 | 내용 |
|------|------|------|
| [`docs/api-db-spec.html`](docs/api-db-spec.html) | 공유용 HTML | REST/WebSocket/MQTT/Redis 인터랙티브 명세 |
| [`docs/benchmark_template.md`](docs/benchmark_template.md) | RPi5 실측 | M1~M5 레이턴시, 리소스 스냅샷 템플릿 |

---

## Redis 키 맵

| 키 | 타입 | TTL / 크기 | 설명 |
|---|---|---|---|
| `csi:raw` | Stream | MAXLEN 36,000 | CSI 원시 스트림 (10 Hz) |
| `audio:events` | Stream | MAXLEN 3,600 | VAD 트리거 오디오 이벤트 |
| `ai:result` | Stream | MAXLEN 36,000 | 추론 통합 스냅샷 |
| `ai:emergency` | Stream | MAXLEN 3,600 | warning/critical 이벤트 |
| `ai:m1:latest` ~ `ai:m4:latest` | String | TTL 3600s | 전문가별 최신 결과 |
| `agg:minute:*` | Hash | TTL 3600s | 분 단위 집계 차트 |
| `node:N:last_seen` | String | TTL 30s | 노드 마지막 수신 시각 |
| `node:N:health` | Hash | TTL 3600s | rx/lost/loss_rate 패킷 통계 |
| `sys:settings` | Hash | TTL 3600s | 앱 설정값 |
| `fcm:token:*` | String | TTL 3600s | FCM 등록 기기 토큰 |
| `mqtt:feedback:last` | String | TTL 3600s | MQTT 피드백 마지막 값 |

모든 데이터는 메모리에만 유지되며, 컨테이너 재시작 시 이력이 복구되지 않습니다.

---

## MQTT 토픽 구조

베이스 토픽: `safewave` (`.env`의 `MQTT_BASE_TOPIC` 변경 가능)

| 토픽 | 방향 | 내용 |
|---|---|---|
| `safewave/ai/result` | ai → 구독자 | 추론 결과 요약 |
| `safewave/ai/emergency` | ai → 구독자 | warning/critical 이벤트 |
| `safewave/feedback` | 구독자 → ai | 피드백 (Redis에 저장) |

Home Assistant MQTT 통합 설정은 `docs/api-db-spec.html` 참조.

---

## API 엔드포인트

**모니터링:**

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/status` | 최신 추론 스냅샷 |
| GET | `/logs?n=60` | 최근 N개 로그 |
| GET | `/history?n=100&level=warning` | 이벤트 이력 |
| GET | `/charts/minute?minutes=10` | 분 단위 집계 차트 |
| GET | `/nodes/health` | 노드별 패킷 통계 |
| GET | `/system/redis-memory` | Redis 메모리 사용량 |
| GET | `/system/health` | 시스템 전체 상태 |
| WS  | `/ws/monitor` | 실시간 스트리밍 |

**제어 및 관리:**

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET/POST | `/settings` | 위험도 임계값, 활성 노드 등 |
| POST | `/auth/register-token` | FCM 토큰 등록 |
| GET | `/auth/tokens` | 등록 토큰 목록 |
| POST | `/notify/test` | FCM 테스트 전송 |
| POST | `/notify/send` | FCM 수동 전송 |
| POST | `/notify/check` | FCM 결과 확인 |
| POST | `/audio/events` | 오디오 이벤트 수동 주입 (마이크 테스트) |

전체 스키마: `http://localhost:8000/docs` 또는 `docs/api-db-spec.html` 참조.

---

## 대시보드 (monitor.html)

- **실시간 위험도 카드**: 낙상(M1), 생체신호(M2), 환경음(M3), 한국어 음성(M4), M5 통합 판단
- **SLM 판단 이유 배너**: Qwen 모델의 위험 판단 근거 텍스트 표시
- **10분 추이 차트**: 위험도 / 심박 / 호흡 시계열
- **1시간 위험도 차트**: SLM 호출 마커 포함
- **마이크 패널**: 브라우저 마이크 녹음 → `POST /audio/events` 즉시 전송 (M3/M4 즉시 테스트)

```powershell
# 로컬 정적 서버로 열기 (마이크 권한 안정)
python -m http.server 8081
# http://127.0.0.1:8081/monitor.html
```

외부 기기 접근 시 URL 파라미터:

```
http://192.168.0.25/monitor.html?api=http://192.168.0.25:8000
```

---

## 필수 요구사항

- Docker Desktop 4.x+ (Compose v2 포함)
- Git
- (선택) NVIDIA GPU + CUDA 드라이버 — GPU 가속 모드 사용 시

---

## 시작하기

### 1. 클론

```bash
git clone <your-repo-url>
cd rp5
```

### 2. `.env` 설정

프로젝트 루트에 `.env` 파일을 만듭니다.

```env
REDIS_HOST=db
REDIS_PORT=6379
MODEL_PATH=/app/models
MQTT_BASE_TOPIC=safewave
EXPERT_INFER_TIMEOUT_MS=1000

# audio-sensing VAD
VAD_THRESHOLD_DB=-45
AUDIO_SAMPLE_RATE=16000
AUDIO_CHANNELS=1

# (선택) FCM 푸시
# FIREBASE_KEY_PATH=/app/auth/firebase_key.json
```

| 변수 | 설명 |
|---|---|
| `EXPERT_INFER_TIMEOUT_MS` | 전문가 모델 추론 타임아웃 (기본 1000ms, CPU 느린 환경은 5000~10000) |
| `VAD_THRESHOLD_DB` | VAD 임계값(dBFS). `-55` ~ `-60`이면 원거리 소리에 민감 |

### 3. 볼륨 및 모델 준비

```powershell
mkdir volumes\models -ErrorAction SilentlyContinue
mkdir api\auth -ErrorAction SilentlyContinue
# Firebase 서비스 계정 키 (없으면 알림 기능만 비활성)
# api/auth/firebase_key.json
```

**ONNX 모델 export (선택):**

```powershell
pip install torch transformers onnx onnxruntime onnxscript
python scripts/export_m1_wifi_pose_onnx.py
python scripts/export_m2_frenel_vital_onnx.py
python scripts/export_m3_ast_onnx.py
python scripts/export_m4_whisper_onnx.py
python scripts/export_m5_qwen_onnx.py
```

`volumes/models/`는 Git에 포함되지 않습니다. 다른 PC에서는 위 스크립트를 다시 실행하세요.

### 4. 실행

**CPU 모드 (기본 / Raspberry Pi 5):**

```bash
docker compose up -d --build
```

**GPU 모드 (NVIDIA 개발 머신):**

```bash
AI_DOCKER_TARGET=gpu-runtime docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

**오디오 센싱 + TTS 포함:**

```bash
docker compose --profile audio up -d
```

### 5. 상태 확인

```bash
docker compose ps
curl http://127.0.0.1:8000/
# {"service":"rp5-api","status":"ok"}
docker logs rp5-ai --tail 30
```

### 6. 더미 데이터 주입 (ESP32 없이 테스트)

```powershell
# Windows PowerShell
$env:MSYS_NO_PATHCONV=1
docker run --rm --network rp5_rp5-network `
  -v "${PWD}/scripts:/scripts" `
  python:3.12-slim `
  python /scripts/dummy_inject.py
```

---

## 유용한 명령어

```bash
# 로그 확인
docker compose logs -f ai
docker compose logs -f api

# Redis 스트림 길이
docker compose exec db redis-cli XLEN ai:result
docker compose exec db redis-cli XLEN csi:raw

# Redis 메모리
docker compose exec db redis-cli INFO memory

# API 빠른 확인
curl http://localhost:8000/status
curl http://localhost:8000/nodes/health
```

---

## 프로젝트 구조

```
rp5/
├── docker-compose.yml          # 기본 구성 (CPU)
├── docker-compose.gpu.yml      # GPU 오버라이드
├── mosquitto.conf              # MQTT 브로커 설정
├── monitor.html                # 웹 대시보드 (마이크 M3/M4 테스트)
├── sensing/
│   ├── main.py                 # CSI UDP 수신 + 패킷 손실 추적
│   ├── audio_main.py           # 마이크 VAD 수집 (audio 프로파일)
│   ├── simulator.py            # CSI/오디오 로컬 시뮬레이터
│   └── Dockerfile
├── ai/
│   ├── main.py                 # 추론 메인 루프 (MQTT 발행 포함)
│   ├── mqtt_helper.py          # MQTT 연결/발행 헬퍼
│   ├── experts/                # M1-M4 전문가 모듈
│   ├── logic/
│   │   ├── qwen_05b.py         # M5 Qwen SLM
│   │   └── emergency_score.py  # 위험도 도메인 가중치 계산
│   ├── utils/                  # get_ort_providers, TurboQuant
│   └── Dockerfile              # cpu-runtime / gpu-runtime 멀티 스테이지
├── api/
│   ├── main.py                 # FastAPI 앱
│   ├── notifier.py             # FCM 푸시
│   └── Dockerfile
├── db/
│   └── redis.conf
├── scripts/
│   ├── dummy_inject.py         # ESP32 없이 더미 CSI+오디오 주입
│   ├── slm_chat_cli.py         # SLM 대화형 CLI 테스트
│   ├── slm_chat_demo.py        # SLM 시나리오 데모
│   ├── tts_worker.py           # TTS 음성 알림 워커
│   ├── export_m1_wifi_pose_onnx.py
│   ├── export_m2_frenel_vital_onnx.py
│   ├── export_m3_ast_onnx.py
│   ├── export_m4_whisper_onnx.py
│   └── export_m5_qwen_onnx.py
├── docs/
│   ├── api-db-spec.html        # REST/WebSocket/MQTT/Redis 인터랙티브 명세
│   └── benchmark_template.md   # RPi5 실측 벤치마크 템플릿
└── volumes/
    └── models/                 # ONNX 모델 파일 (Git 제외)
```

---

## 문제 해결

| 증상 | 원인 | 해결 |
|---|---|---|
| `/status` 204 반환 | AI 스트림 비어 있음 | sensing 서비스 및 로그 확인 |
| 재시작 후 데이터 없음 | 정상 (메모리 전용) | 의도적 설계, 이력 복구 없음 |
| WebSocket 연결 실패 | 패키지 누락 | `docker compose up --build api` |
| GPU 첫 추론 ~6s 지연 | CUDA JIT 컴파일 | 정상 (이후 <2ms, 워밍업 자동 실행) |
| `localhost` 간헐적 실패 | Windows IPv6 우선 | `127.0.0.1` 사용 |
| MQTT 연결 안 됨 | mosquitto 미실행 | `docker compose ps` 확인 후 재시작 |
| ai 컨테이너 exit 139 | ONNX TensorRT EP segfault | `ORT_USE_GPU=0` 또는 cpu-runtime 빌드 확인 |
| M3/M4 `expert_timeout` | CPU 추론 > 1s | `.env`에 `EXPERT_INFER_TIMEOUT_MS=5000` |
| 마이크 권한 오류 | HTTPS/file:// 접근 | `http://127.0.0.1:8081/monitor.html` 사용 |
| dummy_inject.py 경로 오류 | Git Bash 경로 변환 | `MSYS_NO_PATHCONV=1` 접두어 사용 |

---

## 변경 이력

### ver.0.0.3

**AI 엔진:**
- `ai/logic/emergency_score.py` 신규 — 도메인 가중치 기반 위험도 계산 분리 (fall 40% / vital 30% / sound 15% / speech 15%)
- `ai/utils/__init__.py` segfault 수정 — `ort.get_available_providers()` 대신 `ORT_USE_GPU` 환경변수로 CUDA EP 제어 (TensorRT JIT 초기화 제거)
- `ai/logic/qwen_05b.py` segfault 수정 — 동일 패턴 (`_load_model`) 제거, hourly context 캐시 TTL 3s → 10s
- M3 출력 필드 추가: `ast_top_class`, `ast_top_confidence`, `label`, `confidence` (하위 호환 유지)
- 심박 warning 임계값 수정: `_HR_WARN_LO` 50 → 55 bpm (서맥 기준 조정)

**센싱:**
- `sensing/main.py` — `node:N:health` hset + expire pipeline 원자화 (race condition 제거)

**API:**
- `sys:settings` TTL 보장 — POST `/settings` 저장 시 expire 누락 보완

**스크립트 (신규):**
- `scripts/dummy_inject.py` — ESP32 없이 더미 CSI + 오디오 이벤트 주입 (10Hz CSI / 0.5Hz audio)
- `scripts/slm_chat_cli.py` / `slm_chat_demo.py` — M5 SLM 대화형 테스트 도구
- `scripts/tts_worker.py` — MQTT 구독 기반 TTS 음성 알림 워커

### ver.0.0.2

**신규 서비스:**
- `mqtt` — eclipse-mosquitto:2 MQTT 브로커 추가 (포트 1883)
- `audio-sensing` — VAD 기반 마이크 오디오 수집 서비스 (프로파일: audio)
- `homeassistant` — Home Assistant 연동 (포트 8123, MQTT 구독)

**AI 엔진:**
- MQTT 발행 통합 (`safewave/ai/result`, `safewave/ai/emergency`)
- `ThreadPoolExecutor` 매 루프 생성·소멸 → `self._executor` 재사용 (스레드 누수 제거)
- CUDA JIT 첫 추론 지연 해소: `__init__` 시 전문가 모델 GPU 워밍업
- 전문가별 최신 결과 Redis 저장 (`ai:m1:latest` ~ `ai:m4:latest`)
- AI 도커 멀티 스테이지 빌드: `cpu-runtime` / `gpu-runtime` 분리

**센싱:**
- UDP 패킷 시퀀스 번호 파싱 추가
- 노드별 패킷 수신/손실/loss_rate 통계 (`node:N:health` Hash)
- 오디오 센싱 서비스 신규 (`sensing/audio_main.py`)

**API:**
- `POST /audio/events` — 오디오 이벤트 수동 주입
- `GET /system/redis-memory`, `GET /system/health` 신규
- FCM alert_worker 비정상 종료 시 자동 재시작

**버그 수정:**
- `MINUTE_AGG_TTL_SECONDS` 기본값 3900 → 3600 (TTL 규칙 위반)
- `audio:events` 필드 타입 `str` → `int` (sensing/AI 형식 통일)
- Qwen `critical_count` 조건 순서 역전 (unreachable elif 제거)
- Qwen 전체 `print()` → `logging.getLogger` 구조화 로그

**신규 파일:**
- `docker-compose.gpu.yml` — GPU 리소스 예약 오버라이드
- `mosquitto.conf` — MQTT 브로커 설정
- `ai/mqtt_helper.py` — MQTT 연결/발행 헬퍼
- `docs/api-db-spec.html` — 전체 API/DB 명세 (공유용 HTML)
- `.github/workflows/docker-build.yml` — CI 빌드 검증

### ver.0.0.1

- M1~M5 실제 모델 라인업 반영 (DT-Pose, ViFi, AST-Base, Whisper-Small, Qwen2-0.5B)
- Docker Compose 기반 초기 스택 구성 (db / sensing / ai / api)
- GPU/CPU 겸용 ONNX 런타임 구성
- `/status` no-data 처리 안정화
