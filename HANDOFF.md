# HANDOFF — SafeWave-AI (rp5)

> 작성: 2026-09-17 · 작성자 노진산(+Claude/Codex) · 다음 작업자(Claude 포함)는 이 문서부터 읽을 것.
> 규칙·제약은 `AGENTS.md` / `CLAUDE.md`, 근거 현황은 `docs/validation_status.md`.

## 한 줄 요약 (09-17 저녁)

RPi5 기본값 측정, M1·M2(김태연)·M4(이대경) 병합, fp32 대 INT8 비교까지 끝났다.
통합 파트의 CPU·메모리·M5·오디오·Redis 리팩토링은 **`develop`에 커밋·push했지만(`fa008cc`~) RPi5에는
아직 배포·실행하지 않았다.** 다음 모델 업데이트 뒤 같은 조건으로 RPi5에서 1회
재측정하기 전까지 성능 개선이나 안정성 검증 완료로 표현하지 않는다.
09-17 원자료와 현재 코드를 다시 교차 검토한 2차 최적화 결과는
`handoff/rpi5-20260917/DATA_DRIVEN_OPTIMIZATION.md`에 정리했다. 이 문서의 새 후보도 아직 미반영·미검증이다.
담당자 공유 문서: `handoff/rpi5-20260917/TO_KIMTAEYEON_M1_M2.md`, `handoff/rpi5-20260917/REPLY_TO_KIMTAEYEON_M1.md`, `TO_LEEDAEGYEONG_M4.md`.
측정 원자료·상세 기록: 노트북·RPi5 `reports/rpi5-20260917/` (`TEST_LOG.md`, Git 제외).

### 09-17 밤 — 노트북 RPi5 유사 환경 전체 테스트와 M1 2차 답변 반영

기록: 노트북 `reports/laptop/LAPTOP_TEST_LOG.md`(Git 제외). **노트북 수치는 기능 확인용이며 보고서에 쓰지 않는다.**

| 구분 | 내용 |
|---|---|
| 테스트 도구 | `scripts/sim_esp32.py`(3노드 100Hz UDP → sensing, M4 평가 음성 주입), `scripts/alert_e2e_check.py`(경보 경로·규칙 경보 확인), 노트북 override `reports/laptop/compose.laptop.yml`(CPU 한도 보정) |
| 확인된 것 | 5개 서비스 기동, sensing 298pkt/s 파싱 오류 0, M1 실제 창 추론 분당 약 297/300, 음성 이벤트→결과 10.9~12.9s(보정 후), **ai-qwen 정지 상태에서 규칙 경보→API 발송 시도**까지 동작 |
| 테스트 중 고친 것 | 규칙 경보 3중 발송(M1은 전역 모델 → 낙상 규칙은 전역 1건), M5 critical 뒤 락 노드 요청 낭비(ai-experts가 `phase2:active` 노드는 요청 생략), 격자 손실 로그 과다(60초 `m1_gate_stats`로 통합), Firebase 키 없을 때 조용한 실패(`fcm_unavailable` 시작 로그), CPU 전용 torch(ai-experts 이미지 8.79→2.29GB) |
| M1 2차 답변(김태연) | 생략 tick은 0표로 K/N에 포함(`record_skipped_m1_ticks`), zero-fill 격자를 **수신 시각(stream id) ±5ms 전역 슬롯**으로 변경(`grid_slot`, `place_m1_grid_frame`, 장치 시계 처리 제거), 창끝 허용치 기본 15ms(0이면 게이트 끔). 회신 초안 `handoff/rpi5-20260917/REPLY2_TO_KIMTAEYEON_M1.md`(슬롯 충돌 처리·격자 원점 확인 요청) |
| 사람 관점 수정 | "일어날 수가 없어/힘이 없어"가 "괜찮음"으로 분류되던 문제, "괜찮다" 후속 알림이 응급 알림처럼 보이던 문제(별도 일반 알림), 알림 본문에 노드·사유 표시, 대화 내용 로그 제거, Redis 포트 `127.0.0.1`만 바인딩 |
| Docker Desktop(노트북) | 소켓 파일 접근 불가(Win32 1920)로 시작 실패 반복 → `scripts/dev/start_docker_desktop.ps1 -Restart` 사용(소켓 폴더 비켜두기, 자동 업데이트 끄기). 근본 원인(필터 드라이버 추정)은 관리자 권한 `fltmc filters`로 확인 필요 |

**RPi5에서 볼 것:** `m1_gate_stats`의 `zero_filled_frames`·`slot_collisions`(노트북은 시뮬레이터 몰아 보내기로 분당 1,797·1,080), 창끝 허용치 10/15ms 통과율, 규칙 경보 1건 여부.

