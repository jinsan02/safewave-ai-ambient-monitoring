# RPi5 데이터 기반 2차 최적화 검토

기준일: 2026-09-17  
대상: Raspberry Pi 5 8GB / 4코어 / Debian 13 / M4 INT8 / M5 Qwen2.5-1.5B Q5 GGUF

이 문서는 09-17 실측 원자료와 현재 로컬 미커밋 코드를 교차 검토한 결과다. 아래 예상 효과는
별도 표기가 없으면 **가설 또는 계산값**이며, 변경 후 RPi5 실측값이 아니다.

## 1. 판단에 사용한 데이터

| 자료 | 신뢰 범위 | 주의 |
|---|---|---|
| M4 직접 녹음 28개 thread sweep | 격리 지연·RSS·정확도 비교에 사용 가능 | 개발에 사용한 표본이라 독립 정확도 검증은 아님 |
| `solo-m2`, `solo-m3` | 해당 모델의 CPU·지연 기준으로 사용 가능 | 이전/다음 회차와 같은 호스트라 장시간 메모리 기준은 제한적 |
| `cmp-int8-peak` | 유효한 전체 동시 부하 120초 기준 | M2 스텁, cache 256MB, 변경 전 코드라 현재 후보 성능은 아님 |
| `check-all` | 병목 위치를 찾는 참고 | 종료 직후 OOM이 시작됨 |
| `solo-m1`, `solo-m4`, `solo-m5`, `base-idle` | 일부 지연 참고만 가능 | OOM·이전 M5 호출·재시작 오염 |
| `cmp-fp32-peak` | 실패/OOM 증거로만 사용 | 컨테이너 재시작 3회로 성능 비교 무효 |

현재 로컬 통합 리팩토링은 RPi5에서 실행하지 않았다. 따라서 이 문서에서 `반영됨`은 코드 또는
Compose 반영을 뜻하며 성능 검증 완료를 뜻하지 않는다.

## 2. 데이터가 말하는 현재 한계

| 관찰 | 실측 | 해석 |
|---|---:|---|
| 전체 INT8 CPU | 호스트 p50 92.9%, p95 94.4%; 주요 컨테이너 합계 약 364% | 네 코어가 거의 포화되어 thread 추가보다 작업량·중복 제거가 우선 |
| M4 thread | 1T 7.59초, 2T 4.43초, 4T 4.45초 | M4는 2T가 최적점. 4T 확대 근거 없음 |
| M5 동시 지연 | p50 24.7초, 최대 33.4초 | 단독 9초는 오염 회차라 절대 기준으로 쓰지 않음 |
| M5 메모리 | ai-qwen 2.485→4.304GB, 호스트 swap 801→1,752MB | 5회 호출과 메모리 계단 상승이 대응. 원인 분해 필요 |
| M2 스텁 | `solo-m2` 3,606건 중 warning 1,347건(37.4%) | 고정 심박 118이 M5 호출·메모리 측정을 오염 |
| 오디오 | M4 격리 4.43초, 전체 이벤트→결과 p50 30.7초 | 모델 자체보다 CPU 경합·순차 작업·대기열이 지연을 키움 |
| swap과 수신 | OOM 구간 3노드가 동시에 21%·47% 손실 | RF보다 호스트 swap/I/O·스케줄 지연의 영향이 큼 |
| 기동 | experts와 qwen warmup 약 62초 중첩, peak 7,416MB·swap 706MB | 병렬 기동은 빠르지만 8GB의 transient 위험을 키움 |

## 3. P0 — 다음 RPi5 측정 전에 결정하거나 계측할 것

### 3-1. M5 RAM cache는 0MB를 첫 비교값으로 사용

현재 로컬 기본은 64MB지만, 다음 실험의 권장 시작값은 `QWEN_GGUF_CACHE=0`이다.

- 현재 `qwen_gguf.py`는 `LlamaRAMCache`를 사용하고 `n_batch`를 지정하지 않아 기본 512다.
- llama-cpp-python 현재 구현은 `logits_all=False`여도 `n_batch × vocab` 크기의 `scores` 배열을 만들며,
  상태 저장 시 이를 복사한다. Qwen vocab 151,936 기준 scores 한 장은 약 296.75MiB다.
- RAM cache의 표시 용량 계산은 `llama_state_size`만 합산하므로 scores 복사본은 용량 상한에 포함되지
  않는다. 따라서 64MB 설정도 실제 프로세스 RAM 64MB 상한이 아니다.
- 기존 5회 호출에서 약 0.37~0.50GB 단위로 호스트 메모리가 상승한 패턴과 부합하지만, RPi5에 설치된
  패키지 버전이 `llama-cpp-python>=0.3.0`로 고정되지 않아 **원인 확정은 아니다.**

