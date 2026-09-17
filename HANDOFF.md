# HANDOFF — SafeWave-AI (rp5)

> 작성: 2026-09-17 · 작성자 노진산(+Claude) · 다음 작업자(Codex 포함)는 이 문서부터 읽을 것.
> 규칙·제약은 `AGENTS.md` / `CLAUDE.md`, 근거 현황은 `docs/validation_status.md`.

## 한 줄 요약

**오늘 목표는 RPi5에서 develop 기본값 속도 측정.** 팀원 모델 브랜치 병합은 그다음 단계다. 서두르지 않는다.

---

## 1. 현재 상태 (2026-09-17 오후)

### 저장소

| 위치 | 브랜치 / 커밋 | 비고 |
|---|---|---|
| GitHub `origin/develop` | 이 문서를 추가한 커밋 | 기준 |
| 노트북 `C:\rp5` | `develop` | 로컬 Docker는 **검증용**(GPU 이미지, `ORT_USE_GPU=0`). 여기 수치를 RPi5 표에 넣지 말 것 |
| RPi5 `~/safewave` | `develop` (오늘 `perf/ort-thread-limit`에서 전환) | 아래 stash 참고 |

### RPi5

| 항목 | 값 |
|---|---|
| 접속 | `ssh csi@192.168.1.2` (노트북 `id_ed25519` 키 등록 완료) |
| 주의 | IP가 DHCP로 바뀜. 예전 `192.168.0.13`/`rp5` 계정은 무효. 노트북 `~/.ssh/config`는 아직 옛 항목 |
| 하드웨어 | Raspberry Pi 5 Model B Rev 1.1, 8 GB, 4코어, NVMe 234 GB(여유 약 78 GB) |
| OS / Docker | Debian 13 기반, kernel 6.12.75+rpt-rpi-2712 / Docker 29.4.3, Compose v5.1.3 |
| `.env` (Git 미추적) | `AI_DOCKER_TARGET=cpu-runtime`, `M1_MAX_NODES=1`, `M1_FALL_THRESHOLD=0.7` 등 |
| ESP32 | 노드 1·2·3 연결, 합계 약 300 pkt/s |
| stash | `stash@{0}`: 팀원의 미커밋 수정(ORT 스레드 제한 실험 + M1 sigmoid). 삭제하지 말고 팀원과 정리 |

### RPi5 모델 (`~/safewave/volumes/models/`, Git 미포함)

| 모델 | 폴더 | 상태 |
|---|---|---|
| M1 | `m1_wifi_pose_onnx/` | **김태연 학습 CNN-GRU** (입력 `(1,1,64,100)`, 출력 `fall_logit`). 노트북의 5노드 스텁으로 덮어쓰지 말 것 |
| M2 | `m2_frenel_vital_onnx/` | 노트북에서 복사한 미학습 스텁 |
| M3 | `ast_onnx/` | 노트북에서 복사한 6/11자 구버전 AST |
| M4 | `whisper_onnx/` | 노트북 fp32 ONNX 복사 (작성 시점에 복사 진행 중 — 크기 1.76 GB 확인할 것) |
| M5 | `qwen_15b_gguf_q5/` + `qwen_15b/` | GGUF 1.23 GB + 토크나이저 (작성 시점에 GGUF 복사 진행 중) |

### 진행 중이던 작업 (작성 시점)

- RPi5 이미지 빌드: `nohup docker compose build db sensing ai-experts ai-qwen api` → 로그 `~/build_20260917.log`
  - db·sensing·api는 완료(단, 오늘 수정 **이전** 커밋 기준). ai-experts·ai-qwen(llama.cpp ARM 컴파일)은 진행 중이었음
- 노트북 → RPi5 모델 복사(scp)

확인 명령:

```bash
ssh csi@192.168.1.2 "pgrep -f 'compose build' >/dev/null && echo 빌드중 || echo 빌드끝; grep -iE '^ERROR|failed to solve' ~/build_20260917.log | tail; du -sh ~/safewave/volumes/models/*"
```

---

## 2. 오늘 한 일 (2026-09-15 ~ 09-17)

- develop 문서 정직화 커밋 5건 (README·주석·export 스크립트의 미검증 수치 제거, `docs/validation_status.md` 추적)
- 노트북 Docker Desktop 기동 실패 해결: 남은 유닉스 소켓 파일 때문. `%LOCALAPPDATA%\Docker\run`,
  `%LOCALAPPDATA%\docker-secrets-engine` 폴더를 **이름만 바꿔서** 해결(`*.stale-20260917`). 공장 초기화 불필요