**결정 필요(사람 관점 점검에서 남은 것):**
1. M5가 규칙 점수 0.09~0.13 입력을 critical(1.0)로 올릴 수 있음 — 낙상 발화였으므로 타당했지만, M5 단독 상향 폭 제한 여부
2. 응답 없음·도움 요청 시 재알림·보호자 확인(ack)·119 연계 등 에스컬레이션
3. 시스템 정지·노드 오프라인을 보호자에게 알리는 heartbeat
4. API 인증(현재 없음, CORS `*`), 설정 변경(M1·AI 끄기) 보호, MQTT 익명 허용
5. 음성 원본 보존 정책·마이크 동의·음소거, TTS 인터넷 의존(로컬 WAV)
6. 알림 수신 기기 ID 기본값 공유(`galaxy_flip4`), 노드→방 이름 매핑, 대시보드 좁은 화면 레이아웃

### 다음 작업자 인계 — 통합 최적화 작업 (커밋 완료, RPi5 재측정 전)

아래 변경은 09-17 노진산 확인 후 `develop`에 5개 커밋으로 올렸다.

| 커밋 | 범위 |
|---|---|
| `fa008cc` feat(ai) | M1 5Hz 창끝 게이트·K/N, 오디오 최신 1건·M4 우선, `runtime_inputs.py` |
| `a5c2795` refactor(m5) | `risk_policy.py` 통합, drain·쿨다운·문맥 cache, `tests/test_m5_pipeline.py` |
| `6ed4380` fix(api,sensing,tts) | task 수명주기, `sensing/redis_client.py`, TTS 큐 경계, M2 기본 off |
| `c40c6ee` chore(infra) | Compose profile·자원 한도, Redis 512MB/noeviction, bench 계측 추가 |
| `f2436b9` docs | 이 문서, `RESOURCE_BUDGET`·`DATA_DRIVEN_OPTIMIZATION`·`REPLY_TO_KIMTAEYEON_M1` |

커밋 전 재확인: `compileall`, `tests.test_m5_pipeline` 18개 통과, 기본·audio·voice·integration·home profile
`docker compose config --quiet` 통과, `git diff --check` 이상 없음. 이미지 빌드·컨테이너 실행은 하지 않았다.

#### 09-17 추가 — M5 우회 1차 경보와 경보 누락 수정 (코드·단위 테스트만, RPi5 미검증)

서브에이전트 3관점(경보 경로 추적·재사용 코드·남은 허점) 조사 결과, 경보가 M5에 완전히 묶여 있었고
(ai:emergency 기록자는 ai-qwen뿐, API는 critical만 처리하는데 규칙 floor는 0.65) FCM이 음성 확인 30초 뒤에 나갔다.

| 변경 | 내용 |
|---|---|
| 규칙 1차 경보 | `ai/main.py` `_write_rule_alert`: `risk_policy.rule_alert_reason`(낙상 K/N 확정·낙상+위험음·생체신호 위기)이면 M5 없이 `ai:emergency`에 critical(`slm_mode="rule"`, `gate_score` 보존) 기록. 노드별 90초 쿨다운, `phase2:active` 있으면 생략, Redis 오류는 로그만. `RULE_ALERT_ENABLED`(기본 true). 스냅샷에 `rule_alert` |
| FCM 먼저 | `api/main.py` `_handle_single_emergency`: 락 → **즉시 critical FCM** → `VOICE_ENABLED=true`일 때만 TTS·STT 확인 → "괜찮다" 응답이면 후속 warning 알림(`notify:followup:{msg_id}:{device}`, TTL 3600). 기본 Core 구성은 `VOICE_ENABLED=false` |
| Redis 가득 참 | 락·중복 방지 키·TTS 큐 쓰기 실패 시에도 FCM은 발송 |
| 오래된 응답 | `_fresh_transcript`: TTS 재생 종료 이후 녹음된 오디오의 transcript만 인정 |
| API 재시작 | alert worker가 `$` 대신 최근 `ALERT_REPLAY_MS`(30초)부터 읽음. 중복은 `notify:sent`가 막음 |
| M1 투표 | 창끝 게이트만 놓친 tick은 K/N 투표 유지(`should_reset_m1_votes`), 창 미충족·결과 1초 초과일 때만 초기화 |

테스트 25개 통과(추가 7개). 남은 것: ai-experts·ai-qwen Redis 쓰기 실패 대응, ai-qwen 쿨다운 중 요청 보류,
단일 마이크 노드 필터, `/status` stale 표시, M1 중복 시각·긴 공백 처리, ai-qwen 내부 규칙 대체 경로(생체신호 위기 0.75,
`label` 미참조), cooldown 락 90초 동안 같은 노드 두 번째 사건 미평가(정책 결정 필요).
**RPi5 확인 항목:** 규칙 경보가 M1 3/5 확정 시 1회만 기록되는지, ai-qwen을 멈춘 상태에서도 FCM이 나가는지.

#### 09-17 전체 파이프라인 점검 (코드 읽기, 미실행)

수정함: alert worker가 형식이 깨진 `ai:emergency` 항목 하나에 같은 위치를 무한 재시도하던 문제(먼저 전진 후 건너뜀),
CSI 루프가 같은 위치에서 3회 연속 실패하면 최신 위치로 건너뛰도록(`csi_error_skipped`).

점검 결과와 처리 (심각도 순):

