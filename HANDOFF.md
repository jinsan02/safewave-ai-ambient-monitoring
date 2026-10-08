# HANDOFF — SafeWave-AI (rp5)

> 갱신: **2026-10-08** (10-05 N_pose 보드 추론 실기 연동·3환경 검증·경보 경로 보완·RPi5 최신화, 10-06 M1 coverage 분석·앱 추가 요청 — 0·1·2·4-3·5·6절) / 2026-09-25 새벽 (09-24 M5 노트북 프로필·판정표 하한, M4 환각 거르기·긴급 음성 경보, 노트북 시작 스크립트 — 자세한 내용은 `handoff/laptop-20260923/`) / 2026-09-23 밤 (4-2 노트북 실험·M3 v34 병합 추가) / 본문 2026-09-17 밤 · 노진산(+Claude/Codex) · 다음 작업자(Claude·Codex)는 이 문서부터 읽는다.
> 규칙·제약: `AGENTS.md` / `CLAUDE.md` · 근거 현황: `docs/validation_status.md`
> 이전 판(09-17 작업 경과를 누적 기록한 487줄)은 Git 기록 `f022cf2:HANDOFF.md`에 있다.

## 0. 한눈에

### 10-08 기준 최신
- **N_pose 보드 추론 실기 연동 완료**(10-05): 노드 5대가 펌웨어 `rebuild/model` `5e0d838`(v1, 비HT 고정)로 노트북·RPi5 두 곳에 송신. RPi5는 5대 모두 50 Hz 수신. **노트북 수신은 22:11 이후 99% 실패**(보드 `errno=12`, ARP 추정 — `handoff/NPOSE_NODE_STATUS_20261005.md` 1절, 김태연 원문).
- **3환경 검증 완료**(10-05, 노트북 GPU·CPU·RPi5, 같은 평가 데이터): 정확도는 세 환경 같음(M3 낙상 46/50, M4 CER 8.7~9.4%, M5 위험 과소 0, 경계값 18/18), RPi5는 M3·M4 각 약 6 s로 느림 → 4-3절.
- **경보 경로 보완 3건 커밋·push**(`3552027`): 긴급 문장이면 M3 생략, 밀린 오디오 순서대로 처리, 오디오 결과 나이는 처리 완료 기준. RPi5 주입→경보 **16.8 s → 5.4 s**, 밀림 상황에서도 경보 확인. 단위 테스트 53개.
- **RPi5 최신화**: 스택은 `5ce24da` 이미지 + ai-experts만 `3552027` 덧씌우기 이미지(1-RPi5 표). 앱 기기 토큰을 RPi5로 옮김, 모니터는 RPi5에 연결.
- **M1(보드 판정)은 꺼 둠**(`sys:settings` m1=false). RPi5 5분 실측에서 판정 조건(coverage ≥ 0.9)을 만족하는 창이 노드별 1~14%뿐 → 기준 조정·펌웨어 확인 필요(6절, `reports/laptop/val_20261005/M1_COVERAGE_ANALYSIS_20261006.md`).
- **보호자 앱 추가 요청** 작성(`handoff/guardian_app_kit/TO_APP_20261006.md`, 미커밋): 서버 주소 RPi5로 변경, 확인 버튼(ack)이 한 번도 안 들어옴 → 앱 쪽 확인, 토큰 재등록·사유 표시·사건 묶기·감시 상태 표시.
- **다음 주**: 영상 녹화와 함께 빈 방 30분 + 모의 낙상으로 M1 판정 규칙(임계·K/N·coverage) 결정, "낙상+충격음" 보강 규칙 시간 맞춤.
- **노진산 개인 논문(DCS 추계, 마감 10-26)은 별도 폴더 `C:\dcs-fewshot-pareto`로 분리**했다(M5 few-shot 예시 수 실험). 이 저장소 운영 코드는 그 실험 때문에 고치지 않는다.
- 공유 자료: `reports/laptop/val_20261005/`(Git 제외) — `SUMMARY.md`, `WEEKLY_REPORT_20261005.md`, `viz/`, `safewave_val_20261005.zip`. Codex 인계 `handoff/CODEX_CONTINUE_20261005.md`(미커밋).

### 10-05 이전 기록
- **10-05 N_pose 연동 준비**(코드·단위 테스트·노트북 시뮬레이터 확인, 실제 펌웨어 미확인)
  - sensing이 CSI2·CSR! 수신. 보드 점수는 새 스트림 `m1:score`
  - `M1_SOURCE=board`면 노드별 K/N → 허브 판정. 노트북 `reports/laptop/compose.*.yml` 기본이 board
  - `M1_ALERT_MODE` 스위치(기본 standalone)
  - 시뮬레이터 `scripts/sim_esp32.py --format npose --hz 50 --fall-at 12`
  - 출력 기록기에 `m1_node_score.csv`
  - 제약: 판정은 `csi:raw` 수신으로 돌기 때문에, 보드가 CSI2를 끄고 CSR!만 보내면 갱신되지 않는다
