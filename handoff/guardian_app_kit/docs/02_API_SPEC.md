# 엔드포인트·푸시 명세

기준: 서버 저장소 `develop`의 `api/main.py`, `api/notifier.py` (최초 2026-09-23 확인, 경보 확인 API 10-05 반영, 10-13 상태 주석). 인증 없음.
기본 주소 `http://<RPi5>:8000`. 앱은 모든 응답에서 **알 수 없는 필드를 무시**하고 누락 필드는 기본값을 쓴다.

> 10-13 주의: `APP_EXPECTED_NODES`의 서버 기본값은 여전히 `1,2,3`이다. 노드 5대 시연에서는 서버 배포 설정을 `1,2,3,4,5`로 맞춰야 `/app/summary.nodes_expected`가 5가 된다. 10-12 노트북 GPU 라이브에서 앱 ack 서버 기록을 확인했지만, RPi5에서 같은 경로가 확인된 것은 아니다. 새 `/monitor/recent`는 녹화·진단용이며 M4 전사 원문을 포함하므로 앱 화면·저장소에 그대로 쓰지 않는다.

## 1. 통신 경로

| 경로 | 방향 | 도달 조건 | 용도 |
|---|---|---|---|
| FCM 푸시 | 서버 → 앱 | 양쪽 인터넷. 폰이 집 밖이어도 도착 | 응급, 응답 확인, 테스트, 정상 동작(서버 설정 시) |
| REST | 앱 → 서버 | 같은 LAN 또는 원격 접속(결정 D1) | 등록, 조회, 확인·신고 |
| WebSocket | 서버 → 앱 | 위와 같음 | 실시간 상태(P1) |

## 2. FCM 계약

서버 환경변수 `FCM_DATA_ONLY`로 두 형식 중 하나를 보낸다. data 값은 **모두 문자열**이다.

| 형식 | 서버 설정 | 앱 백그라운드·종료 시 |
|---|---|---|
| 시스템 알림(notification + data) | `FCM_DATA_ONLY=false` (**현재 기본값**) | 시스템이 직접 표시, `onMessageReceived` 미호출 → 전체 화면 경보·반복 사이렌·로컬 저장 불가. 탭하면 data가 intent extras로 전달 |
| **data 전용**, `android.priority=high`, TTL 600초 | `FCM_DATA_ONLY=true` | 항상 `onMessageReceived` 호출 → 앱이 알림을 직접 만든다 |

**앱 MVP 배포와 같은 날 서버를 `FCM_DATA_ONLY=true`로 바꾼다.** 앱은 두 형식을 모두 처리한다.

### 2-1. data 필드 (서버 구현 완료, 2026-09-23)

| type | 채널 | 필드 (모두 문자열) |
|---|---|---|
| `emergency` | `emergency_alarm` | `msg_id`, `ts_ms`, `node_id`, `risk_score`, `risk_level`(critical), `emergency`("True"), `summary`, `slm_mode`(rule·qwen·fallback) |
| `voice_ok` | `safety_alert` | `msg_id`, `node_id`, `ts_ms`, `emergency`("False") |
| `heartbeat` | `safety_alert` | `nodes_online`, `nodes_expected` (서버 `HEARTBEAT_INTERVAL_SEC` > 0일 때) |
| `test` | `safety_alert` | — |
| `ack` | `safety_alert` | `msg_id`, `device_id`(확인한 보호자), `action`(seen·called), `ts_ms` — 다른 보호자가 확인했을 때. 확인한 본인에게는 가지 않는다(10-05 구현) |

data 전용 형식에는 `title`, `body`가 data 안에 함께 온다(시스템 알림 형식에서는 notification에 있음).

- `msg_id` = `ai:emergency` 스트림 ID = `/history`의 `_id` = 피드백 API 경로의 `{msg_id}`.
- 구버전 호환: `type`이 없고 `risk_level`="critical"이면 `emergency`로 처리한다.
- `voice_ok`는 `msg_id`로 경보와 연결한다(없으면 같은 `node_id`의 가장 가까운 `ts_ms`).
- 예시: `mock/fcm_emergency.json`(data 전용), `mock/fcm_emergency_legacy.json`(시스템 알림 형식).

## 3. REST — 앱이 쓰는 것

### `GET /`
`{"service":"rp5-api","status":"ok"}` — 연결 확인.

### `POST /auth/register-token`
요청 `{"token":"<fcm>","device_id":"<uuid>"}` → `{"status":"registered","device_id":"<uuid>","ttl_seconds":3600}`. 빈 토큰·잘못된 `device_id`는 422.
- 서버 저장 TTL 1시간, 서버가 살아 있으면 10분마다 연장. 앱은 시작·`onNewToken`·30분 주기로 재등록(멱등).
- `device_id` 기본값 `galaxy_flip4`를 쓰지 말 것(여러 보호자가 서로 덮어씀).

### `GET /auth/tokens`
`{"devices":["<uuid>",...],"count":2,"fcm_ready":true}`. 토큰 값은 반환하지 않는다.

