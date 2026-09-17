# 통합 아키텍처 조정안 — 2026-09-17 RPi5 측정 기준

대상: 노진산(통합). 근거는 모두 RPi5 실측(`reports/rpi5-20260917/TEST_LOG.md`, Git 제외).
판단 기준: **메모리 점유, 스레드(코어) 사용, 추론 지연·처리량.** 모델 내부 개선은 담당자 문서로 넘겼다
(`TO_KIMTAEYEON_M1_M2.md`, `TO_LEEDAEGYEONG_M4.md`).

## 0. 현재 상태 (09-17 16:58)

| 항목 | 값 |
|---|---|
| 구성 | GUI 해제(콘솔 부팅), M1 김태연 인계 모델, M2 미학습 스텁, M3 6/11 AST, **M4 이대경 INT8(설정 보완 복사본)**, M5 Qwen2.5-1.5B Q5 |
| 메모리 | 5.3 GB 사용 / 8 GB, 스왑 979 MB. ai-experts 2.2 GB, ai-qwen 3.3 GB |
| 이미 반영 | 데스크톱 GUI 해제, M4 INT8 기본값(`.env`), `M1_MAX_NODES`·`M1_FALL_THRESHOLD` 오버라이드 삭제, 빌드 캐시 정리 |

## 1. 측정으로 확인된 병목 요약

| 영역 | 증상 (RPi5) | 영향 |
|---|---|---|
| CSI 루프 | 패킷마다 M1 실행(동시 부하 시 p50 11.6 ms) × 300 pkt/s → 건너뜀·버퍼 리셋 반복 | **M1이 실제 CSI 창을 한 번도 받지 못함** (점수 0 입력 값 고정) |
| 메모리 | fp32 시절 전역 OOM 22회. INT8에서도 M5 반복 시 ai-qwen 2.0 → 4.3 GB, 호스트 7.5 GB·스왑 1.75 GB | 부하가 길어지면 OOM 재발 가능 |
| 음성 경로 | 음성 1건: M3 단독 6.3 s + M4(INT8) 단독 4.4 s, 순차 처리. 동시 부하에서 이벤트→결과 p50 30.7 s / 최대 49 s | **Phase 2 대기 15 s 초과** |
| M5 | 1회 단독 약 9 s, 동시 부하 23~25 s, 호출 간격 제한 5 s | 대기 누적, 코어 경합 |
| CPU | 동시 부하 호스트 91~93%, 온도 최고 73.8 °C (스로틀링 없음) | 여유 없음 |
| 기동 | ai-experts 준비 fp32 92 s → INT8 46~52 s | 개선됨 |
| M2 | 스텁이 심박 118 고정 → 바이탈 경고 상시 | 위험도·M5 호출 빈도 왜곡 |

## 2. 조정안 (우선순위)

### P0 — 기능·안정성

| 번호 | 조정 | 방법 | 기대 효과 | 확인 |
|---|---|---|---|---|
| B1 | **M1 전역 200ms + zero-fill + 창끝 게이트 + K=3/N=5** | `M1_INFER_INTERVAL_MS=200`, 필수 노드 1·2·3의 100Hz 격자를 유지하고 결측 슬롯만 0으로 채움. 마지막 실제 프레임이 현재 수신 시각 기준 10ms 이내일 때만 추론. 5회 중 3회 이상을 사건 판정 | M1 호출 최대 300 → 5회/s, backlog·버퍼 reset 해소, 단일 창 오탐 억제 | RPi5에서 `fall_score` 변동, `input_status`, 게이트 통과율, K/N 사건 수, `csi_backlog_skipped` 확인 |
| M2 | **M2 스텁 기본 off** | `api/main.py`·`ai/main.py`·`monitor.html` 기본값을 false로 조정. 스텁 파일은 삭제하지 않고 검증 모델 결과 후 재배선 | 바이탈 경고 상시 발생 제거 → 위험도·M5 호출 측정 정상화 | Redis `sys:settings` 잔존값 확인, `risk_level` 분포, `slm_needed` 빈도 |
| A2 | **ai-qwen 메모리 증가 억제** | `QWEN_GGUF_CACHE_MB` 256 → 64 우선 적용, 그래도 늘면 캐시 끄고(`QWEN_GGUF_CACHE=0`) 비교. `n_ctx` 2048 유지 | 반복 호출 시 2.0 → 4.3 GB 증가 억제 (원인 미확인 — 캐시 여부로 분리) | M5 20회 반복 전후 ai-qwen RSS |
| A3 | OOM 격리 (선택) | 커널 `cgroup_enable=memory` + 컨테이너 `mem_limit` (재부팅 필요) | OOM이 나도 전체 스왑 폭주·수신 정지로 번지지 않음 | 강제 부하에서 다른 컨테이너 재시작 0 |