- **10-05 보호자 앱**: 경보 확인 API(S2), Firebase 키 배치(노트북 `api/auth/`, Git 제외), 앱 토큰 등록·시험 푸시 성공. 전달 문서 `handoff/guardian_app_kit/TO_APP_20261005.md`

- **코드**: 통합 파트 리팩토링·M5 우회 규칙 경보·경보 누락 수정·M1 2차 답변 반영까지 `develop`에 있다.
  노트북에서 RPi5 유사 조건 전체 스택 테스트로 기능을 확인했다(수치는 보고 금지).
- **RPi5**: 아직 새 코드를 받지 않았다(`5b30f96`, 09-17 17:20 확인). ai-qwen은 정지 상태, 스왑 가득.
- **다음 할 일**: RPi5에서 pull·재빌드 → 기능 확인 → 전체 스택 1회 실측 (2절).
- **09-23**: M3 v34_homepos(소민섭) 병합, 노트북 CPU·GPU 효용 실험(4-2). RPi5에는 아직 미반영.
- **결정 대기**: `handoff/rpi5-20260917/OPEN_DECISIONS.md` (18건). 결정 전에는 구현하지 않는다.

## 1. 현재 상태

### 저장소

| 위치 | 상태 |
|---|---|
| GitHub `origin/develop` | `73dcecc`(10-05). 팀 병합 PR #3(M1·M2), PR #4(M4) 포함. `feature/M1-M2`, `m4/lee-daegyeong-whisper-int8` 브랜치 유지 |
| 노트북 `C:\rp5` | `develop` = 원격 + 미커밋 문서(M1 분석 문서·스크립트는 **로컬 전용으로 두기로 함**: `handoff/M1_*_20260928.md`, `M1_EVENT_TEST_PROTOCOL_20261005.md`, `scripts/dev/m1_event_test.py`, `m1_false_alarm_analysis.py`). 스택은 GPU(`reports/laptop/compose.gpu.yml`, `M1_SOURCE=board`) |
| RPi5 `~/safewave` | **`5ce24da`**(10-05). 미추적 파일: `scripts/dev/m3_env_eval.py`·`push_e2e.py`(수동 복사, 내용은 `73dcecc`와 같음 → **pull 전에 옮기거나 지워야 pull이 됨**), `.env.bak-20261005`. 이전 stash 2개는 그대로 |

### RPi5 (마지막 확인 10-05 23:05, 10-06부터 ssh 접속 안 됨 — 꺼졌거나 IP 변경 추정)

| 항목 | 10-05 상태 |
|---|---|
| 이미지 | `safewave-*`(10-05 22:15 `5ce24da`로 재빌드). **ai-experts만 `~/overlay_20261005` 덧씌우기 이미지(`3552027` 코드)**, 원본은 `safewave-ai-experts:pre_audiofix_20261005` |
| 설정 | `M1_SOURCE=board`, `M1_ALERT_MODE=standalone`, `VOICE_ENABLED=false`, M3 FP32 `v34_homepos.onnx`(설치됨), M4 `whisper_onnx_int8_ft_svc`, M5 Qwen2.5-1.5B Q5(2스레드) / `sys:settings` m1=false·m2=false·m3·m4·m5=true |
| 앱 | `api/auth/firebase_key.json` 배치(root 소유, 600), `fcm_ready=true`. 앱 기기 1대 토큰을 노트북에서 옮겨 등록(10-05 22:27, API가 TTL 연장) |
| 자원 | 전체 스택 + M5 적재 시 사용 5.1 GiB, 여유 2.7 GiB, **M5 평가 뒤 스왑 1.9/2.0 GiB**, 최고 64 °C, 스로틀링 없음 |
| 노드 | N_pose 5대(IP·MAC 표: `handoff/NPOSE_NODE_STATUS_20261005.md` 2절), 새 프레임 비율 76~84%, 노드 5는 간헐 멈춤 |

### RPi5 (09-17 17:20 기록 — 하드웨어·접속은 지금도 유효)

