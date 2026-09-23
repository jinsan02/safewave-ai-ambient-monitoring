# 앱을 위해 서버(통합 파트)가 바꿀 것

담당: 노진산. 서버 저장소 규칙: Python만, 운영 데이터는 Redis(TTL ≤ 3600초), **새 Redis 키는 확인 후 추가**,
`feature/*` 브랜치 커밋 금지, `develop`에서 작업.

| ID | 우선 | 내용 | 파일 | Redis | 상태 |
|---|---|---|---|---|---|
| **S0** | **필수** | 응급 푸시를 **data 전용 + `android.priority=high`**로 변경(제목·본문도 data에). 응답 확인·테스트도 data 전용으로 통일 | `api/notifier.py` `_send_fcm`, `send_risk_notification`, `send_voice_ok_notification` | 없음 | **완료** — `FCM_DATA_ONLY`(기본 false, 앱 배포일 true) |
| **S1** | **필수** | data에 `type`, `msg_id`(ai:emergency 스트림 ID), `slm_mode` 추가. `voice_ok`에도 `msg_id` | `api/main.py` `_handle_single_emergency` | 없음 | **완료** |
| S2 | MVP 권장 | `POST /alerts/{msg_id}/ack`, `GET /alerts/{msg_id}`, `/history` 항목에 `acked_by` | `api/main.py` | **새 키** `alert:ack:{msg_id}` Hash(`device_id`→`action:ts`), TTL 3600 | 확인 필요 |
| S3 | MVP 권장 | 오탐·미탐 신고 → 기존 `mqtt:feedback:last`(TTL 3600) → M5 점수 ±0.08 보정(기존 로직) | `api/main.py` | 기존 키 | **완료** — `POST /alerts/{msg_id}/feedback`, 1시간 M5 보정, 규칙 경보 무관 |
| S4 | P1 | `GET /app/summary`(기존 키 조합, transcript 제외) | `api/main.py` | 없음 | **완료** |
| S5 | P1 | `DELETE /auth/register-token/{device_id}` | `api/main.py` | 기존 키 삭제 | **완료** |
| S6 | P1 | `X-API-Key` 인증, `/notify/send`·`/notify/check` 제거 또는 보호, CORS 제한 | `api/main.py`, `api/notifier.py` | 없음 | 결정 필요(D5) |
| S7 | P1 | `/history?before=`, `count=n` 적용 | `api/main.py` | 없음 | **완료** — 필요한 만큼만 200건씩 읽음 |
| S8 | P2 | `SystemSettings.node_names` + 소리 기반 경보 본문에 방 이름 | `api/main.py` | 기존 `sys:settings` | 미착수 |
| S9 | P1 | 정기 `heartbeat` 푸시 | `api/main.py`(주기 task) | 없음 | **완료** — `HEARTBEAT_INTERVAL_SEC`(기본 0=끔, 권장 86400) |
| S10 | P1 | ack 없으면 N분 뒤 재발송(최대 M회) | S2 이후 | S2 키 사용 | S2 이후 |

## 구현 기록 (2026-09-23)
노트북 Docker에서 API를 재빌드해 `/app/summary`, `/history?before=`(경고 항목 건너뛰며 페이지 나눔),
`POST /alerts/{msg_id}/feedback`(잘못된 값 422, 키 TTL 3599초), `DELETE /auth/register-token`을 실제 호출로 확인했다.
FCM 실제 발송은 Firebase 키가 없어 확인하지 못했다.
단위 테스트: data 전용 필드(모두 문자열, title/body 포함), type·msg_id, 홈 요약(설치 노드 집계, 전사 미포함).

## S0 상세 (가장 중요)
- 현재 `_send_fcm`은 `messaging.Notification(title, body)`와 `AndroidNotification(channel_id=...)`을 넣어 보낸다.
  이 형식은 앱이 백그라운드·종료일 때 시스템이 직접 표시하고 앱 코드(`onMessageReceived`)가 실행되지 않는다.
- 변경: `notification` 제거, `data`에 `title`·`body` 포함, `AndroidConfig(priority="high", ttl=600초)`.
- 호환: 앱이 아직 없는 기기에서는 알림이 보이지 않게 되므로, **앱 MVP 배포와 같은 날 적용**하거나
  환경변수(`FCM_DATA_ONLY=true`)로 전환한다.
- 시험: `scripts/alert_e2e_check.py` + 앱 설치 기기에서 앱 종료·잠금 상태 수신.

## 서버 쪽 전제
- `api/auth/firebase_key.json`(서비스 계정 키) 배치. 없으면 API 시작 로그 `fcm_unavailable`.
- 앱의 `google-services.json`은 **같은 Firebase 프로젝트**에서 받는다.
- 원격 접속 방식(D1) 결정 전까지 앱의 REST 기능은 LAN에서만 동작.