### P1 — 지연·Phase 2

| 번호 | 조정 | 방법 | 기대 효과 | 확인 |
|---|---|---|---|---|
| C1 | 오디오 순서 M4 → M3 | `_audio_worker_loop`에서 M4 먼저, `audio:result`를 M4 결과로 먼저 기록(또는 M3 결과 분리) | 전사 결과가 M3를 기다리지 않음 (단독 기준 10.7 → 4.4 s) | Phase 2 응답 도착 시간 |
| C3 | **밀린 오디오 건너뛰기** | 처리 중 쌓인 `audio:events`는 최신 1건만 처리 (CSI 루프의 backlog 건너뜀과 같은 방식) | 대기열 누적(30~49 s) 제거 | 연속 주입 시 이벤트→결과 지연이 늘지 않는지 |
| C2 | M4 최대 생성 토큰 | `max_new_tokens` 128 → 48을 환경변수로 노출 | 환각으로 길게 생성할 때 시간 상한 | 28개 평가 정확도 불변 확인 |
| D | **상황별 코어 배분** | 평상시: M1·M2(1) + M3(2) + M5(3). **Phase 2 진행 중**: M3 일시 정지·M5 호출 보류로 M4에 코어 우선. 단독 스레드 스윕으로 정하지 않고 동시 부하 측정으로 결정 | Phase 2 응답 시간 확보 | 동시 부하에서 음성 이벤트→결과 < 15 s |
| M5 | M5 호출 간격 | `SLM_MIN_INTERVAL_MS` 5 s < 실제 지연 23 s → 처리 중 요청 누적 방지(처리 중이면 건너뜀), 간격 재설정 | 대기 누적·경합 감소 | M5 요청 대비 처리 비율, 지연 분포 |

### P2 — 운영·빌드

| 번호 | 조정 | 근거 |
|---|---|---|
| W | M1 워밍업을 `(1, M1_MAX_NODES, 64, 100)` 0 텐서로 | 5채널 모델에서 `warmup_failed` |
| G | ai-experts 이미지에서 CUDA판 torch 제거 (CPU 인덱스 고정) | 이미지 9.8 GB, 전체 빌드 약 40분 |
| H | `/audio/events`의 `trigger_ai=True`가 노드 CSI 스트림에 빈 이벤트를 넣고 `csi:raw`를 36,000건으로 자름 | 대시보드 마이크 테스트가 실 ESP 입력을 오염 |
| M4 | INT8 설정 보완 복사본을 정식 경로로 정리 | 이대경 쪽 `generation_config.json` 수정본 받기 또는 서비스 transformers 버전 결정 |
| F | 대시보드 "AI 준비 중" 표시 | api 준비 후 AI 준비까지 이전 위험도 표시 |
| T | 온도 | 부하 시 72~74 °C — 방열 확인 |
| S | RPi5 워킹트리 | 노트북 커밋 후 RPi5는 동일 내용 미커밋 파일을 치우고(`git checkout -- <파일>`) pull |

## 3. 재측정 계획 (조정 후, 같은 조건 1회 비교)

1. `bench_startup.py --runs 1` — 기동 시간·메모리
2. 전체 스택 비교와 같은 조건: 모든 모델, 음성 15 s마다, 임계값 0, 120 s (`bench_rpi5.py --threshold 0.0 --audio ... --audio-every 15`)
3. M4 28개 평가 (INT8, 스레드 기본) — C2 적용 시 정확도 불변 확인
4. 비교 지표: 유효성(재시작 0), 메모리·스왑 최대, 호스트·컨테이너 CPU, ai:result/s, 건너뜀·리셋 횟수, **M1 점수 변동 여부**, 음성 이벤트→결과, M5 지연

## 4. 결정이 필요한 것