- RPi5 레포 develop 전환, 팀원 수정 stash 보관, 이미지 재빌드 시작, 모델 복사
- 코드 수정 (이 문서와 같은 커밋)
  - `ai/experts/m1_wifi_pose.py`: 출력 이름이 `fall_logit`이면 sigmoid. RPi5 모델로 확인 — 점수 0.29~0.62 연속값(수정 전 0/1 포화)
  - `sensing/main.py`: seq 차이를 uint32로 계산, 늦게 온·중복 패킷은 기준을 되돌리지 않음, 크게 역행하면 재부팅으로 보고 기준 재설정. 8개 시나리오 통과
  - `api/main.py`: `GET /system/resources` (호스트 CPU 전체·코어별, 온도, 메모리, 디스크 — 저장 없음)
  - `monitor.html`: 시스템 자원 패널. **제목에 연결된 API 주소가 표시됨** — `localhost`면 노트북 값이다
- 문서: README·CLAUDE.md·`docs/validation_status.md` 갱신, `AGENTS.md`·이 문서 추가

---

## 3. 오늘 남은 일 — RPi5 develop 기본값 속도 측정

### 3-1. 최신 코드 반영 (빌드가 끝난 뒤)

오늘 수정은 빌드 시작 후에 커밋됐으므로 한 번 더 받아서 다시 빌드해야 한다. 캐시 덕분에 짧다.

```bash
ssh csi@192.168.1.2
cd ~/safewave && git pull --ff-only origin develop && git log --oneline -1
docker compose build sensing api ai-experts
docker compose up -d db mqtt sensing ai-experts ai-qwen api
docker compose ps
```

`tts-worker`/`homeassistant`/`audio-sensing`은 오늘 측정 범위 밖이면 올리지 않아도 된다.

### 3-2. 동작 확인

- 노트북 대시보드: `python -m http.server 8081` → `http://127.0.0.1:8081/monitor.html?api=http://192.168.1.2:8000`
  - 자원 패널 제목이 `192.168.1.2:8000`인지, 코어 4개가 보이는지 확인
  - 모델 토글 M1~M5 모두 켜짐 확인 (`GET /settings`)
- 로그: `docker compose logs --tail 50 ai-experts ai-qwen` — `warmup_failed`, `expert_failure`, `qwen_warmup_completed` 확인
- 측정 기준 커밋 해시를 기록한다

### 3-3. 측정 항목과 방법

`docs/benchmark_template.md` 양식에 채우고, **원시 로그를 같이 보관**한다. 입력 조건(실 ESP32 3노드 / 마이크 유무)을 적는다.

| 항목 | 방법 |
|---|---|
| 호스트 CPU·온도·메모리·디스크 | `curl http://192.168.1.2:8000/system/resources` 반복, `top -bn1`, `vcgencmd measure_temp`, `free -m` |
| 컨테이너별 CPU | `docker stats --no-stream` (메모리는 cgroup 꺼져 있어 0B — 무시) |
| Redis 메모리 | `docker exec rp5-db redis-cli INFO memory` |
| M1·M2 지연 | `ai:result`의 `expert_latency_ms.fall` / `.vital` 분포 |
| **M3·M4 지연** | `ai:result`로 재지 말 것. `audio:result` 스트림 ID 시각 − payload `ts_ms` |
| M5 지연 | `docker logs rp5-ai-qwen \| grep qwen_invoked` 의 `qwen_infer_ms` |
| 패킷 손실 | `node:N:health`의 `rx`·`last_seq`를 수 초 간격 두 번 읽어 증가량 비교 |
| backlog | `docker logs rp5-ai-experts \| grep -c csi_backlog_skipped` (구간당) |
| 장시간 | 30분·1시간 구동 후 재시작·OOM·스로틀링 여부 |

M5는 위험도가 임계값(0.6)을 넘을 때만 호출된다. 표본이 부족하면 측정 동안만 `/settings`의
`risk_threshold`를 낮추고, **끝나면 0.6으로 되돌린다.** (`POST /settings`는 전체 교체라 GET → 수정 → POST)

M3·M4 입력이 필요하면 대시보드 마이크 패널이나 `scripts/dummy_inject.py`를 쓴다. 더미는 2초마다
오디오를 넣으므로 실제 VAD 입력보다 가혹한 조건임을 기록한다.

---

## 4. 다음 단계 — 팀원 모델 병합 (기본값 측정이 끝난 뒤)

원칙: **한 번에 하나씩** 병합 → 모델 교체 → 3-3과 같은 방법으로 측정 → 기본값과 비교.
`feature/*` 등 팀원 브랜치에는 커밋하지 않는다.