| # | 문제 | 처리 |
|---|---|---|
| 1 | FCM 토큰·설정 TTL 1시간 → 앱이 재등록하지 않으면 푸시 중단, 설정 초기화 | **해결:** TTL 3600s는 유지하고 API `_ttl_refresh_worker`가 10분마다 연장(`TTL_REFRESH_SEC`). 시스템이 1시간 넘게 멈추면 규칙대로 만료되므로 앱은 시작 시 재등록 |
| 2 | 단일 마이크(노드 1)와 CSI 노드 불일치 → 노드 2·3 경보는 음성 응답 불가 | **해결:** `VOICE_NODE_ID`(Compose 기본 1)의 오디오에서 응답을 찾음. 오디오 결과를 CSI 노드 스냅샷에 붙이는 방식과 M3 생략 락 확인은 그대로 |
| 3 | `ai:mN:latest`를 모든 노드가 패킷마다 덮어씀 | **해결:** M1은 새 추론 때, M3/M4는 새 오디오 결과 때, M2는 노드별 1초 간격으로만 기록 |
| 4 | API 모델이 `emergency_breakdown` 등을 버려 대시보드 분해 막대가 0 | **해결:** `UnifiedSnapshot`에 `slm_needed`·`rule_alert`·`emergency_breakdown`·`expert_latency_ms`·`model_latency_ms`, `EmergencySummary`에 `slm_mode`·`qwen_reason` 추가(응답 필드 추가만) |
| 5 | 웹소켓 ping·`/status` "no data yet"를 대시보드가 "정상 0.00"으로 그림 | **해결:** `monitor.html`에서 ping·`ts_ms` 없는 응답 무시 |
| 6 | 오디오 워커 M3·M4 추론이 멈추면 오디오 영구 정지 | **해결:** `_audio_watchdog`가 한 건 180초 초과 또는 스레드 종료 시 `os._exit(1)` → `restart: always`로 복구(`AUDIO_STALL_EXIT_SEC`). 오디오 결과 캐시를 XADD 전에 저장해 스트림 쓰기 거부 시에도 병합 유지 |
| 7 | TTS 합성·재생 타임아웃 없음 | **해결:** 합성 10초, 재생 20초 초과 시 중단·kill |
| 8 | MQTT 첫 연결 실패 시 재시도 없음 | **해결:** `connect_async`+`loop_start`로 백그라운드 재접속. paho 2.x는 `CallbackAPIVersion.VERSION1` 명시(`ai/mqtt_helper.py`). 기본 `MQTT_ENABLED=0`이라 실행 확인은 안 함 |
| 9 | ai-qwen 일반 예외 busy loop | **해결:** 1초 대기 |
| 10 | 재연결 시 이전 Redis 클라이언트 미종료, 오디오 스레드는 옛 클라이언트 사용 | **보류:** redis-py 클라이언트는 명령마다 재연결하므로 기능 영향은 작음. 소켓 정리는 컨테이너 실측으로 확인 후 |
| 11 | ai-qwen이 `cooldown` 락도 잠금으로 봄 → 경보 후 90초간 같은 노드 M5 미평가 | **유지(정책):** 90초는 같은 노드 재경보 억제 기간이다. 규칙 경보·API도 같은 락을 쓰므로 M5만 풀어도 새 경보는 나가지 않는다. 이 기간 두 번째 사건을 알려야 하면 락 정책 전체를 함께 바꾼다 |
| 12 | M3 입력이 log-Mel이 아닌 PCM reshape | **담당자 영역:** M3 v3.4 인계 때 해결 |

#### 해결·조정한 문제

