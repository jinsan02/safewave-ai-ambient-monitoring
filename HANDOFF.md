# HANDOFF — SafeWave-AI (rp5)

> 작성: 2026-09-17 · 작성자 노진산(+Claude) · 다음 작업자(Codex 포함)는 이 문서부터 읽을 것.
> 규칙·제약은 `AGENTS.md` / `CLAUDE.md`, 근거 현황은 `docs/validation_status.md`.

## 한 줄 요약 (09-17 저녁)

RPi5 기본값 측정, M1·M2(김태연)·M4(이대경) 병합, fp32 대 INT8 비교까지 끝났다.
**다음 단계는 통합 파트 전체 리팩토링**(누수·오류·허점 점검, 클린 코드)이고, 그와 함께
`handoff/rpi5-20260917/TUNING_PROPOSAL.md`의 조정안을 반영한 뒤 같은 조건으로 1회 재측정한다.
담당자 공유 문서: `handoff/rpi5-20260917/TO_KIMTAEYEON_M1_M2.md`, `TO_LEEDAEGYEONG_M4.md`.
측정 원자료·상세 기록: 노트북·RPi5 `reports/rpi5-20260917/` (`TEST_LOG.md`, Git 제외).

---

## 1. 현재 상태 (2026-09-17 16:58)

### 저장소

| 위치 | 브랜치 / 커밋 | 비고 |
|---|---|---|
| GitHub `origin/develop` | 이 문서를 갱신한 커밋 | PR #3(M1·M2), PR #4(M4) 병합 포함. `feature/M1-M2`, `m4/lee-daegyeong-whisper-int8` 브랜치 유지 |
| 노트북 `C:\rp5` | `develop` | 로컬 Docker 수치는 판단에 쓰지 않는다 (RPi5만 의미 있음) |
| RPi5 `~/safewave` | `develop` `8d9d4f8` + **미커밋 3개** | `ai/experts/m1_wifi_pose.py`, `scripts/bench_rpi5.py`, `scripts/eval_m4_stt.py` — 노트북 커밋과 같은 내용. pull 전에 `git checkout -- <파일>`로 치울 것 |

### RPi5

| 항목 | 값 |
|---|---|
| 접속 | `ssh csi@192.168.1.2` (노트북 `id_ed25519` 키 등록 완료) |
| 주의 | IP가 DHCP로 바뀜. 예전 `192.168.0.13`/`rp5` 계정은 무효. 노트북 `~/.ssh/config`는 아직 옛 항목 |
| 하드웨어 | Raspberry Pi 5 Model B Rev 1.1, 8 GB, 4코어, NVMe 234 GB (**사용 33%**, 빌드 캐시 88 GB 정리) |
| OS / Docker | Debian 13 기반, kernel 6.12.75+rpt-rpi-2712 / Docker 29.4.3, Compose v5.1.3 |
| 부팅 | **콘솔(`multi-user.target`)** — 09-17 데스크톱 GUI 해제. 되돌리기: `sudo systemctl set-default graphical.target` |
| `.env` (Git 미추적) | `AI_DOCKER_TARGET=cpu-runtime`, **`M4_KO_STT_MODEL=whisper_onnx_int8_ft_svc`**. `M1_MAX_NODES`·`M1_FALL_THRESHOLD`는 삭제(코드 기본 5·0.80). 백업 `~/backup/.env.bak-20260917*` |
| 상태 (16:58) | 6개 서비스 실행, 재시작 0, 메모리 5.3 GB·스왑 979 MB, ai-experts 2.2 GB, ai-qwen 3.3 GB, 72.5 °C |
| ESP32 | 노드 1·2·3 연결, 합계 약 300 pkt/s. 09-17 전원 재시작 후 손실 0.3~4.5% (5절 1번) |
| stash | `stash@{0}`: 팀원의 미커밋 수정(ORT 스레드 제한 실험 + M1 sigmoid). 삭제하지 말고 팀원과 정리 |

