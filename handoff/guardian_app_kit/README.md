# SafeWave 보호자 앱 개발 키트 (Android)

> **2026-10-13 상태 메모:** 이 키트의 계약·과거 요청은 그대로 둔다. 10-06까지 서버에 들어오지 않던 확인(ack)은 10-12 **노트북 GPU 라이브**에서 긴급 음성→경보 0.92초→FCM 발송 시도 1.00초→앱 확인 서버 기록 6.73초로 확인됐다. 10-13 재생은 6초 뒤 ack 자동 주입이므로 별도 실기기 확인이 아니다. RPi5의 ack·TTS 경로는 미검증이다. 앱의 기대 노드 수 3→5 설정도 남아 있다. 최신 근거는 [`docs/validation_status.md`](../../docs/validation_status.md).

작성 2026-09-23 · 노진산(통합) → 앱 담당 소민섭
서버 기준: `jinsan02/rp5` `develop` (api/main.py, api/notifier.py에서 확인한 계약)

## 이 키트로 하는 일
1. `docs/`를 읽고 결정 사항(`docs/05_OPEN_DECISIONS.md`)을 정한다.
2. 새 저장소 `safewave-guardian-android`를 만들고 `structure/PROJECT_STRUCTURE.md` 구성대로 시작한다.
3. `templates/AGENTS.md`를 새 저장소 루트에 복사한다(Claude Code를 쓰면 같은 내용으로 `CLAUDE.md`도).
4. `docs/`, `mock/`을 새 저장소에 그대로 복사하고, `structure/PROJECT_STRUCTURE.md`도 새 저장소 `docs/`에 넣는다.
5. `prompts/01_KICKOFF.md`를 AI 코딩 에이전트에 넣고, 이후 `prompts/02_PHASES.md` 순서로 진행한다.

## 구성

| 경로 | 내용 |
|---|---|
| `docs/00_FRAMEWORK_DECISION.md` | 프레임워크 비교와 추천(**Kotlin + Jetpack Compose**) |
| `docs/01_FEATURE_SPEC.md` | 기능 명세 — 필수(MVP) / 권장 / 향후 / 제외, 완료 기준 |
| `docs/02_API_SPEC.md` | 엔드포인트·FCM 계약(현재 + 예정), 응답 예시 |
| `docs/03_ANDROID_ALERT_RELIABILITY.md` | 잠금·무음·방해금지·절전 상태에서 알림을 보이게 하는 구현 방법과 실기기 확인 항목 |
| `docs/04_SERVER_CHANGES.md` | 앱을 위해 서버(통합 파트)가 바꿔야 할 것. **S0은 필수** |
| `docs/05_OPEN_DECISIONS.md` | 결정 필요 사항 |
| `docs/06_LEGACY_SPEC_AUDIT.md` | 예전 「고신뢰성 긴급 사고 전파」 문서 검토 결과(살린 것·뺀 것·이유) |
| `structure/PROJECT_STRUCTURE.md` | 앱 저장소 디렉토리·패키지 구성 |
| `templates/AGENTS.md` | 앱 저장소용 에이전트 규칙 |
| `prompts/01_KICKOFF.md`, `02_PHASES.md` | 개발 프롬프트(시작 + 단계별) |
| `mock/*.json` | 서버 없이 개발하기 위한 응답·푸시 예시 |
| `TO_APP_20261005.md` | **실서버 연동 전달**(키·설정 준비, 연동 순서, 앱 쪽 요청) — 10-05 경보 확인 API 포함 |
| `TO_APP_20261006.md` | 10-06 추가 요청·당시 ack 미수신 기록. 10-12 노트북 GPU 라이브 확인 이후 상태는 위 메모 참조 |
| `TO_APP_20261013.md` | 최신 서버 오류 계약, 노트북 사전 확인 결과, RPi5에서 함께 확인할 항목 |
| `APP_INTEGRATION_TEST_20261013.md` | 앱 등록부터 RPi5 긴급 음성→푸시→ack까지의 실행 절차와 중단 조건 |

## 가장 중요한 세 가지
1. **응급 알림은 푸시만으로 완결돼야 한다.** 보호자 폰은 대부분 집 밖이라 RPi5 API에 닿지 않는다.
2. **서버는 data 전용 푸시를 지원한다(S0 완료, `FCM_DATA_ONLY`).** 기본값은 시스템 알림 형식이라,
   **앱 배포와 같은 날 서버를 `FCM_DATA_ONLY=true`로 바꿔야** 전체 화면 경보·반복 사이렌이 동작한다.
3. **"보장", "우회", "95%" 같은 표현을 쓰지 않는다.** 실기기(플립4)에서 확인한 것만 완료로 적는다.