- M2 스텁: 기본 off는 적용 완료. 검증 결과에 따라 파일 제거·FFT 폴백·M5 입력 제외를 최종 결정
- C1 적용 시 `audio:result` 형식(M4·M3 결과를 한 건에 두 번 쓸지, 두 건으로 나눌지) — api Phase 2가 읽는 형식과 함께 결정
- M4 guard 포함 wrapper 통합 여부 (Python 3.10 환경 분리 필요)

## 5. 통합 코드 반영 현황 (2026-09-17, RPi5 재측정 보류)

이번 변경은 다음 모델 업데이트와 함께 RPi5에서 한 번에 재측정하기 위해 코드·운영 기본값까지만
반영했다. 노트북에서는 성능 수치를 만들지 않았고, Python 컴파일·단위 테스트·Compose 렌더만 확인했다.

### 반영

- M1: 운영 형상 `(1, M1_MAX_NODES, 64, 100)` 워밍업, 노드별 100Hz 격자 zero-fill,
  필수 노드 1·2·3 창끝 게이트 후 전역 5Hz 추론, 최근 5회 중 3회 집계, 중간 스냅샷은
  직전 결과를 최대 1초 재사용한다. CSI executor에는 M1/M2만 제출한다.
- 오디오: backlog는 최신 1건으로 병합, M4를 먼저 실행한다. 기존 `audio:result` 형식은 유지하며,
  `phase2:active:{node}`가 있을 때 M3를 생략해 M4 결과를 바로 기록한다.
- Phase 2 락 값은 실행 중 `active`, 완료 후 남은 TTL 동안 `cooldown`으로 바뀐다. M3는 실제
  `active` 동안만 생략하고, 중복 M5 판단은 `cooldown`이 끝날 때까지 억제한다(기존 값 `1`도 active로 호환).
- M4: `M4_MAX_NEW_TOKENS=48` 기본 후보.
- M5: 한 번에 읽은 backlog를 소진한 뒤 최고 위험·최신 후보 하나만 처리한다. 30초 초과 후보를
  폐기하고, 추론 **완료 시점**부터 5초 쿨다운한다. Phase 2 활성 노드는 건너뛴다.
- M5 결과: `ai:emergency.risk_score`는 gate 점수가 아니라 M5 최종 점수를 기록하며,
  최종 score/level/emergency를 한 번 더 정규화한다.
- M5 이력: `ai:result` 스냅샷 수를 경고 횟수로 세지 않는다. `ai:emergency`를 기준으로 같은
  노드의 90초 이내 반복을 한 사건으로 합친다.
- RPi5 후보 기본값: M3 1, M4 2, M5 2 threads, GGUF cache 64 MB. ai-experts의
  OpenMP/BLAS 숨은 스레드는 1로 제한하고 glibc arena는 2로 제한한다.
- Linux cgroup CPU 가중치: sensing/ai-experts 2048, Redis 1536, API/M5/audio 1024,
  MQTT/TTS 512, Home Assistant 256. CPU 상한·코어 고정은 두지 않는다.
- Redis/data plane: `csi:raw` 36,000, `audio:events` 120, Redis 512MB/noeviction.
  Redis 256MB부터 warning, 384MB부터 critical로 보고한다.
- 컨테이너 memory/OOM 경계: sensing·Redis·ai-experts·API를 보호하고 M5·Home Assistant가
  경합 시 먼저 양보한다. ai-experts/M5는 3GB 상한과 swap 금지 후보를 적용했다.
- 오디오 노드 격리: ai-experts 프로세스 내부에서 노드별 최신 결과를 30초만 유지하며,
  Phase 2 transcript도 같은 노드의 `ai:result`만 받는다. API의 빈 CSI 트리거는 제거했다.
- M5 historical context: GGUF/1.5B는 `ai:result` 1,800건 재스캔을 제거하고 minute 집계와
  `ai:emergency` 300건(60초 캐시)만 사용한다. legacy 0.5B는 scan 300과 사건 중복 제거를 적용했다.
- TTS: 큐는 32건/TTL 1시간으로 제한하고 합성 MP3는 재생 뒤 삭제한다.
- 서비스 profile: 기본은 Core 5개로 줄이고 audio/TTS는 `audio`·`voice`, MQTT는 `integration`,
  Home Assistant는 `home`에서만 실행한다. ai-experts의 MQTT 기본값은 off다.