| 영역 | 기존 문제 | 로컬 반영 내용 | 현재 판정 |
|---|---|---|---|
| M1 입력 | 패킷마다 추론해 backlog·deque reset, 실제 창은 0 입력 | 100Hz 격자 zero-fill, 필수 노드 1·2·3 창끝 게이트, 전역 5Hz 추론, K=3/N=5 집계, 운영 5노드 형상 워밍업 | 코드 반영, RPi5 실제 점수·게이트 통과율 미검증 |
| CSI executor | M3/M4까지 CSI 경로에서 빈 입력으로 실행 가능 | CSI executor는 M1/M2만 제출 | 코드 해결 |
| 오디오 backlog | 밀린 이벤트를 모두 순차 처리해 30.7~49초 지연 | backlog 최신 1건으로 병합 | 코드 해결, RPi5 지연 미검증 |
| Phase 2 우선순위 | M3 6.3초 뒤 M4 실행 | M4 우선, `phase2:active` 동안 M3 생략 | 코드 해결, 15초 기준 미검증 |
| 오디오 노드 혼선 | 전역 최신 결과가 다른 노드 CSI에 합쳐질 수 있음 | 프로세스 내 노드별 cache, 최대 30초만 병합; Phase 2 transcript도 같은 노드만 수용 | 코드 해결 |
| 빈 CSI 오염 | `/audio/events`가 M1 창에 빈 CSI trigger 추가 | `trigger_ai`는 호환 필드로만 받고 CSI 쓰기 제거 | 코드 해결 |
| M4 tail | 최대 128 token 생성으로 환각 시 장시간 디코드 | `M4_MAX_NEW_TOKENS=48` 후보 | 설정 반영, 28개 정확도 회귀 미검증 |
| M5 backlog | 오래된 요청을 순서대로 실행하고 처리 중 요청 누적 | 최대 2,000건 bounded drain 후 30초 이내 최고 위험·최신 1건 선택 | 코드 해결 |
| M5 쿨다운 | 시작 시각 기준이라 긴 추론 뒤 즉시 재호출 | 성공·실패 모두 완료 시점부터 5초 쿨다운 | 코드 해결 |
| M5/Phase 2 중복 | 음성 확인 중에도 같은 노드 M5 재실행 | lock을 `active`/`cooldown`으로 나누고 TTL 동안 M5 억제 | 코드 해결 |
| M5 결과 모순 | gate score와 M5 최종 level/emergency가 불일치 가능 | `ai:emergency`에 fused score 기록, 최종 score/level/emergency 일괄 정규화 | 단위 테스트 통과 |
| M5 vital 보정 | 모델 JSON score 문자열에서 타입 오류 가능 | `_safe_float` 뒤 warning 하한 적용 | 단위 테스트 통과 |
| M5 시간 문맥 | `ai:result` 1,800건 반복 scan, snapshot 수를 사건 수로 과대 계산 | `ai:emergency` 300건/60초 cache, 같은 노드 90초 중복 제거, vital은 minute 집계 사용 | 코드 해결 |
| M5 정책 중복 | 0.5B/1.5B에 context·feedback·level 정책 복제 | `ai/logic/risk_policy.py` 순수 정책 모듈로 통합 | 회귀 테스트 통과 |
| 이전 P2 코드 확인 | `_apply_context_window`의 critical 강제와 `_alert_worker` 블로킹 여부 미확인 | context는 경고 3회에 +0.1만 적용하며 critical 강제 없음; alert worker는 이벤트별 task로 분리되어 주 루프를 막지 않음 | 확인 완료 |
| M5 메모리 | RAM cache 256MB, RSS 2.0→4.3~5.2GB | cache 64MB, `n_ctx=2048` 유지, glibc arena 2 | 완화만 함; 누수 원인 미확정 |
| CPU 경합 | M3=2/M4=2/M5=3과 숨은 BLAS thread | M3=1/M4=2/M5=2, OpenMP/BLAS=1, CFS `cpu_shares` 적용 | 후보 반영, RPi5 미검증 |
| OOM 전파 | M5 증가가 sensing·Redis까지 압박 | 컨테이너별 memory/OOM 우선순위, experts/M5 3GB·swap 금지 후보 | Compose 반영; memory cgroup 비활성이라 RPi5에서는 아직 강제 안 될 수 있음 |
| Redis 무경계 | CSI 180만·오디오 3,600, Redis 3GB/LRU | CSI 36,000, 오디오 120, Redis 512MB/noeviction, 256/384MB 경고 | 코드·설정 해결, 실제 사용량 미검증 |
| TTS 누적 | 무경계 queue와 합성 MP3 잔류 | queue 32건/TTL 1시간, 재생 후 임시 MP3 삭제 | 코드 해결 |
| TTS 수명주기 | MQTT 내부 queue 무경계, 같은 초 임시파일 충돌, 종료 정리 없음 | queue 32건 최신 유지, 파일명 microsecond, MQTT/Redis 정리 | 코드 해결 |
| 부가 서비스 | MQTT·TTS·Home Assistant가 기본 스택 자원 사용 | 기본 Core 5개, `audio/voice`, `integration`, `home` profile로 분리 | Compose 렌더 검증 완료 |
| API task 수명주기 | Phase 2 자식 task 예외·종료 미관리 | task registry, 구조화 실패 로그, shutdown cancel/gather | 코드 해결 |
| sensing 중복/쓰기 실패 | CSI·오디오 Redis 연결 코드 중복, audio noeviction 시 캡처 재시작 | 공통 `redis_client.py`, audio ResponseError는 이벤트만 폐기 | 코드 해결 |
| M1 입력 조립 | main loop 안에 준비 판정·슬롯 조립이 인라인 | `runtime_inputs.py` 순수 함수로 분리 | 5노드 슬롯 회귀 테스트 통과 |
| Redis 준비 | `depends_on`이 Redis readiness를 보장하지 않음 | Redis healthcheck와 `service_healthy` dependency | Compose 렌더 검증 완료 |
| 측정 공백 | cgroup/컨테이너 메모리·PSI 기록 부족 | bench에 CPU/memory PSI, governor, controller, CpuShares, 사용 가능 시 컨테이너 RSS 추가 | 도구 반영 |

검증 완료: bundled Python으로 `compileall`, `tests.test_m5_pipeline` **18개**, 기본/audio/voice/integration/home
profile을 포함한 `docker compose config --quiet`, `git diff --check`. 노트북 성능값은 만들지 않았다.

#### 아직 해결하지 않았거나 다음 결정이 필요한 것

