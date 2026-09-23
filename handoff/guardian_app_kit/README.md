# SafeWave 보호자 앱 개발 키트 (Android)

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

## 가장 중요한 세 가지
1. **응급 알림은 푸시만으로 완결돼야 한다.** 보호자 폰은 대부분 집 밖이라 RPi5 API에 닿지 않는다.
2. **서버는 data 전용 푸시를 지원한다(S0 완료, `FCM_DATA_ONLY`).** 기본값은 시스템 알림 형식이라,
   **앱 배포와 같은 날 서버를 `FCM_DATA_ONLY=true`로 바꿔야** 전체 화면 경보·반복 사이렌이 동작한다.
3. **"보장", "우회", "95%" 같은 표현을 쓰지 않는다.** 실기기(플립4)에서 확인한 것만 완료로 적는다.
