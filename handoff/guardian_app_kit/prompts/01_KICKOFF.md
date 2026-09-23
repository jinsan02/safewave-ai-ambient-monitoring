# 시작 프롬프트

새 저장소에 `AGENTS.md`(또는 `CLAUDE.md`), `docs/`, `mock/`을 넣은 뒤 아래를 그대로 붙여 넣는다.
`[ ]` 안은 결정(`docs/05_OPEN_DECISIONS.md`)에 맞게 고친다.

```text
너는 Android 개발자다. SafeWave(독거인 안전 모니터링 시스템)의 보호자 앱 MVP를 이 저장소에 새로 만든다.
서버는 별도 저장소(Raspberry Pi 5의 FastAPI)에 이미 있고 여기서는 수정하지 않는다.

먼저 다음 파일을 모두 읽어라: AGENTS.md, docs/00_FRAMEWORK_DECISION.md, docs/01_FEATURE_SPEC.md,
docs/02_API_SPEC.md, docs/03_ANDROID_ALERT_RELIABILITY.md, docs/05_OPEN_DECISIONS.md,
docs/PROJECT_STRUCTURE.md, mock/ 전체.
저장소 구조는 docs/PROJECT_STRUCTURE.md를 따른다.

핵심 목표: 앱이 종료·잠금·무음 상태여도 응급 푸시가 전체 화면 경보 + 반복 사이렌으로 나타나는 것.
MVP 범위: 기능 명세 3절 F1~F10. P1·P2·제외 항목은 만들지 않는다.

결정 사항(현재):
- 패키지명 [com.safewave.guardian], 앱 이름 [세이프웨이브 보호자]
- 원격 접속 [LAN 전용 — 서버 불통을 정상 상황으로 처리]
- 앱에서 설정 변경 [읽기 전용], 음성 전사 [표시 안 함], 경보 반복 [5분 후 정지]
- 배포 [APK 사이드로딩]

서버 계약 주의:
- 서버는 FCM_DATA_ONLY 설정에 따라 시스템 알림 형식(현재 기본) 또는 data 전용 high priority 형식을 보낸다.
  두 형식 모두 type·msg_id를 포함한다. 두 형식을 처리하는 FcmPayloadParser를 만들고, 완전한 동작은 data 전용 기준.
- 사용 가능: /app/summary, POST /alerts/{msg_id}/feedback, DELETE /auth/register-token/{device_id}, /history?before=.
- 아직 없음: POST /alerts/{msg_id}/ack, GET /alerts/{msg_id}. Repository 인터페이스 뒤에 두고 404면 로컬 기록 + "준비 중".
- google-services.json이 없으면 목 모드로 개발한다(debug 빌드, mock/*.json 응답, 가짜 푸시 트리거 버튼).

진행 방식:
1. 지금은 코드를 쓰지 말고 다음을 제안하라: 프로젝트 골격(파일 목록), libs.versions.toml 의존성, 매니페스트 권한,
   알림 수신 → 경보 화면까지의 클래스 흐름, 단계별 계획(prompts/02_PHASES.md와 대조), 명세에서 모호하거나
   충돌하는 점.
2. 내가 확인하면 Phase 0부터 진행한다. 각 단계 끝에 빌드·단위 테스트 결과, 에뮬레이터로 확인한 것,
   실기기에서 확인해야 할 것을 나눠서 보고한다.
```