1. **M5 RSS 증가 원인은 미확정이다.** 64MB cache는 완화안일 뿐이다. RPi5에서 20회 호출 전후
   RSS 증가가 100MB 초과 또는 총 2.8GB 초과면 `QWEN_GGUF_CACHE=0` 비교 후 할당 추적한다.
2. **RPi5 memory cgroup 활성화는 하지 않았다.** `/sys/fs/cgroup/cgroup.controllers`에 `memory`가
   없으면 compose `mem_limit`/`memswap_limit`가 적용되지 않는다. 부팅 설정 변경은 사람 승인과 재부팅이 필요하다.
3. **M5 2 threads는 전체 시스템 보호안이지 단일 추론 가속안이 아니다.** 다음 측정에서 p95가 30초를
   넘으면 M5만 3 threads로 되돌린다.
4. **M2 RPi5 모델은 여전히 스텁**이다. 코드·API·대시보드 기본값을 off로 조정했지만, Redis에 이미 저장된
   오래된 `sys:settings`가 있으면 재시작 후에도 true일 수 있어 배포 시 설정을 확인해야 한다. 스텁 파일을
   제거하지 않았고, 검증 모델이 오면 별도 재배선한다.
5. MQTT/TTS/audio/HA profile 분리는 반영해 현재 기본 Compose는 Core 5개다. 다만 TTS 고정 WAV와
   M2~M4 선택 로드는 아직 코드에 반영하지 않았다.
6. ~~M5 우회 1차 경보 미구현~~ → 09-17 추가 절에서 반영(규칙 경보·FCM 먼저). RPi5 동작 확인 전.
7. ai-experts CPU 이미지의 CUDA torch 제거, M4 반복 방지 wrapper 통합, 대시보드 준비 상태 표시는 미반영이다.
8. **Redis 512MB/noeviction 도달 시 전 서비스의 실패 처리는 완전하지 않다.** sensing CSI와 audio는
   `ResponseError`를 제한 처리하지만 ai-experts/M5/API 쓰기는 동일 수준의 backpressure·경보 처리가 없다.
   정상 예산은 256MB 이하이므로 먼저 실측하되, 384MB critical에서 운영 경보 또는 쓰기 축소 정책이 필요하다.
9. Phase 2 자식 task 예외 회수와 shutdown 정리는 반영했다. 다만 TTS·FCM·Redis 실패를 실제 컨테이너
   통합 테스트로 검증하지 않았다.
10. Redis healthcheck/readiness는 반영했다. ai-experts는 별도 readiness 신호가 없어 API/M5가
    `service_started`만 기다린다. 모델 warmup 전 표시·동작을 실제 재시작 시나리오로 검증해야 한다.
11. 로컬 검증은 compile/unit/config뿐이다. Docker 이미지 build·컨테이너 실행·API/Redis 통합 테스트와
    `bench_rpi5.py`의 E2(회차 사이 잔여 M5 호출 소진)는 수행하지 않았다.

12. M1 담당자 회신을 반영해 `M1_INFER_INTERVAL_MS=200`, `M1_REQUIRED_NODES=1,2,3`,
    device uint32 시계 기반 zero-fill, 창끝 생존 게이트, K=3/N=5를 적용했다. 소폭 역행 패킷은
    늦게 도착한 것으로 버리고, 큰 역행은 장치 재시작으로 보고 창을 재시작한다. `CSI2` 792B
    진단 포맷은 기존 788B 계약과 Redis stream 구조를 바꾸므로 아직 파서·펌웨어에 반영하지 않았다.
    회신 초안과 승인 조건은 `handoff/rpi5-20260917/REPLY_TO_KIMTAEYEON_M1.md`에 있다.

#### 09-17 실측 기반 2차 검토 — 추가 발견, 아직 코드 미반영

상세 근거·예상 효과·실험 순서는 `handoff/rpi5-20260917/DATA_DRIVEN_OPTIMIZATION.md`를 따른다.
다음 작업자는 아래를 기존 반영 항목과 혼동하지 말고, 결정·계측 후 하나씩 적용한다.