### RPi5 모델 (`~/safewave/volumes/models/`, Git 미포함)

| 모델 | 폴더 | 상태 |
|---|---|---|
| M1 | `m1_wifi_pose_onnx/` | **김태연 인계 모델** (3노드 학습 → 5채널, 입력 `(1,5,64,100)`, 출력 `fall_score`, 그래프 내 sigmoid). 이전 1노드 모델은 `m1_wifi_pose_onnx_1node_20260802/` |
| M2 | `m2_frenel_vital_onnx/` | 미학습 스텁 (심박 118 고정 → 경고 유발, 조정안 M2) |
| M3 | `ast_onnx/` | 6/11자 AST |
| M4 (기본) | `whisper_onnx_int8_ft_svc/` | 이대경 INT8의 **평가·서비스용 복사본** — `generation_config.json`만 fp32 것으로 교체(원본 `.orig`), 가중치는 원본과 하드링크 |
| M4 원본 | `whisper_onnx_int8_ft/` | 이대경 INT8 원본 (무수정) |
| M4 이전 | `whisper_onnx/` | fp32 `SungBeom/whisper-small-ko` (되돌릴 때 `.env`의 `M4_KO_STT_MODEL` 줄 삭제) |
| M5 | `qwen_15b_gguf_q5/` + `qwen_15b/` | GGUF 1.23 GB + 토크나이저 |
| 평가 데이터 | `data/m4_eval_2398/` | 이대경 M4 고정 평가 세트 (공개 저장소 업로드 금지) |

### 진행 중인 작업

없음 (16:58 기준 모든 측정 종료, 백그라운드 작업 없음).

---

## 2. 오늘 한 일 (2026-09-15 ~ 09-17)

- develop 문서 정직화 커밋 5건 (README·주석·export 스크립트의 미검증 수치 제거, `docs/validation_status.md` 추적)
- 노트북 Docker Desktop 기동 실패 해결: 남은 유닉스 소켓 파일 때문. `%LOCALAPPDATA%\Docker\run`,
  `%LOCALAPPDATA%\docker-secrets-engine` 폴더를 **이름만 바꿔서** 해결(`*.stale-20260917`). 공장 초기화 불필요
- RPi5 레포 develop 전환, 팀원 수정 stash 보관, 이미지 재빌드 시작, 모델 복사
- 코드 수정 (이 문서와 같은 커밋)
  - `ai/experts/m1_wifi_pose.py`: 출력 이름이 `fall_logit`이면 sigmoid (오전) → **오후 M1 인계 병합 때 삭제**(인계 모델은 그래프 안에 sigmoid)
  - `sensing/main.py`: seq 차이를 uint32로 계산, 늦게 온·중복 패킷은 기준을 되돌리지 않음, 크게 역행하면 재부팅으로 보고 기준 재설정. 8개 시나리오 통과
  - `api/main.py`: `GET /system/resources` (호스트 CPU 전체·코어별, 온도, 메모리, 디스크 — 저장 없음)
  - `monitor.html`: 시스템 자원 패널. **제목에 연결된 API 주소가 표시됨** — `localhost`면 노트북 값이다