| 항목 | 값 |
|---|---|
| 접속 | `ssh csi@192.168.1.2` (노트북 `id_ed25519` 등록). DHCP라 IP가 바뀔 수 있음. 노트북 `~/.ssh/config`의 옛 항목(`192.168.0.13`, `rp5`)은 무효 |
| 하드웨어·OS | RPi5 8GB 4코어, NVMe 234GB(사용 33%), Debian 13, kernel 6.12.75+rpt-rpi-2712, Docker 29.4.3 / Compose v5.1.3 |
| 부팅 | 콘솔(`multi-user.target`, GUI 해제). 되돌리기 `sudo systemctl set-default graphical.target` |
| 컨테이너 | ai-qwen **정지**(M2 스텁이 유휴 중 11초마다 M5를 불러 RSS 5.2GB → OOM 반복). 나머지 실행. 스왑 2,047MB(가득) |
| `.env` | `AI_DOCKER_TARGET=cpu-runtime`, `M4_KO_STT_MODEL=whisper_onnx_int8_ft_svc`. 백업 `~/backup/.env.bak-20260917*` |
| cgroup | memory controller 비활성 → Compose `mem_limit`이 적용되지 않고 `docker stats` 메모리는 0B |
| ESP32 | 노드 1·2·3, 합계 약 300 pkt/s. 노드 1 간헐 대량 손실 이력(전원 재시작 후 회복) |
| 기타 | 다른 프로젝트 이미지 약 15GB(소유자 확인 전 삭제 금지) |

### 모델 (`volumes/models/`, Git 미포함)

| 모델 | 폴더 | 비고 |
|---|---|---|
| M1 (보드) | 각 노드 펌웨어 안 | **10-05부터 운영 경로.** N_pose v1(WiFall 사전학습 s1, int8, 보드 추론 14 ms). 허브는 `m1:score`로 판정만(`M1_SOURCE=board`) |
| M1 (허브 ONNX, 예전 경로) | `m1_wifi_pose_onnx/` | `M1_SOURCE=onnx`일 때만. 김태연 인계(3노드 학습 → 입력 `(1,5,64,100)`, 출력 `fall_score`, 그래프 내 sigmoid, 임계 0.80). 이전 1노드 모델 `..._1node_20260802/`(RPi5), `..._stub_backup_20260917/`(노트북) |
| M2 | `m2_frenel_vital_onnx/` | **폐기**(팀 결정). 미학습 스텁(심박 118 고정), 기본 off, 다시 켜지 않음 |
| M3 | `ast_onnx/v34_homepos.onnx` | 소민섭 v34 6-class(전처리 그래프 내장, sha256 `d06265e9…b615`). 받기·검증 `scripts/setup_m3_ast_onnx.py`. 옛 모델 백업은 09-24 폴더 정리 때 휴지통으로 이동. RPi5 설치 완료(10-05, FP32 — INT8 아님) |
| M4 | `whisper_onnx_int8_ft_svc/` (기본) | 이대경 INT8의 서비스용 복사본(`generation_config.json`만 교체, 가중치 하드링크). 원본 `whisper_onnx_int8_ft/`, 이전 fp32 `whisper_onnx/` |
| M5 | `qwen_15b_gguf_q5/` + `qwen_15b/` | GGUF Q5_K_M 1.23GB + 토크나이저 |
| 평가 데이터 | `data/m4_eval_2398/` | 이대경 M4 평가 세트 2,398개(노트북·RPi5). **공개 저장소 업로드 금지** |

## 2. 다음 할 일 (10-08 기준)

### 2-0. 우선순위
1. **RPi5 접속 복구** → `git stash`·미추적 파일 정리(1절 저장소 표) → `git pull --ff-only origin develop`(`73dcecc`) → `docker compose build ai-experts && docker compose up -d` → 덧씌우기 이미지 대신 정식 이미지로 동작 확인(`docker exec rp5-ai-experts grep -c audio_event_plan /app/main.py`가 2), 확인 후 `pre_audiofix_20261005` 태그·`~/overlay_20261005`는 사용자 확인 받고 정리
2. **노트북 수신 복구**: 노트북에서 `ping 192.168.1.20`~`.24`, 무선 절전 끄기, 방화벽 UDP 5005 확인 → 새 프레임 비율이 오르는지 5분 측정(`scripts/dev/npose_quality.py --seconds 300`). 송신 실패가 coverage를 깎는지도 이 측정으로 확인
3. **M1 판정 규칙 결정(다음 주, 영상 녹화 동반)**: 빈 방 30분 이상 → 규칙 후보별 시간당 헛경보, 모의 낙상 → 감지율. 후보: 임계 1회 ≥ 0.9 또는 5회 중 2회 ≥ 0.8, **coverage 0.9 / 0.8 / 0.7**. 바꿀 때는 `M1_BOARD_*` 환경변수만(재빌드 불필요). 절차 `handoff/M1_EVENT_TEST_PROTOCOL_20261005.md`(로컬), 근거 `handoff/M1_WIFALL_VAL_20261005.md`
4. **"낙상+충격음"·"긴급어+낙상 의심" 보강 규칙 시간 맞춤**: 보드 점수는 순간값인데 소리 결과는 1~20 s 늦게 와서 같은 틱에 겹치지 않음 → 노드별 최근 점수 보관 후 소리 시각 기준 판정(`ai/logic/emergency_score.py`, `ai/runtime_inputs.py`). 지금 `M1_ALERT_MODE=corroborated`는 사실상 off와 같다
5. **앱**: `TO_APP_20261006.md` 전달·커밋 여부 결정, 앱 확인(ack) 종단 시험. 서버 쪽 S11(감지 가능 노드 수)·S12(시험 경보 표시)·S13(heartbeat 켜기)은 결정 후 구현
6. 김태연 전달: 노드 5 멈춤(2차 펌웨어 뒤에도 5분 중 약 90초), 노드 2 coverage 저하, 새 프레임 비율 개선 가능성