공식 구현 참고: [Llama 상태 저장](https://github.com/abetlen/llama-cpp-python/blob/main/llama_cpp/llama.py#L2091-L2119),
[RAM cache 용량 계산](https://github.com/abetlen/llama-cpp-python/blob/main/llama_cpp/llama_cache.py#L55-L90).

다음 실행에서 먼저 실제 패키지 버전과 소스 해시를 기록하고 cache 0/64를 각각 20회 비교한다.
cache를 끄더라도 현재 context의 공통 prefix는 llama.cpp 내부 KV prefix 재사용 경로가 있어 성능 회귀는
실측으로 판단한다.

### 3-2. RSS가 아니라 메모리 성분을 기록

다음 항목을 모델 load, warmup, 호출 1·5·10·20회 전후에 같은 시각축으로 저장한다.

- 컨테이너 main PID와 자식의 `/proc/<pid>/smaps_rollup`: `Rss`, `Pss`, `Anonymous`,
  `Private_Dirty`, `SwapPss`
- `/proc/<pid>/status`: `VmRSS`, `VmHWM`, `VmSwap`, thread 수, context switch
- cgroup 활성 시 `memory.current`, `memory.peak`, `memory.events`, `memory.swap.current`, `cpu.stat`
- `/proc/vmstat`의 `pswpin`, `pswpout`, major fault 증가량
- M5 prompt/output token, source stream ID, gate→start→complete 시간

20회 뒤 ai-qwen 총 RSS 2.8GB 이하, 증가 100MB 이하, swap 증가 0MB를 1차 합격선으로 둔다.

### 3-3. M2 스텁은 성능 측정에서 기본 off 권장

스텁 파일을 삭제하거나 모델 내부를 바꾸지 않는다. 검증된 M2가 들어오기 전까지 RPi5 성능·M5 메모리
측정에서는 토글을 off로 두는 것이 권장안이다. 고정 118bpm이 만든 warning과 약 11초 간격 M5 호출을
실제 운영 위험으로 오인하지 않기 위해서다.

### 3-4. 음성 profile과 Phase 2 동작 계약 정리

현재 기본 Core 5개에는 `audio-sensing`과 `tts-worker`가 없지만 API는 critical 발생 시 TTS 신호를
최대 15초, STT transcript를 다시 최대 15초 기다린다. `VOICE_ENABLED=false`인 Core 모드에서는 Phase 2를
즉시 건너뛰고 1차 알림으로 fail-open 해야 한다.

또 단일 마이크는 기본적으로 모든 음성을 node 1로 기록하지만 API는 emergency node와 같은 node의
transcript만 인정한다. 단일 마이크 MVP는 Phase 2를 전역 한 건으로 직렬화하고 활성 emergency node에
녹음 결과를 귀속시키는 정책이 필요하다. 여러 방 구성은 마이크↔node 매핑을 명시해야 한다.

새 전역 lock/key 또는 매핑 key는 Redis 계약 변경이므로 구현 전에 사람 확인이 필요하다.

### 3-5. M5가 없어도 명확한 고위험은 1차 알림

현재 M5가 OOM·warmup·재시작 상태면 `ai:emergency`가 생성되지 않을 수 있다. 명확한 M1 낙상 또는
vital crisis는 규칙 기반 1차 경보를 먼저 기록하고 M5는 설명·문맥 보강 역할로 두는 구조가 MVP 가용성에
필요하다. 임계값과 이벤트 형식은 안전 정책이므로 별도 승인과 회귀 테스트 후 구현한다.

## 4. P1 — 효과가 큰 추가 리팩토링 후보

| 후보 | 현재 비용/결함 | 권장 방향 | 예상 효과와 검증 |
|---|---|---|---|
| M5 `n_threads_batch` | `n_threads`만 2로 제한. 라이브러리 기본 batch thread는 CPU 전체 코어 | `2/2`부터 시작, 필요 시 `3/2`, 마지막에 `3/3` | prefill 순간 M4·sensing 경합 감소 가능. 전체 p95와 CPU PSI로 선택 |
| CSI decision tick | 300pkt/s 모두에서 M2 executor·위험도 계산·latest SET 수행 | deque는 100Hz로 누적, 노드별 최대 10Hz에서 M2/결정 수행 | M2 호출·위험 계산 최대 90% 감소. 모델 담당자와 출력 cadence 합의 필요 |
| expert latest write | 실제 새 결과가 없어도 CSI hot path에서 latest key를 반복 SET | M1/M2 실제 추론, M3/M4 오디오 완료 시에만 기록 | 이론상 latest SET 95% 이상 감소. Redis commandstats로 확인 |
| sensing health write | 패킷마다 XADD+SET+HSET+EXPIRE, 최대 약 1,200 command/s | CSI XADD는 유지하고 health/last_seen은 node별 1Hz | 계산상 약 1,200→312 command/s. sensing/db CPU·loss 회귀 확인 |
| snapshot pipeline | XADD와 minute aggregate가 별도 RTT | 한 Redis pipeline으로 묶음 | snapshot 30/s 기준 약 30 RTT/s 감소 |
| M5 cooldown pending | backlog를 drain한 뒤 cooldown이면 후보를 영구 폐기 | freshness 30초 안에서 최고 후보 한 건을 pending 유지 | cooldown 중 단발 critical이 정확히 한 번 처리되는지 테스트 |
| Phase 2 전역 admission | 같은 node만 M5를 막아 다른 node M5는 실행 가능 | 단일 마이크 MVP는 어떤 Phase 2 active 중에도 신규 M5 시작 억제 | M4 15초 목표 보호. 신규 key/전역 상태는 승인 필요 |
| 선택 모델 load | 비음성 profile도 ai-experts가 M3/M4를 항상 적재 | Core/voice별 명시적 load set | INT8 M4 단독 RSS 2,259MB 기준 큰 여지. 실제 증분 RSS 재측정 필요 |

`solo-m2`의 ai-experts CPU 47%, 호스트 18.1%는 CSI hot path 비용이 모델 추론 0.1ms보다 훨씬
크다는 점을 보여준다. 따라서 thread를 더 줄이는 것보다 호출·Redis RTT를 줄이는 쪽을 먼저 검증한다.

## 5. 복구·가용성 빈틈

1. `ai-experts`의 `CSI_STREAM_START_ID=0-0`은 재시작 때 최대 36,000개 과거 CSI를 재생한다.
   최근 bounded window로 창만 채우고 live 전환 전 alert gate를 닫거나 tail에서 새로 시작해야 한다.
2. API alert worker는 `$`에서 시작해 API 중단 중 기록된 `ai:emergency`를 놓친다. bounded replay와
   dedupe 또는 consumer group이 필요하다. stream 구조 변경은 먼저 승인받는다.
3. ai-qwen `restart: always`는 OOM 때 모델 재로딩 폭주를 만들 수 있다. 제한 재시작은 직접 1차 경보와
   degraded 상태 표시를 먼저 갖춘 뒤 적용한다.
4. startup 때 experts와 qwen warmup을 직렬화하면 peak는 줄지만 전체 ready는 INT8 기준 약
   107~113초로 늘 수 있다. post-change startup peak가 7.0GB 또는 swap 증가 256MB를 넘을 때만 채택한다.
5. `_capture_audio_clip()` 내부 task는 Phase 2 task registry 밖에 있어 shutdown·예외 회수가 완전하지 않다.
6. 모든 Docker 로그에 회전 상한을 둔다. `local` 또는 `json-file` 10MB×3 후보는 9개 서비스 기준
   약 270MB로 장애 로그 디스크 증가를 제한한다. benchmark 원시 로그는 별도 보존한다.

## 6. 운영체제 스케줄링 결론

- 동일 Cortex-A76 4코어이므로 cpuset/isolcpus로 서비스별 코어를 고정하지 않는다.
- `SCHED_FIFO/RR`, IRQ affinity, RPS 변경은 peak에서도 패킷 손실 0.08%였으므로 현재 근거가 없다.
- M4 4 thread는 2 thread보다 느렸다. M4=2를 유지한다.
- performance governor 고정·오버클럭·zram/zswap 상시 활성은 보류한다. 2분 peak가 73.8°C,
  throttling 0이었고 CPU가 이미 포화라 압축 swap은 지연을 악화할 수 있다.
- cgroup memory를 활성화한 뒤 ai-experts/M5 3GB·swap 0 후보를 먼저 검증한다. 바로 2.8GB로 낮추지 않는다.
- swap 사용량 자체보다 `si/so`, major fault, memory PSI full로 thrashing을 판단한다. clean reboot 뒤
  AI swap 금지가 확인된 경우에만 `vm.swappiness=10`을 A/B한다.

## 7. M5 후속 실험 순서

1. clean reboot, swap 0, commit/model/env 및 llama-cpp-python 버전·소스 해시 기록
2. cache 0, `threads=2`, `threads_batch=2`, 고정 입력 20회
3. 같은 조건 cache 64 비교
4. p95가 30초를 넘고 CPU PSI가 안정적이면 `threads=3`, `threads_batch=2` 비교
5. 호출 순간 Anonymous peak가 문제일 때만 `n_batch/n_ubatch=256` 비교
6. cache off 뒤에도 Anonymous가 계단 상승하면 idle 시 `malloc_trim(0)` 실험 또는 할당 추적
7. 마지막 수단으로 Q4 모델을 별도 품질 평가와 함께 A/B

채택 기준: RSS 증가 100MB/20회 이하, 총 2.8GB 이하, M5 p95 30초 이하, host CPU p95 90% 이하,
memory PSI full≈0, swap 증가 0, sensing/db/api 재시작 0.

## 8. 전체 재검증 최소 순서

1. Core idle 5분: 서비스별 PSS/Anonymous와 Redis 기준선
2. CSI 300pkt/s 5분: 현재 hot path와 10Hz decision tick 비교
3. sensing health 100Hz 대 1Hz 비교
4. M5 cache 0/64 각각 20회
5. voice peak: Phase 2 중 전역 M5 admission 차단 전후 비교
6. node 1·2·3 각각 critical 강제 후 TTS→STT 라우팅 확인
7. ai-experts/API/ai-qwen 각각 재시작 fault injection
8. 최종 30분 peak: RSS 기울기, swap si/so, PSI, gap/reset, alert 누락 확인

이 시스템은 의료기기가 아니다. 자원 합격은 탐지 정확도나 임상 안전성을 검증하지 않는다.