- 문서: README·CLAUDE.md·`docs/validation_status.md` 갱신, `AGENTS.md`·이 문서 추가
- (오후) RPi5 기본값 측정: 기동 3회, 전체 동작, 모델별 단독, OOM 22회 사건 기록 → `reports/rpi5-20260917/TEST_LOG.md`
- (오후) 측정 도구: `bench_rpi5.py`(재시작·스왑 기록, 오염 회차 자동 무효 표시), `bench_startup.py`, `eval_m4_stt.py`(피크 메모리, 인계 wrapper 백엔드 옵션), `m4_handoff_eval.Dockerfile`(인계 wrapper 전용 환경 — RPi5 미사용)
- (오후) 대시보드: ESP32 노드 통신 패널, `/nodes/health`에 RSSI
- (오후) RPi5 데스크톱 GUI 해제, M1·M2 병합(PR #3) 및 M1 임계값 `M1_FALL_THRESHOLD` 0.80, M4 병합(PR #4)
- (오후) M4 fp32 대 INT8 RPi5 평가(직접 녹음 28개) 및 전체 스택 비교 → **M4 기본값 INT8 전환**
- (오후) 버전 v0.3.0 (`VERSION`, README)

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

### 3-3. 측정 도구와 순서

`scripts/bench_rpi5.py`를 RPi5 호스트에서 실행한다 (redis-py·ffmpeg·vcgencmd 설치돼 있음).
결과는 `~/safewave/reports/bench/<시각>-<label>/`에 `summary.md`·`raw.json`·ai-experts 로그로 남는다.
`--models`·`--threshold` 변경은 끝나면(오류 포함) 원래 설정으로 되돌린다. 음성 샘플은
`~/safewave/data/bench_audio/{emergency_ko,safe_ko}.mp3` (edge-tts 생성, Git 미포함).

```bash
cd ~/safewave
B="python3 scripts/bench_rpi5.py"
A=data/bench_audio/emergency_ko.mp3
$B --label base-idle     --duration 300                                   # 전 모델 on, 오디오 없음
$B --label base-audio    --duration 300 --audio $A --audio-every 10       # M3+M4 부하
$B --label base-m5       --duration 180 --threshold 0.0                   # M5 반복 호출(쿨다운 5초)
$B --label base-peak     --duration 180 --threshold 0.0 --audio $A --audio-every 10   # 응급 시 M4+M5 동시
$B --label solo-m1       --duration 120 --models m1
$B --label solo-m2       --duration 120 --models m2
$B --label solo-m3       --duration 120 --models m3 --audio $A --audio-every 10
$B --label solo-m4       --duration 120 --models m4 --audio $A --audio-every 10
$B --label solo-m5       --duration 120 --models m5 --threshold 0.0
$B --label base-long     --duration 1800 --interval 15                    # 발열·안정성
```

`--audio-every`를 줄여 가며 M3+M4 지연이 누적되기 시작하는 간격(= 오디오 1건 처리 시간)을 찾는다.
팀원 모델 병합 뒤에는 **같은 label 체계에 접두어만 바꿔** (`m4int8-audio` 등) 다시 돌려 비교한다.

### 3-3b. 기동 준비 시간

`python3 scripts/bench_startup.py --runs 3` — AI·API·sensing 컨테이너를 다시 만들고 서비스별 준비 신호
(ai-experts `engine_init_completed`, ai-qwen `qwen_service_started`, api `/status`, sensing 노드 갱신)와
첫 `ai:result`까지의 시간, 기동 중 메모리·스왑 최고치를 잰다. 회차마다 파이프라인이 1분가량 멈춘다.
결과는 `reports/startup/<시각>/`. 모델이 페이지 캐시에 있는 warm 조건이며, 재부팅 직후 cold는 따로 잰다.
09-17 첫 기동(로그 기준): ai-experts 약 62 s(그중 M4 워밍업 약 47 s), ai-qwen 워밍업 포함 약 60 s.
기동 직후 메모리 사용 7.2 GB / 8 GB, 스왑 583 MB — 측정 시 스왑 여부를 함께 본다.

### 3-4. M4 STT 정확도·지연 평가 (이대경 고정 평가 세트)

- 데이터: `data/m4_eval_2398/` (노트북·RPi5 모두, Git 제외). 2,398개 WAV = AI Hub 2,370 + 직접 녹음 28,
  라벨 fall_related/help_direct 각 1,199. SHA-256 2,398개 대조 완료. **원본 음성이라 공개 저장소 업로드 금지.**
  이 세트는 개발 판단에 쓰인 세트라 최종 독립 테스트 결과로 보고하지 않는다.
- 도구: `scripts/eval_m4_stt.py` — 운영 `WhisperSmallModel`을 그대로 불러 CER/WER/키워드/지연/RTF 측정.
  지표 정의는 이대경 `CT2_REFERENCE.md`와 같지만 디코딩 경로(transformers greedy)가 CT2와 달라
  **CT2 수치(파인튜닝 INT8 CER 4.28% 등)와 직접 비교하지 않는다.**
- 현재 기본 M4: `SungBeom/whisper-small-ko` fp32 Optimum ONNX (이대경 표의 Zeroth 기본 모델과 다른 모델).
- 노트북 참고(28개 직접 녹음, x86 CPU): CER 44.2%, WER 80.0%, 키워드 46.4%, 파일당 1.76 s.
  짧은 응급 발화에서 환각 삽입이 많다("119 불러줘" → "저기 요즘은 너무 많이 배우고 싶어요"). RPi5 수치 아님.

RPi5 실행 순서 (`~/safewave`):

```bash
E="docker compose run --rm --no-deps -v ./data/m4_eval_2398:/eval:ro -v ./scripts:/scripts:ro -v ./reports:/reports"
M="python3 /scripts/eval_m4_stt.py --manifest /eval/manifest.csv --model /app/models/whisper_onnx"

# ① 격리 스레드 스윕 — 운영 AI 컨테이너를 잠시 멈춘다 (sensing·api·db는 유지)
docker compose stop ai-experts ai-qwen
for t in 1 2 4; do $E -e M4_ORT_THREADS=$t ai-experts $M --label base-fp32-direct28-t$t --limit 28; done
# ② 기본값 표본 200 (①에서 고른 스레드) — ids.txt를 이후 모델 비교에 재사용
$E -e M4_ORT_THREADS=2 ai-experts $M --label base-fp32-s200 --limit 200
docker compose up -d ai-experts ai-qwen
# ③ 부하 조건 — 운영 스택 + M5 반복 호출 중에 같은 28개
python3 scripts/bench_rpi5.py --label m4eval-load --duration 600 --threshold 0.0 &
$E -e M4_ORT_THREADS=2 ai-experts $M --label base-fp32-direct28-load --limit 28
# ④ 전체 2,398개 — ②의 파일당 시간으로 소요를 추정한 뒤 격리 상태에서 야간 실행 (중단 시 같은 명령으로 재개)
```

판단 기준: 파일당 지연 p95가 **Phase 2 대기 15초**(`TTS_WAIT_SEC`)와 실제 오디오 이벤트 간격 안에 들어오는지.
결과는 `reports/m4eval/<label>/summary.md`.

### 3-5. 측정 항목과 방법 (bench 도구 내부 동작)

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

### 3-6. 09-17 기본값 측정 결과 요약 (상세: `reports/rpi5-20260917/TEST_LOG.md`, 노트북·RPi5, Git 제외)

- 5개 모델 모두 동작 확인. M4가 "살려주세요 도와주세요" 정확히 전사.
- 기동 준비 약 92 s (M4 워밍업 약 50 s), 기동 중 메모리 7.4 GB·스왑 0.7 GB.
- 지연(RPi5): M1 단독 4.7 ms / 동시 10.4 ms, M2 0.1 ms, 음성 1건 M3 단독 6.3 s·M4 단독 11.1 s,
  M5 단독 약 9 s / 동시 23 s. 동시 실행 시 CPU 91%, 최고 71 °C, 스로틀링 없음.
- **M1 패킷 단위 추론이 300 pkt/s를 못 따라감** (단독에서도 backlog 건너뜀).
- **15:25~15:45 전역 OOM 22회** (ai-experts 21, ai-qwen 1). solo-m5·base-idle은 무효, solo-m1·solo-m4는 부분 오염,
  solo-m2·solo-m3만 온전. 데스크톱 GUI(`graphical.target`)가 켜져 있음.
- 사건 후 ai-experts는 수동 정지 상태(`docker start rp5-ai-experts`로 재개). 조율안 합의 후 재측정 예정.

### 3-7. 조정 대기 목록 (M1·M2 → M4 병합 측정이 끝난 뒤 한 번에 반영)

> **09-17 저녁 갱신:** 우선순위·근거·확인 방법을 정리한 최신본은 `handoff/rpi5-20260917/TUNING_PROPOSAL.md`.
> 아래 표는 초기 목록이며, A1(GUI 해제)은 반영 완료. M4 INT8 기본값 전환도 반영 완료.

09-17 기본값 결과는 부하·OOM 포함 그대로 확정. 아래는 **아직 반영하지 않은** 후보다. 병합 측정 중 새로 나오는 항목도 여기에 추가한다.

| 번호 | 항목 | 종류 | 근거 (09-17) |
|---|---|---|---|
| A1 | RPi5 데스크톱 GUI 해제 (콘솔 부팅) | 시스템 설정 — **사용자가 직접 실행** | OOM 촉발 `wf-panel-pi`, OS·데스크톱 약 1.2 GB |
| A2 | ai-qwen 프롬프트 캐시 256 → 64 MB (`QWEN_GGUF_CACHE_MB`) | 설정 | ai-qwen 2.0 → 3.3 GB 증가 후 OOM |
| A3 | 커널 메모리 cgroup 활성 + 컨테이너 메모리 상한 | 시스템 설정 + 재부팅 (선택) | 전역 OOM → 스왑 폭주 → sensing까지 정지 |
| B1 | **[기능 차단]** M1 추론 간격 `M1_INFER_STRIDE` (노드당 N프레임마다, 후보 10) | 코드 | M1 약 5 ms × 300 pkt/s → backlog 건너뜀 → 노드 버퍼 리셋 → 100프레임 창이 안 참 → **M1이 전부 0 입력만 추론** (기본값·병합 후 모두 점수 한 값 고정, TEST_LOG 12절) |
| C1 | 오디오 워커 순서 M3→M4 를 M4→M3 | 코드 | 음성 1건 M3 6.3 s + M4 11.1 s > Phase 2 15 s |
| C2 | M4 최대 생성 토큰 128 → 48 (env 노출) | 코드 | 환각 시 장시간 생성, 워밍업 약 50 s |
| D | 스레드 배분 A/B — 현행(M3 2·M4 2·M5 3) vs (M3 1·M4 2·M5 2) | 설정 | 동시 실행 CPU 91%, M5 9 s → 23 s |
| E1 | bench에 컨테이너 재시작 횟수·스왑 기록, 오염 회차 자동 표시 | 도구 | OOM 섞인 회차 수동 판별 |
| E2 | bench 회차 사이 M5 잔여 호출 소진 대기 | 도구 | check-all의 M5 호출이 solo-m1에 섞임 |
| F | 대시보드 "AI 준비 중" 표시 | UI (선택) | api 준비 후 약 70 s 동안 이전 위험도 표시 |
| G | ai-experts 이미지의 CUDA판 torch 제거 (CPU 인덱스 고정) | 빌드 | 이미지 8.8 GB, 빌드 약 40 분 (5절 9번) |
| H | `/audio/events` `trigger_ai=True`가 노드 CSI 스트림 오염·`csi:raw` 절단 | 코드 | 측정 도구는 우회 중 |

## 4. 다음 단계 — 팀원 모델 병합 (기본값 측정이 끝난 뒤)

원칙: 브랜치 병합 범위는 **팀 합의 완료**. 노진산이 브랜치를 하나씩 보며 진행할 때까지 대기한다.
**한 번에 하나씩** 병합 → 모델 교체 → 3-3·3-4와 같은 방법으로 측정 → 기본값과 비교.
`feature/*` 등 팀원 브랜치에는 커밋하지 않는다.

| 순서(제안) | 대상 | 원격 위치 | 확인할 것 |
|---|---|---|---|
| 1 | M4 이대경 — **09-17 병합 완료** (PR #4 → `8d9d4f8`), RPi5 기본값 INT8 전환(설정 보완 복사본), 결과는 `handoff/rpi5-20260917/TO_LEEDAEGYEONG_M4.md`. 이하 병합 전 확인 내용 — Whisper 파인튜닝 ONNX INT8. 브랜치는 `main`(`bfbd552`) 기반이지만 변경은 파일 추가 39개(`m4_whisper/`, `tests/`, 루트 README 3줄·`.gitignore` 3줄·`.gitattributes`)뿐, develop과 시험 병합 충돌 없음. 가중치는 GitHub 릴리즈 `m4-onnx-int8-20260916`의 `m4-onnx-finetuned-int8.zip`(510.4 MB, 그래프 합계 FP32 약 1,844 MB → INT8 약 716 MB). 인계 README: 기존 `m4_whisper_small.py`에 폴더만 바꾸면 반복 방지 wrapper를 거치지 않음, `confidence=null`을 응급지수에 그대로 넣지 말 것, 자체 wrapper는 Python 3.10·ORT 1.23.2 기준 | `origin/m4/lee-daegyeong-whisper-int8`, 태그 `m4-onnx-int8-20260916` | ONNX INT8은 **Optimum 3파일 형식이라 현재 `WhisperSmallModel` 경로와 호환** — 병합 후 3-4의 `--ids-file`로 같은 표본 비교. 인계 문서 기준 무음 환각 미해결, ONNX 전체 2,398 평가 미실행 |
| 2 | M1·M2 김태연 — **09-17 병합 완료** | PR #3 → `develop` `be5f00b` (`feature/M1-M2` 유지) | M1: 인계 모델 배치(노트북·RPi5), 조건부 시그모이드 삭제·임계값 `M1_FALL_THRESHOLD` 0.80 (**미커밋**), RPi5 `.env`의 `M1_MAX_NODES=1`·`M1_FALL_THRESHOLD=0.7` 삭제. 오프라인 확인 통과(슬롯 3·4 무영향 max|diff| 0). **실 파이프라인에서는 입력 창이 안 차서 전부 0 입력만 추론** → 3-7 B1로 일괄 조정 때 해결. M2: 파일만 병합, 배선 보류(인계 지시) |
| 3 | M3 소민섭 — v3.4 (09-13 배포 결정) | 아직 인계 브랜치 없음 (`origin/feature/ast-base`는 5월 것) | HF 포맷 + `preprocessor_config.json` + `id2label`. **log-Mel 전처리 구현**, **라벨은 이름으로 매핑**(인덱스 매핑 시 7종↔6종 불일치), 운영 임계값 0.6. 계획서의 오탐 4.03회/h는 09-11 held-out 3.97시간 기준(8/30의 1.51회/h는 평가 녹음 일부가 학습에 섞인 낙관 편향) |

병합 뒤 반드시: `emergency_score` 경계 테스트 회귀, 5노드/1노드 주입 시 expert 오류 0, 점수 범위 0~1.

---

## 5. 알려진 문제 · 결정 필요

1. **노드 1 간헐적 대량 손실 이력.** 09-17 오후 노드 1이 18~35% 손실, 최대 1초 공백(RSSI는 -26dBm로 양호).
   ESP 3대 전원 재시작 후 0.3~1.4%, 공백 10ms로 회복(노드 2·3은 2~4.5%, 노드 3 RSSI -35~-38dBm).
   `M1_MAX_NODES=1`이라 M1은 노드 1만 쓰므로, 노드 1이 다시 나빠지면 M1 버퍼가 초기화돼 추론이 끊긴다.
   재발하면 신호보다 전원·발열·펌웨어를 먼저 의심. 측정 중 손실은 `bench_rpi5.py` 결과의 노드 표로 기록한다.
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
