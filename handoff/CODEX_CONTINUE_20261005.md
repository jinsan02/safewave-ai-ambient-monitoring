# Codex 이어받기 — 10-05 3환경 성능 검증(노트북 GPU · 노트북 CPU · RPi5) 마무리

작성: 2026-10-05 22:30, Claude 세션에서 넘김. **이 문서와 `CLAUDE.md`·`AGENTS.md`·`HANDOFF.md`를 먼저 읽는다.**
사용자: 노진산(통합·M5). 한국어로, 짧게, 확인한 것과 추정한 것을 나눠서 보고한다.

## 0. 지켜야 할 것 (어기면 안 됨)
- 커밋·push는 **사용자가 요청할 때만**. 브랜치 `develop`. `feature/*` 커밋 금지. 커밋 메시지에 `Co-Authored-By` 넣지 않음.
- **git에 올리지 않는 것**: `handoff/M1_*_20260928.md`, `handoff/M1_EVENT_TEST_PROTOCOL_20261005.md`, `scripts/dev/m1_event_test.py`, `scripts/dev/m1_false_alarm_analysis.py`, `reports/**`, 이 문서, `handoff/NPOSE_NODE_STATUS_20261005.md`(김태연 원문).
- 아직 커밋 여부를 묻지 않은 새 스크립트: `scripts/dev/m3_env_eval.py`, `scripts/dev/push_e2e.py` → 사용자에게 물어볼 것.
- `api/auth/firebase_key.json`은 절대 git·문서·채팅에 내용 노출 금지(이미 메신저로 오갔으므로 실사용 전 키 교체 권고).
- 새 Redis 키는 사용자 확인 후, TTL ≤ 3600. 비밀번호 입력·sudo는 사용자 몫. 영구 삭제 금지.
- RPi5 측정표(`docs/benchmark_template.md`)에는 RPi5에서 잰 값만.
- 실행 중 컨테이너 안에서 무거운 모델 `docker exec` 금지 → `docker run --rm` 별도 컨테이너로.

## 1. 지금 상태 (22:30)
| 환경 | 상태 | 결과 폴더 |
|---|---|---|
| 노트북 GPU | 완료 (21:33~22:08) | `reports/laptop/val_20261005/laptop_gpu/` |
| 노트북 CPU 짧은 판 | 완료 (22:08~22:21), 스택은 GPU로 복귀됨 | `reports/laptop/val_20261005/laptop_cpu/` |
| RPi5 짧은 판 | **실행 중** (22:22 시작, 약 15분 → 22:37 전후 끝) | RPi5 `~/safewave/reports/rpi5-20261005/val/` |

- RPi5 접속: `ssh csi@192.168.1.2` (노트북 키 등록됨). 저장소 `~/safewave`. 컨테이너 이름은 `rp5-*`, 이미지는 `safewave-*`.
- 진행 확인: `ssh csi@192.168.1.2 'cat ~/safewave/reports/rpi5-20261005/val/suite.log ~/safewave/reports/rpi5-20261005/val/suite_extra.log'` → 둘 다 마지막 줄 `== done`이면 끝.
- 본 묶음 3단계(M3)는 `m3_env_eval.py`가 RPi5에 없어 1초 만에 실패했다 → 스크립트를 scp로 복사하고 **보충 묶음 `run_rpi5_extra.sh`**(본 묶음이 끝나길 기다렸다가 M3 낙상50+오탐16 → 앱 푸시 종단 시험)를 22:28에 걸어 둠. 결과는 같은 `val/` 폴더(`m3_env.json`, `push_e2e.json`).
- **앱 푸시는 이제 RPi5 기준.** 노트북 Redis에 있던 앱 기기 토큰(1대)을 RPi5 API `POST /auth/register-token`으로 옮겼다(22:27, TTL 3600 — API가 주기적으로 갱신). 앱 쪽 서버 주소도 RPi5(`http://192.168.1.2:8000`)로 바꾸도록 앱 담당에게 요청해야 한다.
- 스크립트: `reports/laptop/val_20261005/run_rpi5.sh` (RPi5에도 같은 경로로 있음). 단계: env → N_pose 노드 품질 60초 → 전체 흐름 180초 → M3 낙상50+오탐16 → M4 50건 → M5 30건(rpi5 지시문, M2 off) → 경계값.
- 모니터: http://localhost:8081/monitor.html?api=http://192.168.1.2:8000 (지금 RPi5에 연결됨). 노트북으로 되돌리려면 `api=http://localhost:8000`.
- RPi5 스택: 최신 develop(5ce24da) 이미지, `M1_SOURCE=board`, `M1_ALERT_MODE=standalone`, M3 = **FP32 `v34_homepos.onnx`(노트북과 같은 파일, INT8 아님)**, M4 = `whisper_onnx_int8_ft_svc`, FCM 준비됨, `VOICE_ENABLED=false`.
- `sys:settings` 모델 스위치: 노트북·RPi5 모두 `m1=false, m2=false, m3/m4/m5=true` (누가 20:28에 m1을 꺼둠). 전체 흐름 측정은 M3·M4·M5 기준. M1은 보드 추론이라 N_pose 노드 품질 단계로 따로 본다.

