# 목 데이터

서버 없이 개발·테스트하기 위한 예시. 값은 형식 설명용이며 실측값이 아니다.

| 파일 | 대응 |
|---|---|
| `root.json` | `GET /` |
| `register_token.json` | `POST /auth/register-token` |
| `auth_tokens.json` | `GET /auth/tokens` |
| `notify_test_ok.json` / `notify_test_error.json` | `POST /notify/test` 200 / 500 |
| `status.json` / `status_no_data.json` | `GET /status` (데이터 있음 / 없음) |
| `history.json` | `GET /history` (rule·qwen·fallback 예시) |
| `nodes_health.json` | `GET /nodes/health` |
| `system_health.json` / `system_health_503.json` | `GET /system/health` 200 / 503 |
| `settings.json` | `GET /settings` |
| `charts_minute.json` | `GET /charts/minute` |
| `ws_ping.json` | 웹소켓 유지 신호 |
| `app_summary.json` / `app_summary_no_data.json` | `GET /app/summary` |
| `feedback_ok.json` | `POST /alerts/{msg_id}/feedback` |
| `unregister_token.json` | `DELETE /auth/register-token/{device_id}` |
| `fcm_heartbeat.json` | 정상 동작 신호 |
| `fcm_emergency_legacy.json` | `FCM_DATA_ONLY=false`(현재 기본) 응급 푸시 |
| `fcm_emergency.json` | `FCM_DATA_ONLY=true`(앱 배포 후) 응급 푸시 |
| `fcm_voice_ok.json`, `fcm_test.json` | 응답 확인, 테스트 |
| `*_planned.json` | 아직 서버에 없는 예정 API·푸시(S2 확인 기능) |

주의: `status.json`의 `experts.vital`, `experts.speech_ko.transcript_ko`는 형식 확인용이며 앱에서 표시하지 않는다.