| 우선순위 | 추가 발견 | 실측/코드 근거 | 권장 다음 행동 |
|---|---|---|---|
| P0 | **M5 RAM cache 64MB도 실제 총 RAM 상한이 아닐 가능성** | 유효 INT8 peak 5회 동안 ai-qwen 2.485→4.304GB, swap +951MB. llama-cpp-python state cache는 약 297MiB scores 복사본을 표시 용량에서 제외할 수 있음 | RPi 패키지 버전·소스 확인 후 cache **0/64**를 clean reboot에서 각 20회 비교. 원인 확정 전에는 누수로 단정 금지 |
| P0 | **M2 스텁이 M5 측정을 오염** | solo-M2 warning 1,347/3,606(37.4%), 고정 심박 118, 유휴 중 약 11초마다 M5 | 검증 모델 전까지 성능/M5 메모리 측정에서는 M2 off 권장. 파일·모델 내부는 담당자 합의 없이 수정 금지 |
| P0 | **기본 Core profile에서 Phase 2가 최대 30초 헛대기** | Core 5개에는 audio/TTS가 없지만 API는 TTS 15초+STT 15초 대기 | `VOICE_ENABLED=false`이면 Phase 2를 즉시 건너뛰고 1차 알림으로 fail-open |
| P0 | **단일 마이크와 node별 Phase 2 라우팅 불일치** | audio-sensing 기본 node=1, API는 emergency와 같은 node transcript만 수용 | 단일 마이크 MVP는 Phase 2 전역 직렬화·활성 node 귀속. 새 Redis key/계약은 구현 전 확인 |
| P0 | **M5 부재 시 critical 경보 경로 없음** | M5 OOM/warmup이면 `ai:emergency` 미생성 가능 | 명확한 fall/vital crisis의 규칙 기반 1차 경보 후 M5는 설명·문맥 보강 역할로 분리 |
| P0 | **복구 시 stale/누락 가능** | experts는 CSI `0-0` 재생, API alert worker는 `$` 시작, M5는 drain 후 cooldown 후보 폐기 | bounded warm window·alert bounded replay/dedupe·cooldown pending을 각각 fault test 뒤 적용 |
| P1 | **CSI hot path가 아직 packet 단위** | M1은 전역 5Hz로 조정했지만 M2 executor·위험도·latest SET은 여전히 300pkt/s 경로. solo-M2만 ai-experts CPU 47% | 입력 deque는 100Hz 유지, M2 계약 확정 후 decision tick을 분리 |
| P1 | **Redis health/latest 쓰기 과다** | sensing은 패킷마다 XADD+SET+HSET+EXPIRE, experts latest도 packet 단위 | health/last_seen 1Hz, latest는 실제 새 추론 때만, snapshot XADD+minute aggregate pipeline 비교 |
| P1 | **M5 batch thread는 제한되지 않음** | `n_threads=2`만 전달하며 라이브러리 기본 `n_threads_batch`는 전체 CPU 사용 가능 | cache off 상태에서 2/2→3/2→3/3 순으로 전체 p95·CPU PSI 비교 |
| P1 | **기동·OOM 재시작이 메모리 압박을 증폭** | experts/qwen warmup 약 62초 중첩, peak 7.416GB; OOM 22회 재시작 이력 | PSS/Anonymous 계측 먼저. post-change startup peak>7GB일 때만 순차 warmup; qwen 제한 재시작은 direct fallback 이후 |

운영체제 관점에서 **보류/비권장**: cpuset/isolcpus, `SCHED_FIFO/RR`, IRQ affinity, M4 4 threads,
performance governor 상시 고정, zram/zswap 상시 활성. peak에서 패킷 손실은 0.08%, 온도 73.8°C,
throttling 0이었고 M4는 2T 4.43초와 4T 4.45초로 4T 이점이 없었다.

다음 측정 도구에는 RSS 합계 외에 PSS/Anonymous/Private Dirty/SwapPss, `pswpin/pswpout`, major fault,
cgroup `memory.events`·`memory.swap.current`·`cpu.stat`, Redis commandstats, M5 source ID와
gate→start→complete 시간을 추가한다. 기존 `solo-m1`·`solo-m4`·`solo-m5`·`base-idle`은 OOM/잔여
호출에 오염됐으므로 CPU·메모리 최적화의 절대 기준으로 재사용하지 않는다.

예상 자원 범위와 합격선은 `handoff/rpi5-20260917/RESOURCE_BUDGET.md`, 변경 상세와 측정 순서는
`handoff/rpi5-20260917/TUNING_PROPOSAL.md` 5절을 따른다. 예상 운영 메모리는 5.8~6.4GB,
p95 6.4~6.8GB이나 **변경 후 실측값이 아니다.**

---

## 1. 현재 상태 (2026-09-17 17:15)

### 저장소

| 위치 | 브랜치 / 커밋 | 비고 |
|---|---|---|
| GitHub `origin/develop` | 이 문서를 갱신한 커밋 | PR #3(M1·M2), PR #4(M4) 병합 + 통합 리팩토링 5개 커밋 포함 |
| 노트북 `C:\rp5` | `develop`, 원격과 동일 | 로컬 Docker 수치는 판단에 쓰지 않는다 |
| RPi5 `~/safewave` | `develop` `5b30f96`, 워킹트리 깨끗 (09-17 17:20 확인) | 통합 리팩토링은 **아직 pull하지 않음**. 받으면 이미지 재빌드가 필요하고 측정 기준 코드가 바뀐다. 다음 모델 업데이트와 함께 받고 재측정. 기존 `stash@{0,1}` 보존 |

### RPi5