- 클린 코드: M5 후처리 정책, M1 입력 조립, sensing Redis 연결을 각각 순수/공통 모듈로 분리하고
  Phase 2 task와 TTS MQTT/Redis 수명주기를 명시적으로 관리한다.
- 측정 도구: CPU·memory PSI avg10, CPU governor, cgroup controller, 실제 컨테이너 CpuShares를
  원시 결과에 추가했다. memory cgroup 활성 시 컨테이너 `MemUsage`도 함께 기록한다.

### RPi5 스케줄링 판단

RPi5의 네 Cortex-A76 코어는 big.LITTLE 구성이 아닌 동일 코어다. `cpuset`으로 한 코어를 M5에
고정하면 sensing UDP, Redis, 오디오 IRQ가 나머지 코어에서 경쟁하고 유휴 코어를 빌려 쓰지 못한다.
따라서 이번에는 CFS/cgroup 상대 가중치와 네이티브 thread 상한을 사용한다. `SCHED_FIFO`/`RR`도
우선순위 역전과 Redis·네트워크 starvation 위험 때문에 쓰지 않는다. CPU governor·IRQ affinity는
실기기 현황을 기록만 하고 다음 측정 전 임의 변경하지 않는다.

### 다음 모델 업데이트 후 RPi5 확인 순서

1. 측정 커밋·모델 파일·환경변수를 기록하고 스왑을 비운다(재부팅 또는 사람의 swapoff/swapon).
2. `docker compose config`와 `docker inspect`로 CpuShares와 thread 환경변수가 실제 적용됐는지 확인한다.
3. M3=1/M5=2 후보로 120초 전체 동시 부하를 1회 측정한다. CPU PSI, memory PSI, 스왑 증가,
   재시작, CSI backlog, M1 점수 변화, 오디오 결과 지연, M5 지연을 함께 본다.
4. M4는 28개 고정 표본으로 `max_new_tokens=48`의 정확도·지연 회귀를 확인한다.
5. M5=2에서 p95가 30초를 넘으면 M5=3으로만 되돌리고, M4/M5 비중첩 조건으로 다시 판단한다.
   M3=1이 일반 환경음 지연을 과도하게 늘리면 M3=2로 되돌리되 Phase 2 생략은 유지한다.

M2 스텁은 기본 off로 조정했지만 파일 제거·M5 입력에서의 최종 제외는 검증 모델 담당자 회신 후 결정한다.
M4 반복 방지 wrapper 통합은 이번 변경에 포함하지 않았다.

## 6. 2차 데이터 검토에서 바뀐 우선순위

09-17 원자료와 현재 로컬 코드를 다시 교차 검토한 결과는 `DATA_DRIVEN_OPTIMIZATION.md`에 있다.
이 절의 항목은 아직 코드에 반영하지 않았다.

- M5 cache 64MB는 운영 확정값이 아니다. llama-cpp-python state cache의 scores 복사본이 표시 용량에
  포함되지 않을 가능성이 있어 다음 측정은 cache off/64를 각각 20회 비교한다.
- M2 스텁은 고정 심박 118로 `solo-m2` warning 37.4%를 만들었으므로 성능·M5 메모리 측정에서는
  검증 모델 도착 전까지 off가 권장된다.
- 기본 Core 5개에는 audio/TTS가 없으므로 `VOICE_ENABLED=false`일 때 Phase 2의 TTS 15초+STT 15초
  대기를 즉시 건너뛰는 fail-open 정책이 필요하다.
- 단일 마이크는 node 1로만 기록되지만 Phase 2는 동일 node transcript만 받는다. node 2·3 경보의
  음성 확인을 위해 전역 Phase 2 직렬화와 활성 node 귀속 정책을 먼저 결정한다.
- CPU 추가 최적화는 thread 수보다 CSI hot path의 packet 단위 M2/위험도/latest 쓰기와 sensing health
  쓰기 빈도를 줄이는 쪽이 우선이다.
- 다음 bench는 PSS/Anonymous/Private Dirty/SwapPss, swap si/so, cgroup events, Redis commandstats,
  M5 gate→start→complete를 기록해야 cache·allocator·mmap·I/O 경합을 구분할 수 있다.
