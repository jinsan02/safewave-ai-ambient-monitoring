# AGENTS.md — SafeWave 보호자 앱 (Android)

독거인 안전 모니터링 시스템 SafeWave의 보호자 앱. 서버(RPi5 FastAPI)는 별도 저장소 `jinsan02/rp5`이며 여기서 수정하지 않는다.
명세 원본은 `docs/`. 이 파일과 명세가 다르면 명세를 따르고, 모호하면 추측하지 말고 묻는다.

## 최우선 목표
앱이 꺼져 있고, 화면이 잠겨 있고, 무음이어도 **응급 알림이 보이고 들리는 것**. 다른 기능은 이보다 뒤다.

## 스택
Kotlin, Jetpack Compose(Material 3), 단일 모듈, MVVM + Repository, 수동 DI(`AppContainer`), Retrofit + OkHttp +
kotlinx.serialization, Firebase Messaging, Room, DataStore, WorkManager. 새 라이브러리는 이유를 적고 확인받는다.

## 반드시 지킬 것
- 응급 경로(`push/`, `alarm/`)는 네트워크·UI에 의존하지 않는다. `onMessageReceived`에서 오래 걸리는 일을 하지 않는다.
- FCM data 값은 모두 문자열이다. 구 형식(type 없음 + risk_level=critical)도 응급으로 처리한다.
- 알림 채널 ID는 서버 계약과 같게: `emergency_alarm`, `safety_alert`.
- 전체 화면 알림은 `canUseFullScreenIntent()` 확인 → 없으면 설정 안내, 거부 시 헤드업 + 반복 소리로 동작.
- 소리는 `AudioAttributes.USAGE_ALARM`. 알람 음량을 바꿨으면 반드시 원래 값으로 복구.
- 금지: 접근성 서비스 오버레이, `setRingerMode`, WakeLock 직접 제어, 상시 포그라운드 서비스, `CALL_PHONE` 자동 발신.
- 서버 JSON: 알 수 없는 필드 무시, 누락 필드 기본값. 서버에 없는 API(404)는 "준비 중"으로 처리.
- 표시 금지: `experts.vital`(미학습), `experts.speech_ko.transcript_ko`(개인정보). 낙상 경보에 방 이름 붙이지 않음.
- 개인정보: 로그에 토큰·전화번호·전사 내용 금지. 로컬 알림 보관 최근 100건.
- 주소·키 하드코딩 금지. `google-services.json`은 Git에 올리지 않는다.
- "보장", "우회", "95%" 같은 표현을 UI·문서에 쓰지 않는다.

## 작업 방식
- 단계별로 진행(`prompts/02_PHASES.md`)하고 단계마다 `./gradlew assembleDebug testDebugUnitTest` 결과를 보고한다.
- 에뮬레이터로 확인할 수 없는 것(잠금·무음·방해금지·커버 화면)은 "실기기 확인 필요"로 표시하고 완료로 적지 않는다.
- 커밋 메시지는 한국어 한 줄 요약 + 필요한 경우 본문. 비밀값을 커밋하지 않는다.