| 항목 | 값 |
|---|---|
| 접속 | `ssh csi@192.168.1.2` (노트북 `id_ed25519` 키 등록 완료) |
| 주의 | IP가 DHCP로 바뀜. 예전 `192.168.0.13`/`rp5` 계정은 무효. 노트북 `~/.ssh/config`는 아직 옛 항목 |
| 하드웨어 | Raspberry Pi 5 Model B Rev 1.1, 8 GB, 4코어, NVMe 234 GB (**사용 33%**, 빌드 캐시 88 GB 정리) |
| OS / Docker | Debian 13 기반, kernel 6.12.75+rpt-rpi-2712 / Docker 29.4.3, Compose v5.1.3 |
| 부팅 | **콘솔(`multi-user.target`)** — 09-17 데스크톱 GUI 해제. 되돌리기: `sudo systemctl set-default graphical.target` |
| `.env` (Git 미추적) | `AI_DOCKER_TARGET=cpu-runtime`, **`M4_KO_STT_MODEL=whisper_onnx_int8_ft_svc`**. `M1_MAX_NODES`·`M1_FALL_THRESHOLD`는 삭제(코드 기본 5·0.80). 백업 `~/backup/.env.bak-20260917*` |
| 상태 (17:10) | **ai-qwen 정지**(아래 참고), 나머지 5개 실행. 정지 직후 메모리 사용 3.2 GB·스왑 2,047 MB(가득) |
| ESP32 | 노드 1·2·3 연결, 합계 약 300 pkt/s. 09-17 전원 재시작 후 손실 0.3~4.5% (5절 1번) |
| stash | `stash@{0}`: 09-17 pull 전 보관한 노진산 수정(원격과 동일, drop 가능). `stash@{1}`: 팀원의 미커밋 수정(ORT 스레드 제한 실험 + M1 sigmoid) — 삭제하지 말고 팀원과 정리 |

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

측정은 모두 끝났고 백그라운드 작업은 없다.

**ai-qwen은 17:09에 일부러 정지해 두었다.** 부하를 주지 않은 상태에서도 M2 스텁이 계속 critical을 내서
M5가 약 11초마다 호출됐다(`qwen_invoked`, `risk_score` 0.6506 고정). 그 결과 ai-qwen RSS가 약 5.2 GB까지 커져
전역 OOM으로 17:05·17:08에 연속 강제 종료됐다(재시작 4회). 조정안 A2(캐시)·M2 스텁 항목을 반영하기 전에는
켜 두지 말 것. 다시 켜기: `docker compose start ai-qwen`. 스왑이 가득 찬 상태라 측정 전에는
`sudo swapoff -a && sudo swapon -a`(사람이 실행) 또는 재부팅으로 비우고 시작한다.

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

> **다음 작업자 주의:** 이 절은 09-17 당시의 원래 측정 절차·기록을 보존한 것이다. 위의 통합
> 수정은 `develop`에 있지만 아직 RPi5에 배포되지 않았다. 다음 모델 업데이트 커밋과 함께 RPi5에서
> pull·재빌드한 뒤 `RESOURCE_BUDGET.md` 6절 순서로 재측정한다.

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

09-17 기본값 결과는 부하·OOM 포함 그대로 확정한다. 아래는 당시 후보 목록이다. 현재 로컬 상태는
**A1 완료, A2·B1·C1·C2·D·E1·H 반영, A3는 Compose만 반영/커널 미반영**, E2·F·G 미반영이다.
이 표를 현재 TODO로 해석하지 말고 상단의 다음 Claude 인계를 기준으로 한다.

| 번호 | 항목 | 종류 | 근거 (09-17) |
|---|---|---|---|
| A1 | RPi5 데스크톱 GUI 해제 (콘솔 부팅) | 시스템 설정 — **사용자가 직접 실행** | OOM 촉발 `wf-panel-pi`, OS·데스크톱 약 1.2 GB |
| A2 | ai-qwen 프롬프트 캐시 256 → 64 MB (`QWEN_GGUF_CACHE_MB`) | 설정 | ai-qwen 2.0 → 3.3 GB 증가 후 OOM |
| A3 | 커널 메모리 cgroup 활성 + 컨테이너 메모리 상한 | 시스템 설정 + 재부팅 (선택) | 전역 OOM → 스왑 폭주 → sensing까지 정지 |
| B1 | **[반영]** M1 전역 200ms 추론·창끝 게이트·K=3/N=5 | 코드 | M1 약 5 ms × 300 pkt/s → backlog 건너뜀 → 노드 버퍼 리셋 → **0 입력 고정**. 현재는 100Hz 격자 zero-fill과 필수 노드 1·2·3 게이트로 조정했지만 RPi5 재측정 전 |
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
| 2 | M1·M2 김태연 — **09-17 병합 완료** | PR #3 → `develop` `be5f00b` (`feature/M1-M2` 유지) | M1: 인계 모델 배치(노트북·RPi5), 조건부 시그모이드 삭제·임계값 `M1_FALL_THRESHOLD` 0.80 (`f5dc6cc`), RPi5 `.env`의 `M1_MAX_NODES=1`·`M1_FALL_THRESHOLD=0.7` 삭제. 오프라인 확인 통과(슬롯 3·4 무영향 max|diff| 0). **실 파이프라인에서는 입력 창이 안 차서 전부 0 입력만 추론** → 3-7 B1로 일괄 조정 때 해결. M2: 파일만 병합, 배선 보류(인계 지시) |
| 3 | M3 소민섭 — v3.4 (09-13 배포 결정) | 아직 인계 브랜치 없음 (`origin/feature/ast-base`는 5월 것) | HF 포맷 + `preprocessor_config.json` + `id2label`. **log-Mel 전처리 구현**, **라벨은 이름으로 매핑**(인덱스 매핑 시 7종↔6종 불일치), 운영 임계값 0.6. 계획서의 오탐 4.03회/h는 09-11 held-out 3.97시간 기준(8/30의 1.51회/h는 평가 녹음 일부가 학습에 섞인 낙관 편향) |

