# AGENTS.md — SafeWave-AI (rp5)

Codex 등 코딩 에이전트용 지침. Claude용 `CLAUDE.md`와 핵심 제약은 같다.
**작업을 시작하기 전에 `HANDOFF.md`를 먼저 읽어라.** 현재 진행 상황과 다음 할 일이 거기 있다.

## 프로젝트

독거노인 낙상 감지 시스템 프로토타입. ESP32-S3 WiFi CSI + 마이크 음향을 Raspberry Pi 5에서
Docker Compose로 처리한다. 의료기기가 아니며, 구현 완료를 검증 완료로 표현하지 않는다.

```
ESP32-S3 ──UDP:5005──▶ sensing ──▶ Redis csi:raw ──▶ ai-experts (M1~M4) ──▶ ai:result
마이크 ──▶ audio:events ──▶ ai-experts 오디오 워커 (M3/M4) ──▶ audio:result ─┘
ai:result ──▶ ai-qwen (M5) ──▶ ai:emergency ──▶ api (FastAPI/WS, FCM, Phase 2 음성 확인)
```

| 서비스 | 경로 | 역할 |
|---|---|---|
| sensing | `sensing/main.py` | 788B UDP 수신 → `csi:raw`, 노드 헬스 |
| ai-experts | `ai/main.py`, `ai/experts/` | M1 낙상, M2 바이탈, M3 환경음, M4 한국어 STT |
| ai-qwen | `ai/qwen_service.py`, `ai/logic/` | M5 통합 위험도 (Qwen2.5-1.5B GGUF) |
| api | `api/main.py` | REST/WS, 설정, 알림, Phase 2 |
| db / mqtt | Redis / Mosquitto | 메시지 버스 |
| 대시보드 | `monitor.html` | 정적 페이지, `?api=http://<host>:8000` |

## 반드시 지킬 제약

- **운영 데이터는 Redis에만 둔다.** 디스크에 쓰는 캐시·로컬 DB를 만들지 않는다.
- **새 Redis 키에는 반드시 EXPIRE(≤3600초)를 붙인다.** 키 이름·TTL·스트림 구조를 바꿀 때는 먼저 사람에게 확인한다.
- **CSI는 100Hz, 788바이트, `struct.Struct("<4sBBHIIhH192f")`.** `seq_num`은 uint32.
  ESP32가 IIR 필터를 수행하므로 RPi5에서 추가 필터링하지 않는다.
- **M2 입력은 시간 시리즈.** `data_resp`/`data_heart`는 64채널 공간 스냅샷이며, `ai/main.py`의 노드별 deque로 누적해 쓴다.
- **M1~M4 추론은 ONNX Runtime만 쓴다.** PyTorch/TF 런타임 금지. 예외: M5(ai-qwen 컨테이너)는 llama.cpp GGUF 허용.
- **언어는 Python만.** JS/TS/Node를 새로 도입하지 않는다 (`monitor.html`의 기존 인라인 스크립트 수정은 허용).
- **서비스 경계를 지킨다.** 서비스 간 직접 import 금지, Redis·MQTT로만 통신.
- 로깅은 기존 `_log(level, event, **fields)` 패턴을 따른다.
- `ai/main.py`의 ThreadPoolExecutor·타임아웃 구조를 임의로 바꾸지 않는다.

## 작업 방식

1. **가정하지 않는다.** 해석이 여러 개면 제시하고, 불확실하면 묻는다.
2. **요청된 것만 한다.** 투기적 기능·추상화·설정 옵션을 추가하지 않는다.
3. **외과적으로 바꾼다.** 인접 코드·주석·포맷을 "개선"하지 않는다. 기존 dead code는 언급만 한다.
4. **검증 가능한 목표를 세우고 확인될 때까지 반복한다.** 예: "M1 교체" → "점수 범위 0.0~1.0 유지 확인 후 교체".

## Git 규칙

- 작업 브랜치: `develop`. 원격: `https://github.com/jinsan02/safewave-ai-ambient-monitoring.git`
- `feature/*` 등 팀원 브랜치에는 **커밋하지 않는다** (참고·병합 대상일 뿐).
- 팀원 브랜치 병합은 **develop 기본값 측정이 끝난 뒤** 모델 하나씩 병합 → 측정 순서로 한다.
- 커밋 메시지에 `Co-Authored-By` 줄을 넣지 않는다.
- 남의 미커밋 수정은 지우지 말고 `git stash`로 보관한다. push·force 작업은 사람 확인 후 진행.

## 측정 원칙

- `docs/benchmark_template.md`에는 **RPi5에서 잰 값만** 넣는다. 개발 PC·GPU 수치 금지.
- 기록마다 커밋 해시, 모델 파일, 환경변수, 입력 조건(시뮬레이터/실 ESP32)을 남긴다.
- M3/M4 지연은 `ai:result.expert_latency_ms`로 재지 않는다 (오디오 워커에서 돌아 ~0ms).
  `audio:result` 스트림 ID 시각 − payload `ts_ms`로 잰다.
- 패킷 손실은 `node:N:health` 누적 비율 대신 두 시점의 `rx`·`last_seq` 증가량으로 본다.
- RPi5는 메모리 cgroup이 꺼져 있어 `docker stats` 메모리가 0B로 나온다. 호스트 메모리나 RSS를 쓴다.
- 근거 현황과 주장 범위는 `docs/validation_status.md`를 따른다.

## 자주 쓰는 명령

```bash
# 스택
docker compose up -d
docker compose logs -f ai-experts
docker compose exec db redis-cli XLEN ai:result

# 상태
curl http://localhost:8000/status
curl http://localhost:8000/system/resources

# 더미 주입 (ESP32 없이). REDIS_HOST=db, 컨테이너 안에서 실행
docker cp scripts/dummy_inject.py rp5-ai-experts:/tmp/dummy_inject.py
docker exec -e INJECT_SECONDS=30 rp5-ai-experts python3 /tmp/dummy_inject.py

# 대시보드 (노트북)
python -m http.server 8081   # http://127.0.0.1:8081/monitor.html?api=http://<RPi5-IP>:8000
```

RPi5 접속 정보와 현재 IP는 `HANDOFF.md`에 있다 (DHCP라 바뀔 수 있음).
RPi5의 `.env`는 Git 추적 대상이 아니며 `AI_DOCKER_TARGET=cpu-runtime`, `M1_MAX_NODES=1`이 설정돼 있다.

## 새 전문가 모델 추가 체크리스트

- [ ] `ai/experts/mN_name.py` — 기존 `WifiPoseModel` 패턴을 따른다
- [ ] `scripts/export_mN_name_onnx.py`
- [ ] `ai/main.py`의 `EXPERT_LATEST_KEYS`에 등록
- [ ] `docker-compose.yml` 모델 볼륨 경로 확인
- [ ] `docs/benchmark_template.md`에 지연 항목 추가