### 09-17 계획 (2-1~2-5, 대부분 10-05에 수행 — 참고용)

### 2-1. 준비 (사람)
- 스왑 비우기: `sudo swapoff -a && sudo swapon -a` 또는 재부팅
- memory cgroup 활성화는 선택(`cmdline.txt`에 `cgroup_enable=memory`, 재부팅). 이번 측정은 비활성으로 진행하고 PSI·RSS로 본다

### 2-2. 코드 반영
```bash
cd ~/safewave && git stash list && git pull --ff-only origin develop && git log --oneline -1
docker compose build && docker compose up -d && docker compose ps
```
- CPU 전용 torch로 바뀌어 첫 빌드가 이전(약 40분)보다 짧아야 한다. `nvidia-` 패키지가 있으면 빌드가 실패한다(의도).
- 확인: `.env`의 M4 경로, `VOICE_ENABLED`(음성 서비스를 안 띄우면 false 유지), `GET /settings`의 `models.m2=false`
  (Redis에 옛 설정이 남아 있으면 true일 수 있음), API 로그 `startup_completed`의 `fcm_ready`(키 없으면 `fcm_unavailable`)

### 2-3. 기능 확인 (짧게)
```bash
python3 scripts/alert_e2e_check.py                    # ai-qwen 켠 상태
docker compose stop ai-qwen && python3 scripts/alert_e2e_check.py --node 8 --rule-wait 90; docker compose start ai-qwen
docker logs -f rp5-ai-experts | grep m1_gate_stats     # 60초마다
```
- `m1_gate_stats`: `inferred/expected`, `zero_filled_frames`, `slot_collisions`, `skipped_ticks`, `tail_miss_checks`
  - 노트북 시뮬레이터에서는 0 채움 1,797/분·슬롯 충돌 1,080/분(Windows sleep 해상도로 몰아 보낸 영향 추정). **실제 ESP 값이 핵심**
  - 창끝 허용치 `M1_TAIL_MAX_AGE_MS` 15(기본)와 10을 각각 몇 분씩 비교(김태연 요청)
- 빈 방에서 낙상 규칙 경보(`rule_alert_written`)가 나오는지 기록 — 합성 입력에서는 3/5 양성이 나왔다

### 2-4. 전체 스택 1회 실측
- `scripts/bench_rpi5.py` — 기록 항목: 메모리 최고·스왑, CPU·메모리 PSI, 온도·스로틀, 음성 이벤트→결과(15초 기준),
  M5 `qwen_infer_ms` p50/p95, M1 게이트 통과율, Redis 사용량(256MB 예산), 재시작·OOM
- 여유가 있으면 M5 캐시 `QWEN_GGUF_CACHE_MB` 0/64 각 20회 비교(RSS 증가 원인 확인)
- 합격선·예상 범위: `handoff/rpi5-20260917/RESOURCE_BUDGET.md`
- M3/M4 지연은 `audio:result` 스트림 ID 시각 − payload `ts_ms`로 잰다(`expert_latency_ms` 사용 금지)
- M5 p95가 30초를 넘으면 M5만 3 threads로 되돌려 비교

### 2-5. 마무리
- 결과를 노트북 `reports/`로 동기화(연결이 끊기면 사람이 SSH로 확인할 수 없음)
- 끊기 전 상태 정리(서비스·설정 원복·메모리)를 확인하고 "끊어도 된다"고 알린다
- 결과를 `docs/validation_status.md`와 담당자 문서에 반영

## 3. 지금 코드의 동작 (09-17에 바뀐 핵심)

