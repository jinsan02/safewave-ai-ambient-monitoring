# RPi5 8GB 운영 자원 예산

기준일: 2026-09-17  
대상: Raspberry Pi 5 8GB, 4코어, 콘솔 부팅, M4 INT8, M5 Qwen2.5-1.5B Q5 GGUF

이 문서는 운영 상한과 다음 실기기 검증 기준을 정의한다. 아래 예상치는 기존 RPi5 실측에서
출발한 예산이며, 변경 후 실측값이 아니다.

## 1. 기존 실측과 변경 후 예상

기존 INT8 전체 동시 부하 120초:

- 호스트 메모리 p50 6.5GB / p95 7.1GB / 최대 7.3GB
- swap p50 857MB / 최대 1,752MB
- 호스트 CPU p50 92.9% / p95 94.4%
- ai-experts 약 2.2~2.5GB
- ai-qwen 약 2.0GB에서 반복 호출 후 4.3~5.2GB까지 증가한 이력

이번 경계 적용 후 예상:

| 항목 | 정상 예상 | 8GB 대비 | 경고 기준 |
|---|---:|---:|---:|
| ai-experts | 2.2~2.6GB | 28~33% | 2.9GB |
| ai-qwen | 2.0~2.6GB | 25~33% | 2.8GB |
| Redis | 0.12~0.25GB | 2~3% | 0.25GB |
| OS·Docker·API·sensing·MQTT·TTS | 0.9~1.3GB | 11~16% | 1.5GB |
| 합계(HA 제외) | **5.2~6.75GB** | **65~84%** | 7.0GB |

운영 중심 예상값은 5.8~6.4GB(약 72~80%), 순간 p95는 6.4~6.8GB다. Home Assistant와
실제 오디오/TTS를 모두 올리면 0.3~0.7GB가 추가될 수 있다. `MemAvailable`은 평상시 1.2GB 이상,
최소 1.0GB를 유지해야 한다.

변경 후에도 ai-qwen이 2.8GB를 넘어서 계속 증가하면 이 예산은 성립하지 않는다. 2차 코드·원자료
검토에서 llama-cpp-python RAM cache의 표시 용량이 scores 복사본을 포함하지 않을 가능성이 확인됐다.
따라서 다음 RPi5 실험은 64MB를 운영값으로 확정하지 않고 `QWEN_GGUF_CACHE=0`과 64MB를 각각
20회 비교한다. 자세한 근거는 `DATA_DRIVEN_OPTIMIZATION.md` 3-1절을 따른다.

## 2. Redis 보존 예산

| 데이터 | 운영값 | 실제 시간 범위 | 메모리 예산 |
|---|---:|---:|---:|
| `csi:raw` | 36,000 | 3노드 약 120초 / 5노드 약 72초 | 약 30~45MB |
| `audio:events` | 120 | 6초 이벤트 연속 기준 약 12분 | raw 최대 약 44MB |
| `ai:result` | 18,000 | 3노드 약 10분 / 5노드 약 6분 | 약 20~60MB |
| `audio:result` | 600 | 이벤트 빈도에 따라 약 1시간 | 1MB 미만 예상 |
| `ai:emergency` | 3,600 | Phase 2 lock 기준 수 시간 | 수 MB 예상 |
| minute/latest/health/settings | TTL 30~3,600초 | 최대 1시간 | 수 MB 미만 |

Redis 목표는 256MB 이하, 위험 경고는 384MB, 하드 상한은 512MB다. eviction으로 설정·락·스트림을
조용히 삭제하지 않도록 `noeviction`을 사용한다. 하드 상한 도달은 정상 복구가 아니라 구성 오류로 본다.

## 3. M5 컨텍스트 예산

- GGUF model: 1.285GB 파일
- `n_ctx=2048`: FP16 KV cache 약 56MiB
- RAM prompt cache: 로컬 후보 64MiB, 다음 실험 권장 시작값 off(0); 아직 운영값 미확정
- 출력: 최대 64 tokens
- 실제 프롬프트 기존 관측 중앙값: 약 772 tokens
- 과거 사건: `ai:emergency` 최대 300건, 60초 프로세스 캐시
- vital 추세: `agg:minute:*` 최대 60행