## 1-1. 22:55 갱신 — 3환경 측정·시각화 완료
- RPi5 결과는 `reports/laptop/val_20261005/rpi5/`로 가져왔고, `make_viz.py`를 다시 돌려 `SUMMARY.md`와 `viz/1~8`을 만들었다. 아래 2절의 1·2번은 끝났다.
- 사용자 결정: **영상 녹화와 실제 확률 검증(빈 방 30분 · 모의 낙상)은 다음 주.** 지금은 경보 방식 보완점을 검토하는 단계다(9절 "경보 방식 보완 후보"를 보고 사용자가 고른 것만 구현).

## 2. 해야 할 일 (순서대로)
1. **RPi5 결과를 노트북으로 가져오기**
   ```bash
   scp -r csi@192.168.1.2:~/safewave/reports/rpi5-20261005/val reports/laptop/val_20261005/rpi5
   ```
   M4 결과는 `rpi5/m4eval/rpi5_20261005/`, 전체 흐름은 `rpi5/<시각>-rpi5_npose/summary.md`.
2. **3환경 비교표 + 시각화** (사용자 요청: 그래프·스펙트로그램·히트맵). **스크립트 준비됨**: `reports/laptop/val_20261005/make_viz.py`, 실행 파이썬 `C:\Users\jinsa\anaconda3\python.exe`(rp5 env에는 matplotlib 없음). `rpi5/` 폴더가 생기면 자동으로 3환경 비교가 된다. 저장: `reports/laptop/val_20261005/viz/` + 요약 `reports/laptop/val_20261005/SUMMARY.md`.
   - 막대: M3 낙상 감지(50)·오탐(16)·추론 p50/p95, M4 CER/WER/키워드/p50, M5 정확·인접·safe_fail, 전체 흐름 M3+M4 p50/p95, 컨테이너 메모리·CPU.
   - 히트맵: (환경 × 지표) 정규화 표, M3 조건별(메인방·부엌·화장실 문열림/닫힘) × 환경 감지율, N_pose 노드 × 지표(fresh %, coverage p50, 점수 p95, 사용 가능 %).
   - 스펙트로그램: M3 낙상 클립 1개(감지)·1개(놓침, 화장실_문닫힘) 멜 스펙트로그램, M4 "도와주세요" 클립. 데이터 `data/m3_eval_v34/core/fall_test/`, `data/m4_eval_2398/audio/help_direct/new_eval_0010.wav`.
   - 노트북 파이썬: `C:\Users\jinsa\anaconda3\envs\rp5\python.exe` (matplotlib 있음, TensorFlow 없음). 한글 폰트 `Malgun Gothic`.
3. 결과 보고 후, 미커밋 스크립트 2개 커밋 여부를 사용자에게 묻는다.