| 영역 | 동작 | 설정 |
|---|---|---|
| M1 입력 | 수신 시각(stream id) ±5ms 100Hz **전역 격자**, 빠진 슬롯 0, 같은 슬롯은 최신 프레임으로 교체 | `M1_FRAME_INTERVAL_MS=10` |
| M1 추론 | 전역 200ms tick. 필수 노드 창이 차고 창끝 프레임이 허용치 안일 때만 추론. 같은 tick 안에서는 다음 패킷을 기다림 | `M1_INFER_INTERVAL_MS=200`, `M1_REQUIRED_NODES=1,2,3`, `M1_TAIL_MAX_AGE_MS=15`(0이면 게이트 끔) |
| M1 판정 | K=3/N=5. **추론 없이 끝난 tick은 0표**. 단일 창 양성은 경보 floor를 만들지 않음. raw 점수는 M5 문맥에 보존 | `M1_AGGREGATION_K/N` |
| 규칙 1차 경보 | 낙상 3/5 확정·낙상+위험음·생체신호 위기면 ai-experts가 **M5 없이** `ai:emergency`에 critical(`slm_mode=rule`). 낙상은 **전역 1건**/90초, 생체신호는 노드별. 경보 후 낙상 쿨다운 동안 M5 요청 생략 | `RULE_ALERT_ENABLED`, `RULE_ALERT_COOLDOWN_MS=90000` |
| M5 요청 | 임계 이상, 전역 5초 간격, `phase2:active` 노드는 요청하지 않음(간격도 소모 안 함). ai-qwen은 30초 이내 최고 위험·최신 1건 | `SLM_MIN_INTERVAL_MS` |
| 알림 | critical이면 **즉시 FCM**(본문에 노드·사유). `VOICE_ENABLED=true`일 때만 TTS·음성 확인, "괜찮다"면 별도 일반 알림. 도움 요청·거동 불가 표현은 응급 유지 | `VOICE_ENABLED=false`, `VOICE_NODE_ID=1` |
| 락 | `phase2:active:{node}` 90초 — API·규칙 경보·M5 공통. 같은 노드 재경보 억제(정책상 유지) | `PHASE2_LOCK_SEC` |
| M1 보드 모드(10-05) | `M1_SOURCE=board`: sensing이 CSR! → `m1:score`(MAXLEN 18000, EXPIRE 3600). ai-experts가 노드별 최근 5회 중 3회 ≥ 0.8(창이 찼고 coverage ≥ 0.9, 무작위 모델 제외) → 경보 노드 수 ≥ 1이면 판정. **판정 불가 창은 반대표로 들어간다** | `M1_BOARD_THRESHOLD/K/N/MIN_NODES/MIN_COVERAGE`, `M1_ALERT_MODE` |
| 오디오 워커(10-05 `3552027`) | M4 → M3 순서. **긴급 문장이면 M3 생략**하고 바로 결과. 밀린 이벤트는 건너뛰지 않고 순서대로 처리하되 뒤에 밀린 게 있으면 M3 생략, 60초 넘게 밀린 것만 버림. 결과 유효시간은 **처리 완료 시각부터** 30초 | `AUDIO_BACKLOG_MAX_WAIT_MS=60000`, `AUDIO_RESULT_MAX_AGE_MS=30000` |
| 경보 확인 S2(10-05 `6b0ab33`) | `POST /alerts/{msg_id}/ack`(seen/called) → `alert:ack:{msg_id}` Hash(TTL 3600), 다른 기기에 `type=ack` 푸시, `GET /alerts/{msg_id}`에 `acked_by`·`voice_ok` | — |
| 복구 | API는 재시작 시 최근 30초 경보 재생(중복은 `notify:sent`), 깨진 항목은 건너뜀. CSI 루프는 같은 위치 3회 실패 시 최신으로 이동. 오디오 워커가 180초 멈추면 ai-experts 종료 후 재시작 | `ALERT_REPLAY_MS`, `AUDIO_STALL_EXIT_SEC` |
| TTL | 모든 키 ≤3600s. FCM 토큰·설정은 API가 10분마다 연장 | `TTL_REFRESH_SEC=600` |
| Redis | 512MB/noeviction, 포트는 `127.0.0.1`만. 스트림 MAXLEN: CSI 36,000, 결과 18,000, 오디오 120 | `db/redis.conf` |
| 자원 | 기본 Core 5개 서비스(audio/voice/integration/home은 profile). 스레드 M3 1·M4 2·M5 2, BLAS 1, M5 캐시 64MB. ai-experts는 CPU 전용 torch | `docker-compose.yml` |
| 관측 | 60초 `m1_gate_stats`, 대시보드에 노드·규칙 경보·M1 투표 표시, 웹소켓은 노드별 최신·위험 변화 즉시 전송, 대화 내용은 로그에 남기지 않음 | — |

테스트: `python -m unittest tests.test_m5_pipeline` (**53개**, 10-05 기준. 무거운 서비스 모듈은 AST로 함수만 추출).

## 4. 도구