### `POST /notify/test`
요청 `{"token":"<fcm>"}` → `{"ok":true,"message_id":"..."}`. 빈 토큰은 422. 실패 응답은 아래 6절의 상태코드와 고정 `error` 값을 따른다(원래 Firebase 오류 문구·키 경로는 반환하지 않음).

### `POST /notify/test-registered`
모니터 화면용. 등록된 모든 기기에 일반 테스트 알림을 1회 보내며 토큰을 브라우저에 노출하지 않는다.
성공 응답은 `{"ok":true,"registered":1,"sent":1,"failed":0}`. 등록 기기가 없으면 409
`no_registered_devices`, Firebase 미설정은 503 `fcm_unavailable`, 전부 실패하면 502 `fcm_send_failed`.
200은 Firebase 발송 접수 결과이며 휴대폰 표시 성공 증거가 아니다.

### `POST /notify/test-emergency-registered`
모니터 화면용 응급 표시 시험. 등록된 모든 기기에 `type=emergency`, `test=1`,
`speech_text=살려줘`를 보낸다. 제목과 본문에도 `[시험]`과 “실제 사고가 아닙니다”를 표시한다.
성공 응답은 `{"ok":true,"registered":1,"sent":1,"failed":0,"test":true,"speech_text":"살려줘"}`.
이 API는 `ai:emergency`나 Phase 2를 생성하지 않으며, 감지 파이프라인이 아닌 앱의 응급 푸시 표시만 검증한다.
오류 상태와 200의 주장 범위는 일반 등록 기기 테스트 API와 같다.

### `GET /status`
최신 스냅샷(`mock/status.json`). 데이터가 없으면 **200** `{"message":"no data yet"}`.
사용 필드: `ts_ms`, `node_id`, `risk_score`, `risk_level`(normal|warning|critical), `rule_alert`, `slm_mode`, `qwen_reason`,
`experts.fall.{fall_score,fall_detected,fall_votes,fall_vote_samples,input_status}`, `experts.env_sound.{label,confidence}`,
`experts.speech_ko.{speech_detected,keywords}`, `models`.
표시 금지: `experts.vital`, `experts.speech_ko.transcript_ko`. `ts_ms`가 10초 이상 오래되면 "데이터 지연".

### `GET /history?n=100&level=critical|warning`
`n` 1~3600, 최신순 배열(`mock/history.json`). 항목: `ts_ms`, `node_id`, `risk_score`, `risk_level`, `emergency`,
`summary`, `slm_mode`(rule|qwen|fallback), `qwen_reason`, `_id`(=msg_id), `_ts`(초). `level=warning`은 warning+critical.

### `GET /nodes/health`
`node_1`~`node_6` 고정 키(`mock/nodes_health.json`). `status` online|offline, `age_s`, `loss_rate`, `rx`, `lost`, `rssi`.
설치되지 않은 노드도 offline으로 나온다 → 설정의 설치 노드 목록(기본 1,2,3)만 표시.

### `GET /system/health`
200 `{"status":"ok|degraded","redis":{...},"ts_ms":...}` / 503 `{"status":"degraded","redis":{"connected":false}}`.

### `GET /settings`
`{"risk_threshold":0.6,"active_nodes":[1,2,3,4,5,6],"ai_enabled":true,"models":{"m1":true,"m2":false,"m3":true,"m4":true,"m5":true}}`
MVP는 읽기만. `ai_enabled=false` 또는 `models.m1=false`면 홈에 "감시 일부 꺼짐" 경고.

### `GET /app/summary` (구현 완료)
홈 요약(`mock/app_summary.json`). 음성 전사·생체신호는 없다.
`{"risk_level","risk_score","data_age_s","nodes_online","nodes_expected","server":"ok|degraded","fcm_ready",
"last_alert":{"msg_id","ts_ms","node_id","summary","slm_mode"}|null,"monitoring":{"ai_enabled","m1"},"ts_ms"}`
- 데이터가 없으면 `risk_level`·`risk_score`·`data_age_s`가 null. `data_age_s` ≥ 10이면 "데이터 지연".
- `nodes_expected`는 서버 `APP_EXPECTED_NODES`(기본 1,2,3). `fcm_ready=false`면 "서버에서 알림을 보낼 수 없음" 경고.

### `POST /alerts/{msg_id}/feedback` (구현 완료)
요청 `{"device_id":"<uuid>","feedback":"false_alarm|missed_alert"}` → `{"ok":true,"msg_id","feedback"}`, 그 외 값은 422.
`msg_id` 형식이 틀리면 400 `{"error":"invalid msg_id"}`.
- 서버는 기존 M5 피드백 키에 기록해 **이후 1시간 동안 M5(AI 판단) 점수를 0.08 낮추거나(false_alarm) 높인다(missed_alert).**
  규칙 경보(낙상 확정 등)에는 영향이 없다. 앱은 오탐 신고 확인 창에 이 영향을 짧게 안내한다.
- 가장 최근 신고 1건만 유효하다(여러 번 신고해도 누적되지 않음).

### `DELETE /auth/register-token/{device_id}` (구현 완료)
`{"status":"removed"|"not_found","device_id"}` — 로그아웃·앱 초기화 시.

