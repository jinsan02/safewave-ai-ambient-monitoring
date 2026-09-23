# 엔드포인트·푸시 명세

기준: 서버 저장소 `develop`의 `api/main.py`, `api/notifier.py` (2026-09-23 코드 확인). 인증 없음.
기본 주소 `http://<RPi5>:8000`. 앱은 모든 응답에서 **알 수 없는 필드를 무시**하고 누락 필드는 기본값을 쓴다.

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
| `ack` | — | **예정(S2)**: `msg_id`, `device_id`, `action`, `ts_ms` |

data 전용 형식에는 `title`, `body`가 data 안에 함께 온다(시스템 알림 형식에서는 notification에 있음).

- `msg_id` = `ai:emergency` 스트림 ID = `/history`의 `_id` = 피드백 API 경로의 `{msg_id}`.
- 구버전 호환: `type`이 없고 `risk_level`="critical"이면 `emergency`로 처리한다.
- `voice_ok`는 `msg_id`로 경보와 연결한다(없으면 같은 `node_id`의 가장 가까운 `ts_ms`).
- 예시: `mock/fcm_emergency.json`(data 전용), `mock/fcm_emergency_legacy.json`(시스템 알림 형식).

## 3. REST — 앱이 쓰는 것

### `GET /`
`{"service":"rp5-api","status":"ok"}` — 연결 확인.

### `POST /auth/register-token`
요청 `{"token":"<fcm>","device_id":"<uuid>"}` → `{"status":"registered","device_id":"<uuid>","ttl_seconds":3600}`
- 서버 저장 TTL 1시간, 서버가 살아 있으면 10분마다 연장. 앱은 시작·`onNewToken`·30분 주기로 재등록(멱등).
- `device_id` 기본값 `galaxy_flip4`를 쓰지 말 것(여러 보호자가 서로 덮어씀).

### `GET /auth/tokens`
`{"devices":["<uuid>",...],"count":2}`

### `POST /notify/test`
요청 `{"token":"<fcm>"}` → `{"ok":true,"message_id":"..."}` / 500 `{"ok":false,"error":"..."}`
(서버에 Firebase 키가 없으면 500)

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
- 서버는 기존 M5 피드백 키에 기록해 **이후 1시간 동안 M5(AI 판단) 점수를 0.08 낮추거나(false_alarm) 높인다(missed_alert).**
  규칙 경보(낙상 확정 등)에는 영향이 없다. 앱은 오탐 신고 확인 창에 이 영향을 짧게 안내한다.
- 가장 최근 신고 1건만 유효하다(여러 번 신고해도 누적되지 않음).

### `DELETE /auth/register-token/{device_id}` (구현 완료)
`{"status":"removed"|"not_found","device_id"}` — 로그아웃·앱 초기화 시.

### `GET /history?before=<msg_id>` (구현 완료)
`before`보다 오래된 항목부터 `n`건(필터 적용 후). 다음 페이지는 마지막 항목의 `_id`를 `before`로 준다. 빈 배열이면 끝.

### `GET /charts/minute?minutes=60` (P1)
`[{"ts":<초>,"risk_score_avg","heart_rate_avg","breathing_rate_avg","slm_invoked_count","samples"}]`

### `WS /ws/monitor` (P1)
메시지는 스냅샷(`/status`와 같은 형식) 또는 `{"ping":true}`. 노드별 최신을 최대 4Hz로 보내고 위험 수준 변화·critical은 즉시.

## 4. REST — 앱이 쓰면 안 되는 것
`POST /notify/send`, `POST /notify/check`(임의 푸시 가능, 보호 예정), `GET /logs`(대용량), `POST /audio/events`,
`POST /settings`(전체 교체, MVP 제외), `GET /system/resources`, `GET /system/redis-memory`, `GET /emergency/clip/{ts_ms}`(메타데이터만).

## 5. 예정 엔드포인트 (서버 작업 — `04_SERVER_CHANGES.md`)
앱은 인터페이스로 먼저 만들고, 404면 "준비 중"으로 처리한다.

| 메서드·경로 | 요청 | 응답 | ID |
|---|---|---|---|
| `POST /alerts/{msg_id}/ack` | `{"device_id","action":"seen|called"}` | `{"ok":true,"acked_by":[...]}` | S2 (새 Redis 키 확인 필요) |
| `GET /alerts/{msg_id}` | — | EmergencySummary + `acked_by`, `voice_ok` | S2 |

인증(S6) 적용 시 모든 요청에 `X-API-Key` 헤더. 값이 비면 헤더를 보내지 않는다.

## 6. 오류 처리 규칙

| 상황 | 앱 표시 |
|---|---|
| 연결 실패·타임아웃(5초) | "서버에 연결할 수 없음(집 밖이거나 서버 정지). 응급 알림은 계속 받습니다" + 마지막 값 |
| `{"message":"no data yet"}` | "센서 데이터 대기 중" |
| 503 | "서버 일부 기능 이상" |
| 404(예정 API) | 버튼 "준비 중", 동작은 로컬 기록 |
| 500(`/notify/test`) | "서버에서 알림을 보낼 수 없음(Firebase 설정 확인)" |