| 도구 | 용도 |
|---|---|
| `scripts/bench_rpi5.py` | RPi5 전체 스택 측정(PSI·재시작·스왑·노드 손실, 설정 자동 원복) |
| `scripts/bench_startup.py` | 기동 준비 시간·기동 중 메모리 |
| `scripts/eval_m4_stt.py` | M4 CER/WER/키워드/지연(평가 세트) |
| `scripts/sim_esp32.py` | 3노드 100Hz UDP(788B) + 음성 주입 부하 시뮬레이터(기능 확인용) |
| `scripts/alert_e2e_check.py` | 경보 경로(락·발송 시도)와 규칙 경보 확인 |
| `scripts/dev/push_e2e.py` (10-05) | 앱 푸시 종단 시험: "도와주세요" 주입 → 규칙 경보 → FCM 발송 시각 → 앱 확인(ack) 대기 |
| `scripts/dev/m3_env_eval.py` (10-05) | M3 낙상 50 + 오탐 16 + 생활 소음(VAD 이벤트) 정확도·지연 묶음 |
| `scripts/dev/npose_quality.py` | N_pose 노드 품질(50 Hz, 새 프레임·coverage·점수)과 허브 규칙 후보 8개의 시간당 경보, 원점수 `*_rows.npz` |
| `scripts/dev/validation_recorder.py` | 모델별 출력 CSV 기록(`m1_node_score.csv` 등), 영상 시험용 |
| `reports/laptop/val_20261005/run_*.ps1·sh`, `make_viz.py`, `m1_live_analysis.py` (Git 제외) | 3환경 검증 실행·시각화·M1 실측 분석 |
| `scripts/dev/start_docker_desktop.ps1 -Restart` | 노트북 Docker Desktop 안전 시작(남은 소켓 폴더 비켜두기, 자동 업데이트 끄기) |
| `reports/laptop/start_laptop.ps1` / `stop_laptop.ps1` (Git 제외) | 노트북 한 번에 켜기·끄기: Docker Desktop → 스택(기본 GPU, `-Mode cpu`) → 마이크·TTS 호스트 창(`start_voice_host.ps1`, `-NoVoice`로 생략) → 모니터(127.0.0.1:8081). 음성 확인(`VOICE_ENABLED=true`)은 마이크·TTS 창이 떠 있어야 동작 |
| `reports/laptop/compose.laptop.yml` (Git 제외) | 노트북 RPi5 유사 CPU 한도 override. 기록 `reports/laptop/LAPTOP_TEST_LOG.md` |
| 대시보드 | 노트북 `python -m http.server 8081` → `http://localhost:8081/monitor.html?api=http://192.168.1.2:8000` (노트북 스택은 `api=http://localhost:8000`). 자원 패널 제목의 주소로 어느 장비 값인지 확인 |

## 4-1. 보호자 앱 연동 (09-23)

개발 키트: `handoff/guardian_app_kit/` (README, 명세 7종, 프롬프트, 목 데이터). Git 추적(`handoff/`), 바탕화면 zip으로도 전달.

| 서버 변경 | 내용 | 설정 |
|---|---|---|
| 푸시 형식 | `FCM_DATA_ONLY=true`면 data 전용 high priority(제목·본문도 data). false(기본)면 기존 시스템 알림. **앱 배포와 같은 날 true로 전환** — 시스템 알림 형식은 앱이 꺼져 있으면 앱 코드가 실행되지 않아 전체 화면 경보가 불가 | `FCM_DATA_ONLY` |
| 푸시 필드 | 응급 `type=emergency`, `msg_id`(= `ai:emergency` 스트림 ID), `slm_mode` / 응답 확인 `voice_ok` + `msg_id` / `heartbeat` | — |
| `GET /app/summary` | 홈 요약(위험도, 데이터 지연, 설치 센서 온라인 수, 서버 상태, FCM 준비, 최근 경보). 전사·생체신호 없음 | `APP_EXPECTED_NODES=1,2,3` |
| `POST /alerts/{msg_id}/feedback` | 오탐·미탐 신고 → 기존 `mqtt:feedback:last`(TTL 3600) → **1시간 동안 M5 점수 ±0.08**. 규칙 경보는 영향 없음 | — |
| `DELETE /auth/register-token/{id}` | 토큰 삭제 | — |
| `GET /history?before=` | 페이지 나누기, 200건씩 필요한 만큼만 읽음 | — |
| 정상 동작 신호 | `HEARTBEAT_INTERVAL_SEC`마다 heartbeat 푸시(기본 0=끔, 권장 86400) | `HEARTBEAT_INTERVAL_SEC` |

확인: 노트북 Docker에서 API 재빌드 후 새 엔드포인트 실제 호출, 단위 테스트 3개 추가. FCM 실제 발송은 Firebase 키가 없어 미확인.
iOS용 APNs 설정은 발송 코드에서 뺐다(대상 기기 Android).
결정 대기: S2 확인 기능(새 키 `alert:ack:{msg_id}`), S6 인증, 원격 접속 방식(키트 `docs/05_OPEN_DECISIONS.md`).

## 4-2. 노트북 CPU·GPU 효용 실험 (09-23, 교수님 요청)

기록: `reports/laptop/EXPERIMENT_CPU_GPU.md`(Git 제외). 실제 ESP32 4노드 → 노트북 192.168.1.11(ipTIME 고정 할당). 노트북 수치는 RPi5 측정표에 쓰지 않는다.