### `GET /history?before=<msg_id>` (구현 완료)
`before`보다 오래된 항목부터 `n`건(필터 적용 후). 다음 페이지는 마지막 항목의 `_id`를 `before`로 준다. 빈 배열이면 끝.
`before`가 스트림 ID 형식이 아니면 400 `{"error":"invalid before msg_id"}`.

### `GET /charts/minute?minutes=60` (P1)
`[{"ts":<초>,"risk_score_avg","heart_rate_avg","breathing_rate_avg","slm_invoked_count","samples"}]`

### `WS /ws/monitor` (P1)
메시지는 스냅샷(`/status`와 같은 형식) 또는 `{"ping":true}`. 노드별 최신을 최대 4Hz로 보내고 위험 수준 변화·critical은 즉시.

## 4. REST — 앱이 쓰면 안 되는 것
`POST /notify/send`, `POST /notify/check`(임의 푸시 가능, 보호 예정), `GET /logs`(대용량), `POST /audio/events`,
`POST /settings`(전체 교체, MVP 제외), `GET /system/resources`, `GET /system/redis-memory`, `GET /emergency/clip/{ts_ms}`(메타데이터만).

## 5. 경보 확인 (S2, 2026-10-05 구현)

### `POST /alerts/{msg_id}/ack`
요청 `{"device_id":"<uuid>","action":"seen|called"}` → `{"ok":true,"msg_id","acked_by":[{"device_id","action","ts_ms"}]}`
- `seen` = 경보를 봤음, `called` = 대상자·119에 전화함. 같은 기기에서 `called` 뒤의 `seen`은 무시된다(내려가지 않음). 같은 동작을 다시 보내도 처음 시각을 유지한다(멱등).
- 처음 기록되거나 `seen`→`called`로 바뀔 때만 다른 보호자에게 `ack` 푸시를 보낸다.
- `device_id`는 영문·숫자로 시작, `[A-Za-z0-9._:-]` 64자 이하. 그 외·잘못된 `action`은 422. `msg_id` 형식이 틀리면 400, 없는 경보는 404.
- 기록은 1시간(TTL 3600) 뒤 사라진다. 그 뒤 확인 기록은 앱 로컬 기록을 쓴다.

### `GET /alerts/{msg_id}`
`/history` 항목 + `acked_by` + `voice_ok`. 없으면 404.
- `voice_ok`: `true` = 대상자가 음성으로 괜찮다고 응답, `false` = 무응답·도움 요청, `null` = 음성 확인을 하지 않음(서버 `VOICE_ENABLED=false`) 또는 진행 중(경보 뒤 약 30초).

### `/history` 항목에 `acked_by`
모든 항목에 `acked_by` 배열이 붙는다(없으면 `[]`).

### 아직 없는 것
| 기능 | 상태 |
|---|---|
| 인증 `X-API-Key`(S6) | 결정 대기(D5). 지금은 인증 없음 → LAN 시연 전용 |
| 미확인 재발송(S10) | S2 이후 예정 |

## 6. 오류 처리 규칙

| 상황 | 서버 응답·앱 처리 |
|---|---|
| 연결 실패·타임아웃(HTTP 응답 없음) | 마지막 값을 오래된 값으로 표시하고 재시도. 새 경보 푸시 수신을 보장한다고 표시하지 않음 |
| 200 `{"message":"no data yet"}` | 센서 데이터 대기 중(오류 아님) |
| 400 `invalid msg_id`·`invalid before msg_id` | 요청 ID/커서 수정. 같은 요청 그대로 재시도하지 않음 |
| 404 `alert not found` | 경보가 없거나 이력에서 밀림. 이미 구현된 API이므로 "준비 중"으로 처리하지 않음 |
| 409 `fcm_project_mismatch` (`/notify/*`) | 폰 토큰·서버 Firebase 프로젝트 불일치 확인. 반복 전송하지 않음 |
| 410 `fcm_token_unregistered` (`/notify/*`) | 앱에서 새 FCM 토큰을 얻어 재등록한 뒤 재시험 |
| 422 | 요청 JSON/필드 검증 오류(`detail`) 또는 `invalid_fcm_request`. 입력을 수정한 뒤 재시험 |
| 502 `fcm_send_failed` (`/notify/*`) | FCM 전송 실패. 서버 로그 확인, 짧은 간격의 무한 재시도 금지 |
| 503 `redis_unavailable` | Redis 의존 API 사용 불가. 상태 저하 표시 후 간격을 두고 재시도 |
| 503 `fcm_unavailable` (`/notify/*`) | Firebase 초기화·서버 키 상태 확인. 토큰을 반복 등록해도 해결되지 않음 |
| 500 `internal_error` | 서버 내부 오류. 본문 상세를 사용자에게 그대로 노출하지 않음 |

`/notify/check` 실패에는 `sent:false`도 붙는다. 200·`message_id`는 FCM 발송 API가 요청을 수락했다는 뜻이며 **폰에 실제 표시됐다는 증거는 아니다**. `/system/health`의 Redis 장애는 503 `{"status":"degraded","redis":{"connected":false,"error":"redis_unavailable"}}`다.