병합 뒤 반드시: `emergency_score` 경계 테스트 회귀, 5노드/1노드 주입 시 expert 오류 0, 점수 범위 0~1.

## 4-1. 다음 작업 — 통합 파트 리팩토링 (Codex 시작점)

> **상태 갱신:** 이 절에서 지목한 1차 통합 리팩토링은 로컬 작업 트리에 반영됐다. 다음 Claude는
> 같은 점검을 처음부터 반복하지 말고 상단 변경표와 `git diff`를 검토한다. 다음 구현 후보는
> M5 우회 1차 경보, service profile 축소, 선택 모델 로드이며 모두 노진산 결정 후 진행한다.

목표: 통합 파트의 누수·오류·허점을 점검하고 클린 코드로 정리한다. 모델 내부(M1~M4 가중치·전처리)는 담당자 영역이라 건드리지 않는다.
진행 방식: **점검 목록을 먼저 만들어 노진산에게 보여주고, 승인된 항목만 고친다.** 조정안 반영은 노진산이 결정한다.

| 대상 | 줄 수 | 먼저 볼 것 |
|---|---|---|
| `ai/main.py` | 940 | CSI 루프(패킷마다 M1 → 백로그 스킵 → 버퍼 리셋, 조정안 B1), M1 워밍업 형상(5절 3번), 오디오 워커 순서·밀림(C1·C3), M2 스텁 배선 |
| `ai/qwen_service.py`, `ai/logic/qwen_15b.py`·`qwen_gguf.py` | 178 / 931 / 147 | 호출 간격, 메모리 증가(A2), `_apply_context_window`가 critical을 강제하는지(이전 리뷰에서 미수정으로 기록, 확인 필요) |
| `api/main.py` | 917 | `_alert_worker`의 블로킹 호출(이전 리뷰에서 미수정으로 기록, 확인 필요), `/system/resources`의 0.5초 대기 |
| `sensing/main.py` | 153 | 손실 카운터(09-17 uint32 처리 수정 완료) |
| 공통 | — | `_log` 함수가 `ai/main.py`·`ai/qwen_service.py`·`api/main.py`에 각각 정의됨(중복). 서비스 간 import 금지라 공유 모듈로 합칠지는 결정 필요 |

지켜야 할 것: Redis 키·TTL·스트림 구조를 바꾸려면 먼저 확인. `ThreadPoolExecutor`·타임아웃 패턴은 임의로 교체하지 않는다.
M1 인계 지시(`_preprocess`·슬롯 조립·임계값 0.80·`.onnx` 무수정, M2 파일 무수정)는 계속 유효하다.
검증: `tests/`의 경계·파이프라인 테스트 회귀, RPi5에서는 조정안 반영 뒤 같은 조건으로 1회만 재측정(`scripts/bench_rpi5.py`).

참고: 노트북 `C:\rp5\.claude\worktrees\ecstatic-sanderson-5d7f73\`에 예전 작업 트리 사본이 있다. 전체 검색 때 결과가 중복되니 제외할 것.

---

## 5. 알려진 문제 · 결정 필요

1. **노드 1 간헐적 대량 손실 이력.** 09-17 오후 노드 1이 18~35% 손실, 최대 1초 공백(RSSI는 -26dBm로 양호).
   ESP 3대 전원 재시작 후 0.3~1.4%, 공백 10ms로 회복(노드 2·3은 2~4.5%, 노드 3 RSSI -35~-38dBm).
   현재 코드 기본은 `M1_MAX_NODES=5` 고정 슬롯이다. 노드 1이 다시 나빠지면 해당 슬롯 버퍼가 초기화될 수 있다.
   재발하면 신호보다 전원·발열·펌웨어를 먼저 의심. 측정 중 손실은 `bench_rpi5.py` 결과의 노드 표로 기록한다.
2. **M3·M4 대기열 누적 — 로컬 코드 조정 완료/RPi5 미검증.** 최신 1건 병합, M4 우선,
   Phase 2 중 M3 생략을 반영했다. 이벤트→결과 p50 30.7초/최대 49초가 15초 안으로 들어오는지 다시 잰다.
3. **M1 워밍업 버그 — 로컬 해결/RPi5 미검증.** 운영과 같은 `(1,M1_MAX_NODES,64,100)`으로 바꿨다.
4. **RPi5 `stash@{1}` 정리.** sigmoid는 develop에 반영됨, ORT 스레드 제한은 develop의 `get_session_opts`와 중복,
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