| | CPU(RPi5 스레드) | CPU(풀컨디션) | GPU RTX 5060 |
|---|---|---|---|
| M3 v34 / M4 단독 | 2.15 s / 1.00 s | 0.61 s / 0.65 s | 0.04 s / 0.47 s |
| 음성 이벤트→결과 | 2.98 s | 1.28 s | 0.75 s |
| M5 추론 p50 / p95 | 1.6 s / 20 s | — | 0.75 s / 1.05 s |
| M4 정확도(200) | CER 5.15%, 키워드 90% | — | 같음 |
| M5 정확도(100) | exact 51%, adjacent 82%, safe fail 0 | — | exact 53%, adjacent 82% |
| 경계값 / Phase 2 | 18/18 / 3/3 | — | 18/18 / 3/3 |

발견 (RPi5에도 해당)
- **빈 방 M1 오경보**: 실제 ESP32 데이터에서 낙상 점수 0.5~0.8, 규칙 경보 약 90초마다. 규칙 경보 뒤 90초 M5 억제가 겹쳐 M5가 사실상 호출되지 않음 → 김태연 공유 필요.
- **M5 prefill이 전 코어 사용**: `n_threads_batch` 미지정 → llama-cpp-python 기본값(전 코어). RPi5에서도 M5 실행 중 4코어 점유. 수정 결정 대기.
- **M2 off일 때 상태 문장 `심박:0` → M5가 심정지로 판단(critical)** → "미측정"으로 수정(이번 커밋).
- M2 스텁 on → 전 노드 HR 118 → M5 critical 연속·Phase 2 적체·M3 생략. M2는 계속 off.
- ESP32 패킷이 몰려 도착(약 8%가 1 ms 이내) → M1 격자 0 채움 13% 이상. RPi5 실측 비교 필요.
- 노트북 USB 마이크에서 M4 환각("MBC 뉴스 ○○○입니다", "너 죽어") → 노트북 VAD -35 dB/500 ms로 보정. 환각 필터는 보류(이대경 공유).

노트북 GPU 실행 경로(RPi5 무관): `ai/Dockerfile` gpu-runtime(CUDA 12.8, ORT GPU 1.22, torch cu128), gguf-gpu-runtime(llama.cpp CUDA sm_120).
설정 `reports/laptop/compose.{cpu,gpu}.yml`, GPU M5는 `USE_TORCH=0` 필요(NCCL 심볼 충돌).
도구: `scripts/bench_models.py`, `scripts/dev/{check_esp32_rx,csi_excel_logger,voice_probe,slm_probe,phase2_test,boundary_check}.py`.
담당자별 공유 문서·팀원별 할 일: `handoff/laptop-20260923/README.md`. 빈 방 113분 기록: M1 경보 65건, 오류 0.
M3 평가셋(`data/m3_eval_v34/`)으로 낙상 50회 재현 46/50(담당자 보고값과 일치).

## 4-3. 3환경 검증 (10-05, 같은 평가 데이터)

자료: `reports/laptop/val_20261005/`(Git 제외) — `SUMMARY.md`, `WEEKLY_REPORT_20261005.md`, `viz/1~9`, `safewave_val_20261005.zip`(팀 공유본). RPi5 값만 RPi5 측정표에 쓴다.

| 지표 | 노트북 GPU | 노트북 CPU | RPi5 |
|---|---:|---:|---:|
| M3 낙상 / 오탐 사례 경보 | 46/50 · 5/16 | 46/50 · 5/16 | 46/50 · 5/16 |
| M3 추론 p50 | 41 ms | 839 ms | 6.0 s |
| M4 CER · 키워드(같은 50건) | 9.4% · 84% | 9.4% · 84% | 8.7% · 84% |
| M4 지연 p50 | 0.64 s | 1.0 s | 5.9 s |
| M5 정확 / 위험 과소(30건, rpi5 지시문, M2 꺼짐) | 18 / 0 | 15 / 0 | 16 / 0 |
| M5 추론 p50 | 1.2 s | 2.4 s | 10.8 s |
| 전체 흐름 M3+M4 p95 | 1.5 s | 2.5 s | 25.4 s |
| 경계값 | 18/18 | 18/18 | 18/18 |
| 앱 푸시 주입→발송 | 0.68 s | — | 16.9 s → **보완 후 5.5 s** |

- M3 놓침 4건은 모두 `화장실_문닫힘`. CPU 전체 흐름 측정은 노트북 수신 실패로 노드 일부만 받은 상태.
- RPi5 M1 5분 실측(22:51~): 판정 13,656건 모두 normal, 경보 0. coverage 평균 노드 1·3·4 0.80~0.83(≥0.9 통과 5~14%), 노드 2·5는 무너지는 구간(0.45 / 0) 겹침. 분석 `M1_COVERAGE_ANALYSIS_20261006.md`.

## 5. 팀 연계