## 3. 비교할 때 꼭 적을 주의점
- **표본 크기가 다르다.** GPU는 M4 200건·M5 300건(102 운영)·M3 생활소음 4시간까지, CPU·RPi5는 M4 50건(`m4_ids50.txt`)·M5 30건(seed 5051)·생활소음 생략. 3환경 비교는 **같은 표본**으로 한다 → GPU의 `laptop_gpu/m4eval/laptop_gpu_20261005/results.jsonl`, `laptop_gpu/m5_m2off_*.json`에서 50건·30건과 같은 id만 다시 집계해서 비교(M5 30건은 seed 5051 무작위 30 — `scripts/eval_qwen_accuracy.py --random 30 --seed 5051`이 고르는 id를 확인).
- **노드 수가 다르다.** GPU 전체 흐름(21:33~21:38)은 노드 5대 수신. CPU 전체 흐름(22:08~22:11)은 노트북이 노드 일부만 받음. 원인은 김태연 문서 1절: 22:11 이후 펌웨어가 노트북(192.168.1.11)과 RPi5 두 곳으로 보내는데 **노트북 쪽 송신이 99% 실패(errno=12, ARP 추정)**. RPi5는 5대 모두 정상 수신 중(22:23 확인, 새 프레임 77~87%).
- **펌웨어 세대가 다르다.** GPU 측정(21:3x)은 1차 업로드(자동 형식 고정, 노드 5 fresh 0%), RPi5는 2차 업로드(비HT 고정). N_pose 노드 품질은 같은 조건 비교가 아니다.
- M3 CPU vs GPU: 둘 다 낙상 46/50, 오탐 5/16 동일. 추론 p50 GPU 40.6 ms / CPU 838.8 ms.
- M4 같은 50건: GPU·CPU 모두 CER 9.43%·키워드 84%(전사 결과가 사실상 같음), p50 GPU 636 ms / CPU 1002 ms. GPU 200건 전체는 CER 5.15%(50건 표본이 더 어려운 쪽).
- M5(M2 off): GPU laptop 지시문 97/102 운영 정확, rpi5 지시문 77/102. CPU 30건: laptop 7/8 운영·28/30 정확, rpi5 6/8·15/30. safe_fail 모두 0.
- 앱 푸시(GPU): 주입→경보 593 ms, 경보→발송 91 ms, 기기 1대 발송, 앱 확인(ack) 없음, voice_ok false. 폰 실제 표시는 앱 쪽 확인 필요.
- 경계값: GPU·CPU 모두 18/18.

## 4. 그다음(사용자가 요청하면)
- 노트북 수신 복구: 노트북에서 `ping 192.168.1.20`~`.24`, 무선 절전 끄기, 방화벽 UDP 5005 허용 확인 (김태연 문서 1절).
- 허브 규칙 결정: 빈 방 30분 이상 → `python scripts/dev/npose_quality.py --seconds 1800 --label empty_room --out ...`, 이어서 실제 방 모의 낙상(`handoff/M1_EVENT_TEST_PROTOCOL_20261005.md`). 후보: 1회 ≥ 0.9 또는 5회 중 2회 ≥ 0.8 (`handoff/M1_WIFALL_VAL_20261005.md`). 바꿀 땐 `M1_BOARD_*` 환경변수만, 재빌드 불필요.
- 노드 2·4 새 프레임 비율이 0.9 기준 근처 → 배치 후 다시 잼.
- 김태연 쪽 확인 요청: 2차 펌웨어 커밋 `5e0d838`(rebuild/model), 노드 IP·MAC 표는 `handoff/NPOSE_NODE_STATUS_20261005.md` 2절.

## 5. 자주 쓰는 경로
| 무엇 | 경로 |
|---|---|
| 노트북 compose 오버레이 | `reports/laptop/compose.gpu.yml`, `compose.cpu.yml` (`COMPOSE_FILE='docker-compose.yml;reports/laptop/compose.gpu.yml'`) |
| 측정 스크립트 | `reports/laptop/val_20261005/run_laptop_gpu.ps1`, `run_laptop_gpu_extra.ps1`, `run_laptop_cpu.ps1`, `run_rpi5.sh` |
| 평가기 | `scripts/dev/m3_env_eval.py`, `scripts/eval_m4_stt.py`, `scripts/eval_qwen_accuracy.py`, `scripts/dev/boundary_check.py`, `scripts/dev/npose_quality.py`, `scripts/dev/push_e2e.py`, `scripts/bench_rpi5.py` |
| PowerShell 로그 인코딩 | `suite.log` 일부는 UTF-16 → `iconv -f UTF-16 -t UTF-8` |