| 순서(제안) | 대상 | 원격 위치 | 확인할 것 |
|---|---|---|---|
| 1 | M4 이대경 — Whisper LoRA 파인튜닝 INT8 (234 MB) | `origin/m4/lee-daegyeong-whisper-int8`, 태그 `m4-onnx-int8-20260916` | 브랜치가 `ai/` 여러 파일을 건드림 → develop과의 diff 검토 후 병합. `m4_whisper/README.md`, `VERIFICATION.md`, `artifacts.json`(체크섬) 확인 |
| 2 | M1·M2 김태연 | `origin/feature/M1-M2` (09-16 인계 산출물) | M1 입력 노드 수와 `M1_MAX_NODES` 일치, `fall_logit` 여부, M2 기준 신호 확보 여부 |
| 3 | M3 소민섭 — v3.4 (09-13 배포 결정) | 아직 인계 브랜치 없음 (`origin/feature/ast-base`는 5월 것) | HF 포맷 + `preprocessor_config.json` + `id2label`. **log-Mel 전처리 구현**, **라벨은 이름으로 매핑**(인덱스 매핑 시 7종↔6종 불일치), 운영 임계값 0.6. 계획서의 오탐 4.03회/h는 09-11 held-out 3.97시간 기준(8/30의 1.51회/h는 평가 녹음 일부가 학습에 섞인 낙관 편향) |

병합 뒤 반드시: `emergency_score` 경계 테스트 회귀, 5노드/1노드 주입 시 expert 오류 0, 점수 범위 0~1.

---

## 5. 알려진 문제 · 결정 필요

1. **M1이 보는 노드 1이 패킷을 약 35% 잃는다.** `M1_MAX_NODES=1`이라 M1은 노드 1만 쓴다
   (노드 2·3은 손실 거의 0). M1 버퍼가 자주 끊겨 `packet_gap_detected → deque_reset`가 난다.
   노드 1 ESP 위치·전원·안테나 점검 또는 노드 번호 교체를 검토.
2. **M3·M4 대기열 누적 위험.** 노트북에서 오디오 1건 처리 약 2.75초 > 입력 간격 2초 → 지연이 선형 증가.
   RPi5에서 건당 처리 시간이 입력 간격보다 길면 Phase 2 응답이 15초 창을 넘겨 응급으로 오판정될 수 있다.
3. **M1 워밍업 버그.** `ai/main.py`가 1차원 신호로 워밍업 → `(1,1,64,100)`. 1노드 모델에선 동작하지만
   5노드 모델에선 실패해 첫 추론에 세션 초기화 비용이 섞인다. 5노드 모델 병합 전에 고칠 것.
4. **RPi5 `stash@{0}` 정리.** sigmoid는 develop에 반영됨, ORT 스레드 제한은 develop의 `get_session_opts`와 중복,
   M3 부분은 `sess_options`를 두 번 넘기는 오류가 있었다. 팀원 확인 후 drop 여부 결정.
5. RPi5 메모리 cgroup 비활성 → 컨테이너별 메모리 측정 불가. 필요하면 `cmdline.txt`에
   `cgroup_enable=memory` (재부팅 필요, 사람 확인 후).
6. RPi5에 다른 프로젝트 이미지(`csi-*`, `ai_hack_camp_2026-*`, 약 15 GB)가 있다. 삭제는 소유자 확인 후.
7. 노트북 `~/.ssh/config`의 RPi5 항목이 옛 주소(`192.168.0.13`, `rp5`)다.
8. `sensing` 손실 누적 카운터는 컨테이너 재시작 전까지 과거 값을 유지한다. 측정은 증가량으로.
9. **ai-experts 이미지에 CUDA판 torch가 들어간다.** `requirements.txt`에 torch는 없지만 `optimum`이 끌어오고,
   ARM용 최신 torch 휠이 `nvidia-*` CUDA 13 패키지를 함께 설치한다(8/2 이미지: torch 2.13.0+cu130, nvidia 패키지 15개,
   이미지 8.79 GB). RPi5엔 GPU가 없어 용량·빌드 시간만 늘어난다. 또 M4(`ORTModelForSpeechSeq2Seq`)의 생성 루프가
   torch를 쓰므로 "M1~M4는 ONNX Runtime만" 제약과 어긋난다. 기본값 측정이 끝난 뒤 `cpu-runtime`에서 CPU 전용 torch
   인덱스(`https://download.pytorch.org/whl/cpu`)로 고정하는 방안을 검토 — 측정 기준 커밋이 바뀌므로 측정 전에는 손대지 않는다.

---

## 6. 참고

- 계획·일정: 개발계획서 7장 (9/21 실기기 성능 측정 → 9/28 M3 통합 → 9/30 M4 인계 → 10/12 M1 학습 → 10/19 통합 → 10/26 실환경 검증 → 10/31 실험 종료, 11월 발표)
- 펌웨어 wire contract: `CLAUDE.md` 하단 표
- 측정 양식: `docs/benchmark_template.md` / 근거 현황: `docs/validation_status.md`