| 담당 | 상태 | 문서 |
|---|---|---|
| 김태연 N_pose(M1 보드 추론) | **10-05 인계 문서 `handoff/NPOSE_NODE_STATUS_20261005.md`**(펌웨어 `5e0d838`, 노드 IP·MAC, 노드 5 원인 답변). 노트북 수신 실패·coverage·노드 5 멈춤 확인 요청 중. v2 학습(우리 방 데이터)은 수집 후 | `handoff/M1_WIFALL_VAL_20261005.md` |
| 김태연 M1·M2 (09월) | 2차 답변 반영 완료. 회신 초안(슬롯 충돌 처리·격자 원점 질문) **미발송**. 5노드 2차 수집 예정(`M1_REQUIRED_NODES`만 변경), 다음 모델은 창끝 게이트 불필요 가능. CSI2 792B 진단 포맷은 펌웨어 공유 후, 10/05 종료 | `handoff/rpi5-20260917/TO_KIMTAEYEON_M1_M2.md`, `REPLY_TO_KIMTAEYEON_M1.md`, `REPLY2_TO_KIMTAEYEON_M1.md` |
| 이대경 M4 | INT8 기본값. `lang_to_id` 포함 설정·짧은 발화 설정·오인식 개선 요청 중 | `handoff/rpi5-20260917/TO_LEEDAEGYEONG_M4.md` |
| 소민섭 M3 | v34 병합 완료(09-23). 브랜치의 VAD -55 dB는 보류(-45 유지), 오래된 torch·transformers 고정 제외 | `docs/m3-env-sound-onnx.md` |
| 보호자 앱 | 개발 키트 전달(Android). 서버 S0~S5·S7·S9 구현, **S2(확인) 10-05 구현**, Firebase 연동·실기기 푸시 성공. 10-05 전달 `TO_APP_20261005.md`(이대경 앞), 10-06 추가 요청 `TO_APP_20261006.md`(미커밋) | `handoff/guardian_app_kit/` |

팀원 브랜치(`feature/*` 등)에는 커밋하지 않는다. 병합은 한 번에 하나씩, 병합 후 같은 방법으로 측정한다.

## 6. 알려진 문제 (결정 대기 외)

1. M5 RSS 증가 원인 미확정(64MB 캐시는 완화안) — 2-4에서 캐시 0/64 비교
2. Redis 512MB 도달 시 ai-experts·M5·API 쓰기 실패 대응이 sensing만큼 정교하지 않음(정상 예산 256MB)
3. ai-experts 준비 신호 없음(API·M5는 `service_started`만 기다림), 대시보드 "AI 준비 중" 표시 없음
4. 재연결 시 이전 Redis 클라이언트 미종료(기능 영향 작음, 실측 후 판단)
5. ~~M3 입력이 log-Mel이 아님~~ → v34 병합으로 해결(전처리 그래프 내장)
6. M4 반복 방지 wrapper 미통합
7. 노트북 Docker 소켓 파일 접근 불가(Win32 1920) 근본 원인 미확인 — 관리자 `fltmc filters`
8. 대시보드 좁은 화면에서 카드·범례 잘림
9. **(10-05) M1 판정 가능 창 부족** — coverage ≥ 0.9 기준이 우리 방 노드(새 프레임 약 80%)와 맞지 않아 M1을 켜도 경보가 거의 안 남. 다음 주 실측으로 기준 결정
10. **(10-05) 보드 모드 보강 규칙 시간 어긋남** — 2-0의 4번
11. **(10-05) RPi5 오디오 처리 느림** — M3 FP32 6 s + M4 6 s. 긴급 음성은 보완으로 5.4 s, 일반 소리 결과는 여전히 12~25 s. M3 INT8 검토 여지(사용자 결정으로 FP32 유지 중)
12. **(10-05) 노드 상태 표시** — 멈춘 보드(같은 프레임 반복)도 `status=online`으로 셈. 앱·대시보드에 '감지 가능 노드 수' 필요(TO_APP_20261006 S11)
13. **(10-05) 노트북 수신 실패** — 2-0의 2번

## 7. 참고

- 일정: 개발계획서 7장 (9/21 실기기 성능 측정 → 9/28 M3 통합 → 9/30 M4 인계 → 10/12 M1 학습 → 10/19 통합 → 10/26 실환경 검증 → 10/31 실험 종료, 11월 발표)
- 09-17 측정·조정 문서: `handoff/rpi5-20260917/` (README, TUNING_PROPOSAL, RESOURCE_BUDGET, DATA_DRIVEN_OPTIMIZATION, OPEN_DECISIONS)
- 09-17 RPi5 원자료: 노트북·RPi5 `reports/rpi5-20260917/TEST_LOG.md` (Git 제외)
- 펌웨어 wire contract: `CLAUDE.md` 하단 / 측정 양식 `docs/benchmark_template.md`
- 노트북 `C:\rp5\.claude\worktrees\`에는 예전 작업 트리 사본이 있어 전체 검색 때 제외한다