`n_ctx=2048`을 유지한다. 3,072는 KV cache 약 84MiB로 메모리상 가능하지만 현재 프롬프트에는
필요하지 않다. 4,096은 약 112MiB이며, 품질 근거 없이 늘리지 않는다. minute history는 프로젝트의
TTL 3,600초 규칙 때문에 60분을 상한으로 둔다.

## 4. 컨테이너별 운영 경계

기본 Core profile은 `db`, `sensing`, `ai-experts`, `ai-qwen`, `api` 5개다. 음성 기능은
`--profile audio` 또는 `--profile voice`, MQTT 외부 연동은 `--profile integration`, Home Assistant는
`--profile home`에서만 추가한다. memory limit는 예약량이 아니라 비정상 증가 상한이다.

| 컨테이너 | CPU shares | memory limit | OOM 우선순위 | 역할 |
|---|---:|---:|---:|---|
| sensing | 2048 | 256MB | 강하게 보호 | UDP 유실 방지 |
| db | 1536 | 768MB | 보호 | 전체 메시지 버스 |
| ai-experts | 2048 | 3GB, swap 금지 | 보호 | M1~M4, Phase 2 STT |
| ai-qwen | 1024 | 3GB, swap 금지 | 먼저 종료 가능 | M5 장시간 CPU/메모리 작업 |
| api | 1024 | 384MB | 보호 | Phase 2·알림·상태 |
| audio-sensing | 1024 | 256MB | 보통 | VAD·마이크(`audio`/`voice`) |
| mqtt | 512 | 128MB | 보호 | 외부 연동(`integration`/`home`) |
| tts-worker | 512 | 384MB | 보통 | 외부 합성·재생(`audio`/`voice`) |
| Home Assistant | 256 | 512MB | 가장 먼저 종료 가능 | 선택 서비스(`home` profile) |

memory cgroup이 비활성인 현재 RPi5에서는 memory limit가 적용되지 않을 수 있다. 다음 배포 전에
`/sys/fs/cgroup/cgroup.controllers`에서 `memory`를 확인하고, 비활성이면 사람 승인 후 부팅 설정을
변경한다.

## 5. CPU·스케줄러 합격 기준

- 호스트 CPU p50 ≤75%, p95 ≤90%
- CPU PSI `some avg10` 지속값 <10%
- memory PSI `full avg10` = 0에 근접
- swap `si/so` 지속 발생 없음
- 온도 <80°C, `throttled=0x0`
- `csi_backlog_skipped`와 `packet_gap_detected`가 정상 네트워크에서 0에 근접

RPi5 네 코어는 동일 Cortex-A76이므로 `cpuset`으로 고정 분할하지 않는다. CFS/cgroup 가중치로
수신·Redis·M4를 보호하고 유휴 CPU는 M5가 사용할 수 있게 둔다. `SCHED_FIFO/RR`은 네트워크·Redis
starvation 위험 때문에 사용하지 않는다.

## 6. 다음 RPi5 검증

1. 다음 모델 업데이트 커밋과 모델 파일 해시를 기록한다.
2. 재부팅 또는 사람의 swapoff/swapon으로 이전 swap 오염을 제거한다.
3. 컨테이너의 CpuShares, Memory, MemorySwap, OomScoreAdj 적용값을 `docker inspect`로 확인한다.
4. 시작 직후, 워밍업 완료, 20회 M5 호출 후 RSS뿐 아니라 PSS·Anonymous·Private Dirty·SwapPss와
   Redis `MEMORY USAGE`를 기록한다.
5. 전체 동시 부하 120초 후 30분 안정성 테스트를 수행한다.
6. cache off/64MB를 같은 입력으로 각각 20회 비교한다. ai-qwen RSS 증가가 100MB를 넘거나 총
   2.8GB를 넘으면 무효로 판정한다.
7. Redis가 256MB를 넘으면 스트림별 `MEMORY USAGE`와 `XLEN`을 먼저 확인한다.

이 시스템은 의료기기가 아니며, 자원 예산 통과는 탐지 정확도나 임상 안전성 검증을 의미하지 않는다.
