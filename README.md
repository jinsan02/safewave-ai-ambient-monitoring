# SafeWave-AI

독거인 안전 모니터링 시스템 — Raspberry Pi 5 + Docker Compose 기반

**현재 릴리즈: v0.2.0**

> **검증 범위 고지**
> 이 저장소는 센싱·추론·알림 인터페이스를 통합한 시스템 프로토타입입니다. Raspberry Pi 5용
> 빌드와 자원 설정은 구현되어 있지만, 현재 Git에는 완성된 RPi5 벤치마크나 원시 로그가 없습니다.
> 낙상 정확도, 생체신호 오차, STT 정확도, 임상 안전성, 실거주 환경 성능도 검증되지 않았습니다.
> 상세 근거와 미검증 항목은 [`docs/validation_status.md`](docs/validation_status.md), 기여 범위는
> [`CONTRIBUTIONS.md`](CONTRIBUTIONS.md)를 확인하세요.

---

## 목차

- [시스템 개요](#시스템-개요)
- [재현 가능 범위](#재현-가능-범위)
- [서비스 구성](#서비스-구성)
- [모델 라인업](#모델-라인업)
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

WiFi CSI와 마이크 음향을 수집해 낙상 위험·생체신호·환경음·한국어 음성을 분석하도록 설계하고,
MQTT와 FCM 알림 경로로 연결한 프로토타입입니다. 각 출력은 의료 판정이 아니라 시스템 내부의
위험 추정값입니다.

```
ESP32-S3 (CSI) ──UDP:5005──▶ sensing ──▶ Redis csi:raw ──▶ ai-experts (M1~M4)
마이크 (오디오) ─────────────▶ audio-sensing ──▶ Redis audio:events ──▶ ai-experts
                                                              │
                                        ai:result ◀──────────┤
                                        ai:emergency ◀────────┤──▶ ai-qwen (M5/Qwen)
                                              │               │
                                        api (FastAPI) ◀───────┘
                                        MQTT (Mosquitto) ◀────┘
                                        Home Assistant ◀──MQTT──┘
```

## 재현 가능 범위

**Git 클론만으로 확인 가능한 범위**

- Docker Compose, Redis/MQTT, FastAPI/WebSocket, 대시보드 및 M1~M5 호출 인터페이스
- `emergency_score` 경계 로직과 더미 CSI/오디오 주입 경로
- 외부 모델 없이 동작하는 휴리스틱/폴백 경로

**별도 아티팩트·장비·데이터가 필요한 범위**

- `volumes/models/`의 M1~M5 모델 파일: Git에 포함되지 않음
- ESP32-S3와 마이크를 사용한 실제 센싱 및 Raspberry Pi 5 성능 측정
- 학습 체크포인트를 사용한 M1/M2, 라벨 데이터 기반 M3/M4 정확도 평가
- 스피커·FCM 자격증명·네트워크를 포함한 Phase 2 실기기 검증

현재 노트북에는 로컬 모델 파일이 있으나 공개 저장소 재현성과는 별개입니다. 특히 M1/M2 export
스크립트는 학습 체크포인트를 읽지 않고 초기화된 단순화 네트워크를 내보내므로, 생성된 ONNX의
존재를 학습 완료나 정확도 검증으로 해석하면 안 됩니다.

---

## 서비스 구성

| 컨테이너 | 이미지 / 빌드 | 포트 | 역할 |
|---|---|---|---|
| `rp5-db` | `./db` (Redis) | 6379 | 스트림 허브 |
| `rp5-sensing` | `./sensing` | 5005/UDP | CSI UDP 수신 + 전처리 |
| `rp5-audio-sensing` | `./sensing` (audio_main.py) | — | 마이크 VAD 수집 (프로파일: `audio`) |
| `rp5-mqtt` | eclipse-mosquitto:2 | 1883 | MQTT 브로커 |
| `rp5-ai-experts` | `./ai` (gpu-runtime 기본) | — | M1~M4 전문가 모델 추론 |
| `rp5-ai-qwen` | `./ai` (`gguf-runtime` 기본) | — | M5 Qwen SLM 통합 위험도 판단 |
| `rp5-api` | `./api` | 8000 | FastAPI REST + WebSocket |
| `rp5-tts-worker` | `./api` (tts_worker.py) | — | TTS 음성 알림 생성 (프로파일: `audio`) |
| `rp5-ha` | home-assistant:stable | 8123 | Home Assistant 대시보드 |

### AI 서비스 빌드 타깃

`ai/Dockerfile`은 멀티 스테이지로 구성됩니다:

| 타깃 | 베이스 이미지 | 사용 환경 |
|---|---|---|
| `gpu-runtime` (기본) | nvidia/cuda:11.8.0-cudnn8-devel-ubuntu22.04 | 개발 머신 (RTX GPU) |
| `cpu-runtime` | python:3.12-slim-bookworm | Raspberry Pi 5, CPU 전용 |

---

## 모델 라인업

| ID | 파일 | 입력 | 출력 |
|---|---|---|---|
| M1 | `experts/m1_wifi_pose.py` | CSI `(1, M1_MAX_NODES, 64, 100)` — 기본 5노드. 1노드 학습 모델은 `.env`에 `M1_MAX_NODES=1` | 낙상 위험 점수 (0–1). 출력 이름이 `fall_logit`이면 sigmoid 적용 |
| M2 | `experts/m2_frenel_vital.py` | CSI 시간 시리즈 (N,) @ 100Hz — per-node deque | 생체신호 점수 (HR, RR) |
| M3 | `experts/m3_ast_base.py` | raw waveform → 고정 AST tensor | 휴리스틱 7종 라벨 + AST top class/confidence |
| M4 | `experts/m4_whisper_small.py` | 오디오 PCM (최근 5s) | 한국어 STT |
| M5 | `logic/qwen_gguf.py` — Qwen2.5-1.5B GGUF Q5_K_M (llama.cpp, 기본 배포 목표본) | 상태 한 줄 + [1h추세] 시계열 요약 | 통합 위험도 판단 |

### M5 백엔드 (`SLM_BACKEND` env)

| 백엔드 | 모델 | 외부 qwen-llmops 평가 기록 | SafeWave에서 확인된 범위 |
|---|---|---|---|
| `gguf` (기본) | `qwen_15b_gguf_q5` | Track B raw 0.985 / 1000 합성 케이스 | 런타임·가드레일 통합. RPi5 지연은 미측정 |
| `15b` | `qwen_15b` (ONNX fp32) | Track B raw 1.000 | 토크나이저만 로컬에 있고 fp32 ONNX 가중치는 없음 |
| `05b` | `qwen_05b` (ONNX) | 과거 문서 exact 71%; 현재 로컬 ignored report는 45/100 | 레거시 롤백 경로. 결과 계보 재확인 필요 |

위 수치는 별도 `qwen-llmops`의 합성/정책 평가 기록이며 SafeWave의 낙상·생체신호·임상 성능을
뜻하지 않습니다. 이 저장소의 로컬 결과 파일은 Git에서 제외되어 있고, RPi5 원시 벤치마크도
없으므로 “RPi5 배포 검증 완료”로 표현하지 않습니다.

프롬프트·가드레일(`vital_override`, `hallucination_guard`)은 `logic/qwen_15b.py`가 원본이고,
`qwen_gguf.py`는 생성부만 llama.cpp로 교체한 상속 클래스다. `emergency_score.py` 룰 게이트에
시계열 에스컬레이션(지속 경고 누적·점진 악화 → M5 임계 0.6 floor)이 포함되며,
시계열 소스는 `agg:minute:*` 분 집계다 (없으면 스냅샷 전용 — 하위호환).

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
현재 7종 라벨은 파형 휴리스틱이 선택합니다. AST ONNX는 AudioSet의 일반 class index와 confidence를
반환하고, 그 confidence를 7종 라벨 confidence에 혼합합니다. 따라서 AST가 7종을 학습·검증한
모델이라는 뜻은 아닙니다. 또한 현재 런타임은 raw waveform을 AST 입력 크기로 pad/reshape할 뿐
정식 log-Mel feature extractor를 적용하지 않으므로, AST 출력의 의미도 검증 전 상태입니다.

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

## Redis 키 맵

| 키 | 타입 | TTL / 크기 | 설명 |
|---|---|---|---|
| `csi:raw` | Stream | MAXLEN 36,000 | CSI 원시 스트림 (100 Hz, 788B 패킷) |
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
| `tts:speak:queue` | List | — | TTS 발화 요청 큐 (`tts_worker.py` BLPOP 소비) |
| `user:voice_response:N` | String | TTL 5s | TTS 재생 완료 신호 (노드별, Phase 2 응급 확인 트리거) |
| `notify:sent:{msg_id}:{device_id}` | String | TTL 3600s | FCM 중복 발송 방지 dedupe 키 |

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

## 응급 알림 흐름 (Phase 2 Active Verification)

`ai:emergency`에 critical 이벤트가 들어오면 `api/main.py`의 `_alert_worker`가 즉시 FCM을 보내지 않고, 1차로 음성 확인을 시도합니다.

```
ai:emergency (critical)
  → TTS 발화 큐 적재 (tts:speak:queue)
  → tts_worker.py가 "괜찮으세요?" 음성 재생 → user:voice_response:N 신호
  → api가 ai:result 스트림에서 STT(M4) 응답 대기 (PHASE2_TIMEOUT_SEC, 기본 15s)
  → transcript 키워드 분류
      - 응급 키워드("아파","도와","살려","119" 등) → call_emergency
      - 안전 키워드("괜찮","아니야","없어" 등)     → cancel_alarm
      - 무응답/timeout                              → call_emergency (fail-safe)
  → FCM 발송 (cancel_alarm: warning 알림 / call_emergency: critical 알림)
```

각 응급 이벤트는 `asyncio.create_task`로 비동기 처리되어 다음 이벤트의 큐 처리를 막지 않습니다.

---

## API 엔드포인트

**모니터링:**

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/status` | 최신 추론 스냅샷 |
| GET | `/logs?n=60` | 최근 N개 로그 |
| GET | `/history?n=100&level=warning` | 이벤트 이력 |
| GET | `/charts/minute?minutes=10` | 분 단위 집계 차트 |
| GET | `/nodes/health` | 노드별 패킷 통계 (상태, 마지막 수신, 누적 rx·lost, RSSI) |
| GET | `/system/redis-memory` | Redis 메모리 사용량 |
| GET | `/system/health` | 시스템 전체 상태 |
| GET | `/system/resources` | 호스트 CPU(전체·코어별)·온도·메모리·디스크 — 조회 시 계산, 저장 없음 |
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
- **모델 토글**: M1~M5 개별 on/off (`/settings`의 `models`)
- **ESP32 노드 통신 패널**: 노드 1~6 온라인 여부, 초당 수신량, 구간·누적 손실, RSSI, 마지막 수신 시각 (2초 갱신).
  구간 값은 페이지에서 직전 조회와의 차이로 계산하며 서버에 저장하지 않음
- **시스템 자원 패널**: `?api=`로 연결된 기기의 CPU·온도·메모리·저장소 (3초 갱신). 제목에 연결 주소가 표시되므로
  노트북 Docker(`localhost`)와 RPi5를 혼동하지 않도록 확인

```powershell
# 로컬 정적 서버로 열기 (마이크 권한 안정)
python -m http.server 8081
# http://127.0.0.1:8081/monitor.html
```

외부 기기 접근 시 URL 파라미터:

```
http://127.0.0.1:8081/monitor.html?api=http://<RPi5-IP>:8000
```

RPi5 주소는 DHCP로 바뀔 수 있습니다. 현재 값은 `HANDOFF.md`를 확인하세요.

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
| `M2_CSI_WINDOW_FRAMES` | M2 시간축 누적 프레임 수 (기본 300 = 3초 @ 100Hz). 호흡 완전 해상도는 1000프레임(10초) 권장 |

### 3. 볼륨 및 모델 준비

```powershell
mkdir volumes\models -ErrorAction SilentlyContinue
mkdir api\auth -ErrorAction SilentlyContinue
# Firebase 서비스 계정 키 (없으면 알림 기능만 비활성)
# api/auth/firebase_key.json
```

**모델 export (선택, 품질 검증과 별개):**

```powershell
pip install torch transformers onnx onnxruntime onnxscript
python scripts/export_m1_wifi_pose_onnx.py
python scripts/export_m2_frenel_vital_onnx.py
python scripts/export_m3_ast_onnx.py
python scripts/export_m4_whisper_onnx.py
python scripts/export_m5_qwen_onnx.py
```

`volumes/models/`는 Git에 포함되지 않습니다. M1/M2 스크립트는 현재 학습 체크포인트를 받지 않고
무작위 초기화 네트워크의 구조·ONNX 입출력만 검증합니다. M3/M4는 외부 사전학습 모델을 내려받아
export하며 정확도 평가는 별도입니다. `export_m5_qwen_onnx.py`는 레거시 Qwen 0.5B ONNX용으로,
기본 M5인 Qwen2.5-1.5B GGUF Q5를 생성하지 않습니다. 새 환경에서는 모델 출처·라이선스·해시와
실행 하드웨어를 함께 기록하세요.

### 4. 실행

**GPU 모드 기본 (NVIDIA 개발 머신):**

```bash
docker compose up -d --build
```

**GPU 리소스 예약 명시 (docker-compose.gpu.yml 오버라이드):**

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

**CPU 모드 (Raspberry Pi 5):**

```bash
AI_DOCKER_TARGET=cpu-runtime docker compose up -d --build
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
docker logs rp5-ai-experts --tail 30
docker logs rp5-ai-qwen --tail 30
```

### 6. 더미 데이터 주입 (ESP32 없이 테스트)

```powershell
# Windows PowerShell
$env:MSYS_NO_PATHCONV=1
docker run --rm --network rp5_rp5-network `
  -v "${PWD}/scripts:/scripts" `
  -e REDIS_HOST=db `
  python:3.12-slim `
  sh -c "pip install redis -q && python /scripts/dummy_inject.py"
```

---

## 유용한 명령어

```bash
# 로그 확인
docker compose logs -f ai-experts
docker compose logs -f ai-qwen
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
├── docker-compose.yml          # 기본 구성 (GPU 기본)
├── docker-compose.gpu.yml      # GPU 리소스 예약 오버라이드
├── mosquitto.conf              # MQTT 브로커 설정
├── monitor.html                # 웹 대시보드 (마이크 M3/M4 테스트)
├── sensing/
│   ├── main.py                 # CSI UDP 수신 + 패킷 손실 추적
│   ├── audio_main.py           # 마이크 VAD 수집 (audio 프로파일)
│   └── Dockerfile
├── ai/
│   ├── main.py                 # 추론 메인 루프 (M1~M4, MQTT 발행)
│   ├── qwen_service.py         # M5 Qwen 독립 서비스 (ai-qwen 컨테이너)
│   ├── mqtt_helper.py          # MQTT 연결/발행 헬퍼
│   ├── experts/                # M1-M4 전문가 모듈
│   ├── logic/
│   │   ├── qwen_05b.py         # M5 Qwen SLM (decoder_with_past KV 캐시)
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
│   ├── dummy_inject.py         # ESP32 없이 더미 CSI+오디오 주입 (100Hz CSI / 0.5Hz audio)
│   ├── gen_sos_coeffs.py       # Butterworth SOS 계수 생성 → 펌웨어 biquad_coeffs.h
│   ├── csi_csv_logger.py       # CSI 스트림 CSV 로깅 유틸
│   ├── test_emergency_score.py # emergency_score 단위 테스트
│   ├── test_pipeline_integration.py # M1~M4 → emergency_score → Qwen 경계값 통합 테스트
│   ├── eval_qwen_accuracy.py   # Qwen 프롬프트/few-shot 정확도 평가 (100케이스 채점)
│   ├── qwen_eval_dataset.json  # 평가용 100케이스 정답셋 (ground truth)
│   ├── benchmark_gpu.py        # M1~M5 GPU 추론 레이턴시 벤치마크
│   ├── analyze_csi.py          # CSI 실측 로그 분석 (data/csi/*.csv)
│   ├── analyze_csi_by_node.py  # 노드별 CSI 구간 비교 분석
│   ├── analyze_nodes.py        # 노드 구성/상태 요약
│   ├── slm_chat_cli.py         # SLM 대화형 CLI 테스트
│   ├── slm_chat_demo.py        # SLM 시나리오 데모
│   ├── tts_worker.py           # TTS 음성 알림 워커 (MQTT 구독 + Redis 큐 소비)
│   ├── export_m1_wifi_pose_onnx.py
│   ├── export_m2_frenel_vital_onnx.py
│   ├── export_m3_ast_onnx.py
│   ├── export_m4_whisper_onnx.py
│   └── export_m5_qwen_onnx.py
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
| GPU 첫 추론 지연 | CUDA JIT 또는 모델 초기화 | 워밍업 로그를 확인하고 모델별 지연을 별도 측정 |
| `localhost` 간헐적 실패 | Windows IPv6 우선 | `127.0.0.1` 사용 |
| MQTT 연결 안 됨 | mosquitto 미실행 | `docker compose ps` 확인 후 재시작 |
| ai 컨테이너 exit 139 | ONNX TensorRT EP segfault | `ORT_USE_GPU=0` 또는 cpu-runtime 빌드 확인 |
| M3/M4 `expert_timeout` | CPU 추론 > 1s | `.env`에 `EXPERT_INFER_TIMEOUT_MS=5000` |
| M4가 "(음성 감지, 전사 미확정)"만 출력 | ① `onnx` 패키지 누락 ② optimum 1.17 ↔ transformers 4.46+ 비호환 ③ whisper_onnx `generation_config.json` 구형(lang_to_id 없음) | ①② `ai/requirements.txt`의 `onnx`·`optimum>=1.24` 반영 재빌드 ③ [openai/whisper-small의 generation_config.json](https://huggingface.co/openai/whisper-small/resolve/main/generation_config.json)으로 교체 (SungBeom/whisper-small-ko는 whisper-small 파인튜닝이라 호환) |
| 마이크 권한 오류 | HTTPS/file:// 접근 | `http://127.0.0.1:8081/monitor.html` 사용 |
| dummy_inject.py 경로 오류 | Git Bash 경로 변환 | `MSYS_NO_PATHCONV=1` 접두어 사용 |
| `python: not found` (ai-qwen) | Ubuntu 22.04 python3만 존재 | `command: ["python3", ...]` 사용 (이미 적용) |

---

## 연구 성과

SafeWave/1학기 캡스톤의 연구 방향과 연결된 논문
**「WiFi CSI와 음향 데이터의 다중 모달 융합을 통한 독거노인 낙상 감지 시스템 설계 방향 고찰」**이
2026 한국디지털콘텐츠학회 하계종합학술대회 대학생 논문경진대회 동상을 받았습니다
(2026-07-03, 한국디지털콘텐츠학회).

이 수상은 연구 문제 설정과 논문·발표 성과입니다. SafeWave의 낙상 정확도, 생체신호 오차,
임상 안전성 또는 Raspberry Pi 5 성능 검증으로 해석하지 않습니다.

---

## 변경 이력

### develop (미릴리즈) — 2026-09-17

- M1: 출력 이름이 `fall_logit`인 학습 모델(pose 레포 CNN-GRU)에 sigmoid 적용 — 기존 clip만으로는 0/1 포화
- sensing: 패킷 손실 계산을 wire contract(`seq_num` uint32)에 맞춤. 늦게 도착·중복 패킷은 기준 seq를 되돌리지 않음,
  크게 역행하면 재부팅으로 보고 기준 재설정
- API: `GET /system/resources` 신규, `/nodes/health`에 RSSI 추가. 대시보드에 시스템 자원·ESP32 노드 통신 패널 추가
- scripts: `bench_rpi5.py`(부하·지연 측정), `bench_startup.py`(기동 준비 시간), `eval_m4_stt.py`(M4 고정 세트 평가)
- 문서: `HANDOFF.md`(현재 작업 인계), `AGENTS.md`(Codex용 지침) 추가

### v0.2.0 — 2026-07-21 — M5 Qwen2.5-1.5B GGUF 통합 (qwen-llmops 이식)

**M5 모델 교체 (0.5B ONNX → 1.5B GGUF Q5_K_M):**
- [qwen-llmops](https://github.com/jinsan02/qwen-llmops)의 배포 후보를 이식
  - 별도 프로젝트 기록: Track B raw 0.985 (1000 합성 시계열 케이스), multi-domain 64/64
  - 별도 프록시 환경 기록: 단일스레드 p50 2.5s / p95 3.2s, RSS ~1.44 GB
  - 위 값은 SafeWave RPi5 실측이나 센서 정확도 결과가 아님
- `ai/logic/qwen_15b.py` 신규 — multi-turn few-shot(6개) + 시계열 압축요약 프롬프트 +
  가드레일(`vital_override` 강화판, `hallucination_guard`), 멀티 eos 정지, 첫 완결 JSON early-stop
- `ai/logic/qwen_gguf.py` 신규 — llama.cpp 백엔드 (qwen_15b 상속, 생성부만 교체)
- `ai/qwen_service.py` — `SLM_BACKEND` env 분기 (`gguf` 기본 / `15b` / `05b` 롤백)
- `ai/Dockerfile` — `gguf-runtime` 스테이지 신규 (cmake + llama-cpp-python 소스 빌드)

**`emergency_score.py` 룰 게이트 고도화 (llmops D1~D3 + 시계열):**
- D1: vital 위기 경계 재조정 — HR crit ≤35→**≤40**, RR crit ≤4→**≤5** (직상 서맥/서호흡 미탐 해소)
- D2: 확정 낙상(≥0.80) + 경보/충격음(≥0.80) 동시 → score floor 0.65 (`fall_hazard_bypass`)
- D3: 복합 부스트에 최소 피크 요구 — 2도메인 ≥0.90 / 3+도메인 ≥0.70 (중등도 신호 과승급 차단)
- **시계열 에스컬레이션**: `agg:minute:*` 최근 1h에서 지속 경고(≥60%) 또는 HR/RR 악화 추세 감지 시
  score floor 0.6 (M5 호출 보장). `ai/main.py` 게이트에 30s 캐시로 연결. `time_series=None`이면 기존과 동일.

**모델 아티팩트:**
- `volumes/models/qwen_15b_gguf_q5/` + `qwen_15b/` 토크나이저 — Git 제외, 별도 준비 필요

### v0.1.1 — 2026-06-16

**`emergency_score.py` 위험도 산식 고도화:**
- 복합 위험 보정 차등화: 2도메인 동시 이상 ×1.20 / 3도메인 ×1.35 / 4도메인 ×1.50 (기존 일괄 ×1.2)
- `vital_bypass`: M2 심박/호흡이 위기 범위(`vital_component==1.0`)면 score 최소값 0.65 보장 — 단일 생체신호 위기를 다른 도메인 점수로 희석되지 않게 함
- `keyword_fall_bonus`: M4 응급 키워드(`살려/도와/아파/응급/위험` 등) + M1 낙상 의심(`fall_score≥0.25`) 동시 발생 시 +0.15 가산

**Qwen-0.5B (`logic/qwen_05b.py`) 프롬프트 개선:**
- few-shot 5개 → 6개로 확장: alarm 카운터 예시(알람음 단독 → normal), fall_det 카운터 예시(낮은 신뢰도 낙상감지 → normal) 추가해 0.5B의 패턴 과적합(할루시네이션) 완화
- JSON 출력 스키마 단순화: `is_outlier`/`correlated_with_history` 필드 제거 (0.5B가 일관되게 생성 못 해 항상 `false`로 고정되던 죽은 필드)
- `max_new_tokens` 128 → 56 (생성 속도 개선, 출력은 JSON 한 줄이면 충분)
- `_apply_context_window`의 critical 강제 승급 로직 제거 — 최근 이력만으로 risk_level을 덮어쓰지 않고 모델 판단 우선
- env_label이 `alarm`/`impact`가 아닐 때 `qwen_reason`에서 "알람" 텍스트 후처리 제거 (환경음과 무관한 할루시네이션 텍스트 필터)
- **당시 평가 기록** (`scripts/eval_qwen_accuracy.py`, 합성 100케이스): Exact match 71%, Adjacent(±1) 81%, Critical recall 100%, Safe fail 0%, Normal FP rate 26.8%. 현재 로컬 ignored report는 exact 45/100, adjacent 83/100으로 달라 결과 계보 확인이 필요함
- multi-turn ChatML 포맷 시도 → FP율 53.5%로 악화되어 폐기, single-turn 텍스트 패턴 유지가 0.5B에 더 적합함을 확인

**`api/main.py` Phase 2 Active Verification 신규:**
- 응급 이벤트 발생 시 즉시 FCM 발송하지 않고 TTS "괜찮으세요?" 음성 확인 → STT 응답 대기 → 키워드 기반 의도 분류(`cancel_alarm`/`call_emergency`) → 최종 FCM 발송
- 무응답/timeout(15s)은 fail-safe로 `call_emergency` 처리
- `_alert_worker` 직렬 블로킹 제거: 응급 이벤트별로 `asyncio.create_task`로 분리해 다음 이벤트 큐 처리가 막히지 않게 함

**`ai/utils/__init__.py` 공통 유틸 통합:**
- `safe_float`, `stream_id_ts_ms`, `json_loads`, `build_context_window`, `get_session_opts` 추가 — `qwen_05b.py`/`qwen_service.py`/`ai/main.py`에 중복 구현되어 있던 동일 함수 통합
- 미사용 `TurboQuant` no-op 클래스 제거

**TTS 음성 알림 (`scripts/tts_worker.py`):**
- MQTT 구독(`safewave/ai/result`) 기반 경고 알림 + Redis `tts:speak:queue` 기반 응급 알림 발화 분리
- `docker-compose.yml` — `audio` 프로파일에서 분리해 항상 기동, Redis 의존성 추가, `/dev/snd` 오디오 디바이스 마운트

**모델 추론 안정화:**
- `m1_wifi_pose.py` — 입력 reshape을 ONNX 세션의 실제 input shape에서 동적으로 읽도록 수정 (하드코딩된 192×100 제거)
- `m3_ast_base.py`/`m4_whisper_small.py` — `get_session_opts()` 적용, GPU 모드에서 CPU-only 연산 Memcpy 경고 억제
- `sensing/main.py` — 패킷 손실 카운터 모듈러 연산을 펌웨어 struct(`uint16`)와 일치시킴 (기존 uint32 불일치로 손실률 오계산)
- `sensing/audio_main.py` — waveform을 JSON 배열 대신 raw bytes(`tobytes()`)로 전송, 페이로드 경량화

**대시보드 (`monitor.html`):** M1/M2 실시간 로그와 M3/M4 음성 로그 분리, 음성 로그는 5초 throttle + 내용 변경 시에만 출력 (중복 라인 방지)

**평가/분석 도구 신규 (`scripts/`):**
- `eval_qwen_accuracy.py` + `qwen_eval_dataset.json` — Qwen 프롬프트 회귀 테스트용 100케이스 정답셋, exact/adjacent/safe-fail/FP rate 자동 채점
- `test_pipeline_integration.py` — M1~M4 → emergency_score → Qwen 경계값 통합 테스트
- `benchmark_gpu.py` — M1~M5 GPU 추론 레이턴시 벤치마크
- `analyze_csi.py` / `analyze_csi_by_node.py` / `analyze_nodes.py` — ESP32 실측 CSI 로그 분석 도구

---

### v0.1.0 — 첫 시뮬레이션 파이프라인 확인

**AI 서비스 분리:**
- `rp5-ai` 단일 컨테이너 → `rp5-ai-experts` (M1~M4) + `rp5-ai-qwen` (M5/Qwen) 분리
  - M5 Qwen SLM이 전문가 모델의 추론 타임아웃에 영향을 주던 문제 해소
  - `qwen_service.py` 신규 — Redis `ai:result` 구독 + 독립 SLM 루프

**Qwen decoder_with_past (KV 캐시):**
- `ai/logic/qwen_05b.py` — M4 Whisper와 동일 패턴의 `decoder_with_past` 구현
  - prefill 1회 실행 → present KV 캐시 추출 → `model_with_past.onnx` 스텝 반복
  - `present.X.key/value` → `past_key_values.X.key/value` 동적 매핑 (레이어 수 무관)
  - `model_with_past.onnx` 없으면 자동으로 전체 시퀀스 greedy fallback

**빌드 기본값 변경:**
- AI 서비스 기본 빌드 타깃: `cpu-runtime` → `gpu-runtime`
  - NVIDIA 드라이버 없는 환경에서도 CPU 폴백으로 정상 동작 확인

**더미 시뮬레이터 관측값 (CPU 모드, 모델 없음 기준 — 정확도·RPi5 성능 아님):**

| 항목 | 측정값 |
|---|---|
| CSI 주입 속도 | 100 Hz (6,000 패킷 / 60s) |
| 오디오 이벤트 | ~30 건 / 60s (0.5 Hz) |
| `csi:raw` 스트림 길이 | ~6,000 엔트리 |
| `ai:result` 스트림 길이 | ~60 엔트리 (1 Hz 추론 루프) |
| M1 낙상 점수 범위 | 0.0 – 1.0 (더미 랜덤) |
| M2 HR/RR 추정 | heuristic fallback (모델 없음) |
| M3 환경음 라벨 | `no-audio` (오디오 미연결) |
| M4 STT | `""` (오디오 미연결) |
| M5 Qwen 판단 | `low` (heuristic fallback) |
| ai:result 엔트리 평균 레이턴시 | < 50 ms (CPU, ONNX 없음) |

**버그 수정:**
- `docker-compose.gpu.yml` — 구 `ai:` 서비스명 → `ai-experts` + `ai-qwen` 업데이트
- `ai-qwen` 컨테이너 `python: not found` — Ubuntu 22.04 호환 `python3` 명시

---

### ver.0.0.4 (병합됨)

**펌웨어 연동 — ESP32-S3 wire contract 확정:**
- 펌웨어 레포(`safewave-ai-ambient-monitoring-firmware`) 분석으로 788B 고정 UDP 패킷 구조 확정
- `struct.Struct("<4sBBHIIhH192f")` — 20B 헤더 + float32×64 × 3블록 (raw / resp / heart)
- ESP32가 0.1–0.6Hz(호흡) / 0.8–3.0Hz(심박) 4차 Butterworth DF-II Transposed IIR 필터를 온디바이스 수행
- CSI 수집 주파수 100Hz 확정 (`CSI_FS=100`)

**AI 엔진:**
- `ai/main.py` — M2 시간축 누적 버퍼 추가 (`M2_CSI_WINDOW_FRAMES=300` 환경변수, per-node deque)
- `ai/experts/m2_frenel_vital.py` — FFT fallback `N < 10` 가드 추가

**스크립트:**
- `scripts/dummy_inject.py` — `CSI_HZ` 10 → 100 (펌웨어 실측 주파수 반영)
- `scripts/gen_sos_coeffs.py` 신규

---

### ver.0.0.3

**AI 엔진:**
- `ai/logic/emergency_score.py` 신규 — 도메인 가중치 기반 위험도 계산 분리 (fall 40% / vital 30% / sound 15% / speech 15%)
- `ai/utils/__init__.py` segfault 수정 — `ORT_USE_GPU` 환경변수로 CUDA EP 제어
- M3 출력 필드 추가: `ast_top_class`, `ast_top_confidence`, `label`, `confidence`
- 심박 warning 임계값 수정: `_HR_WARN_LO` 50 → 55 bpm

**센싱:**
- `sensing/main.py` — `node:N:health` hset + expire pipeline 원자화

**API:**
- `sys:settings` TTL 보장 — POST `/settings` 저장 시 expire 누락 보완

**스크립트 (신규):**
- `scripts/dummy_inject.py` — ESP32 없이 더미 CSI + 오디오 이벤트 주입
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

**API:**
- `POST /audio/events` — 오디오 이벤트 수동 주입
- `GET /system/redis-memory`, `GET /system/health` 신규
- FCM alert_worker 비정상 종료 시 자동 재시작

**신규 파일:**
- `docker-compose.gpu.yml` — GPU 리소스 예약 오버라이드
- `mosquitto.conf` — MQTT 브로커 설정
- `ai/mqtt_helper.py` — MQTT 연결/발행 헬퍼

### ver.0.0.1

- M1~M5 인터페이스 라인업 구성. M1/M2는 학습 모델이 아니라 단순화 export 스텁이며,
  M3~M5 외부 모델 아티팩트는 Git에 포함되지 않음
- Docker Compose 기반 초기 스택 구성 (db / sensing / ai / api)
- GPU/CPU 겸용 ONNX 런타임 구성
- `/status` no-data 처리 안정화