## 9. 경보 방식 보완 후보 (22:55 검토, 아직 구현 안 함 — 사용자가 고른 것만)
근거: 10-05 RPi5 측정 + 코드 `ai/main.py` `_audio_worker_loop`(535~) · CSI 루프 규칙 경보(1183~1236), `ai/logic/emergency_score.py`, `ai/runtime_inputs.py` `BoardScoreAggregator`.
1. **긴급 음성 경보가 M3를 기다린다.** M4→M3를 순서대로 다 돈 뒤에야 `audio:result`/캐시를 쓴다. RPi5에서는 M4 5.9 s + M3 6.0 s 때문에 주입→경보가 16.8 s 걸렸다(GPU 0.59 s). → M4 결과를 먼저 캐시하거나, 긴급 문장이 나오면 M3를 생략한다.
2. **오디오 밀림 병합으로 긴급 음성이 버려진다.** 처리 중 새 이벤트가 오면 최신 1건만 처리한다(551~557). RPi5 전체 흐름에서는 18건 중 11건만 처리했다. "도와주세요" 뒤에 소리가 이어지면 긴급 문장 이벤트가 건너뛰어질 수 있다. → 건너뛰는 이벤트도 M4(또는 긴급 문장 검사)는 거치게 하거나, 병합 대상을 '긴급 후보 아님'으로 한정한다.
3. **오디오 결과 유효 시간(`AUDIO_RESULT_MAX_AGE_MS` 30 s)이 RPi5 지연에 바짝 붙어 있다**(M3+M4 p95 25.4 s). 밀리면 결과가 버려져 경보가 조용히 빠진다. → 처리 시작 시각 기준으로 나이를 재거나 60 s로 늘린다.
4. **보드 모드에서 "낙상+충격음"·"긴급어+낙상 의심" 보강 규칙이 거의 성립하지 않는다.** 보드 `fall_score`는 지금 이 순간의 최신 창(0.2 s 단위) 값이다. 그런데 M3/M4 결과는 소리가 난 뒤 1 s(GPU)~12~20 s(RPi5) 지나서 도착하므로 둘이 같은 틱에 겹치지 않는다. 그래서 `corroborated` 모드는 사실상 `off`와 같다. → 보드 점수를 노드별로 최근 N초(예: 소리 시각 ±10 s) 최댓값으로 보관하고, 오디오 이벤트 시각(`ts_ms`) 기준으로 맞춰 판정한다.
5. **coverage ≥ 0.9 조건 때문에 보드 판정에 쓸 수 있는 창이 거의 없다.** RPi5 2차 펌웨어 60초 측정: coverage p50 0.72~0.84, 판정 사용 가능 0~13%(노드 3은 0%). 이대로면 M1을 켜도 경보가 거의 안 난다. → 다음 주 실측에서 `M1_BOARD_MIN_COVERAGE` 0.7~0.8을 후보로 같이 재고, 김태연에게 새 프레임 비율 개선(ping 주기 등)을 요청한다.
6. 앱 확인(ack)과 음성 확인이 GPU·RPi5 둘 다 없었다. RPi5는 `VOICE_ENABLED=false`라 `voice_ok=null`이 정상이다. 앱의 확인 버튼 → `POST /alerts/{id}/ack`가 실제로 동작하는지 앱 쪽에서 한 번 확인해야 한다.
7. (참고) 노트북 수신 실패(김태연 문서 1절)는 경보 코드 문제가 아니다.

## 10. 23:05 갱신 — 마무리 상태
- 경보 보완 1·2·3번을 **구현했다**(미커밋). 수정 파일: `ai/main.py`, `ai/runtime_inputs.py`(`audio_event_plan`), `docker-compose.yml`(`AUDIO_BACKLOG_MAX_WAIT_MS`), `tests/test_m5_pipeline.py`. 단위 테스트 53개 통과.
- RPi5 ai-experts는 `~/overlay_20261005`의 덧씌우기 이미지(`safewave-ai-experts:latest`)로 돌고 있다. 원본은 `safewave-ai-experts:pre_audiofix_20261005`. 커밋·push 뒤 RPi5에서 `git pull` → 정식 재빌드하면 덧씌우기가 필요 없어진다.
- RPi5 확인 결과: 앱 푸시 주입→경보 16.8 s → 5.35 s. 밀림 시험("도와주세요" 직후 소리 2건)에서도 경보 5.3 s. 결과 파일 `reports/laptop/val_20261005/rpi5_audiofix/`.
- M1 5분 실측(RPi5, 22:51~): 원자료 `reports/laptop/val_20261005/rpi5_m1_live/`, 분석 스크립트 `m1_live_analysis.py`. 실측 뒤 `sys:settings` m1은 false로 되돌렸다.
- 공유물: `reports/laptop/val_20261005/WEEKLY_REPORT_20261005.md`, `safewave_val_20261005.zip`.
- 남은 일(사용자 요청 시): 보완 3건 커밋 여부 확인 / 보강 규칙 시간 맞춤(9-4) / 다음 주 영상 시험.
