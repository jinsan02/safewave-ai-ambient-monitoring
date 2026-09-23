# 단계별 프롬프트

각 단계를 시작할 때 해당 블록을 붙여 넣는다. 이전 단계 보고를 확인한 뒤 넘어간다.

## Phase 0 — 골격
```text
Phase 0을 진행해라. Gradle(Kotlin DSL, version catalog), 단일 app 모듈, 패키지 구조(PROJECT_STRUCTURE.md),
AppContainer(수동 DI), 고대비 테마(본문 18sp 이상), Compose 내비게이션(온보딩/홈/이력/상세/센서/설정 빈 화면),
debug 빌드 목 모드(MockInterceptor가 debug/assets/mock/*.json 반환)를 만든다.
완료 조건: assembleDebug 성공, 목 모드에서 빈 화면 간 이동. .gitignore에 google-services.json 포함.
```

## Phase 1 — 응급 경로 (가장 중요)
```text
Phase 1을 진행해라. docs/03_ANDROID_ALERT_RELIABILITY.md 1~3절대로 구현한다.
- NotificationChannels(emergency_alarm: HIGH, USAGE_ALARM 사운드, 진동, 잠금화면 공개, bypassDnd 요청 / safety_alert / general)
- FcmPayloadParser: mock/fcm_*.json 전부 처리(구 형식 포함), 문자열 → 타입 변환 실패에 안전
- GuardianMessagingService.onMessageReceived → Room 저장 → AlertNotifier
- AlertNotifier: CATEGORY_ALARM, FLAG_INSISTENT, full-screen intent(canUseFullScreenIntent 확인), 액션(확인·전화)
- EmergencyAlarmActivity(showWhenLocked, turnScreenOn): 사유·시각(상대/절대)·판단 경로, 버튼 4개
  (대상자 전화 ACTION_DIAL, 119 ACTION_DIAL, 확인, 오탐 신고)
- AlarmPlayer: USAGE_ALARM 반복, 최대 5분, 알람 음량 복구
- debug 전용 "가짜 응급 푸시" 버튼(서비스와 같은 경로 호출)
단위 테스트: 파서(구·신 형식, 누락·잘못된 값), voice_ok 연결 규칙.
보고: 에뮬레이터 확인 항목과 docs/03 5절 실기기 확인표 중 남은 항목을 구분.
```

## Phase 2 — 온보딩·등록
```text
Phase 2를 진행해라. F1, F9, F10.
- 설치 시 UUID 기기 ID 생성(DataStore)
- 온보딩: 서버 주소 입력 → GET / 확인 → POST_NOTIFICATIONS → 전체 화면 권한(Android 14+) → 배터리 최적화 예외 →
  방해금지 예외 안내(삼성 경로 문구) → 토큰 등록 → /notify/test → 테스트 알림 수신 확인
- 토큰 재등록: 앱 시작, onNewToken, TokenRefreshWorker(30분)
- PermissionChecker + 홈 상단 경고 배너(항목별 설정 이동)
단위 테스트: 등록 재시도(백오프), 권한 상태 → 배너 매핑.
```

## Phase 3 — 홈·이력·센서·설정
```text
Phase 3을 진행해라. F5~F8.
- 홈: GET /app/summary(구현됨). 센서 화면만 /nodes/health 사용.
  앱 전면일 때만 10초 폴링. 데이터 지연(10초), no data yet, 503, 연결 실패 문구는 docs/02 6절대로.
  ai_enabled=false 또는 models.m1=false면 "감시 일부 꺼짐" 경고.
- 이력: Room(푸시) + /history(critical 기본, warning 토글) 병합, msg_id(_id) 또는 ts_ms+node_id로 중복 제거, 최근 100건.
- 상세: 경보 정보, voice_ok 반영, 확인·신고 상태.
- 센서: 설치 노드(설정, 기본 1,2,3)만 표시.
- 설정: 서버 주소, API 키, 기기 ID, 보호 대상 이름·번호, 반복 시간, 알림 테스트, 등록 상태(/auth/tokens).
표시 금지 필드(vital, transcript) 확인. 단위 테스트: 병합·중복 제거, 지연 판정, 오류 매핑(MockWebServer).
```

## Phase 4 — 확인·오탐 신고
```text
Phase 4를 진행해라.
- 오탐·미탐 신고: POST /alerts/{msg_id}/feedback (구현됨, feedback=false_alarm|missed_alert). 확인 창에
  "오탐 신고는 1시간 동안 AI 판단을 조금 덜 민감하게 합니다(낙상 확정 경보에는 영향 없음)" 안내.
- 확인: AlertRepository.ack(msg_id, action) → POST /alerts/{msg_id}/ack(아직 없음) → 404·연결 실패면 로컬에만 기록하고
  "서버 미연동" 표시, 다음 앱 실행 때 재전송 시도.
확인 시 사이렌 정지. 다른 보호자 확인(type=ack 푸시, 예정)은 파서만 준비하고 화면에 "○○ 확인" 표시.
```

## Phase 5 — 마무리
```text
Phase 5를 진행해라. 접근성(TalkBack 라벨, 대비, 큰 글꼴 200%), 개인정보(로그 점검, 100건 제한),
오류·빈 상태 화면 점검, README(빌드·설치·Firebase 설정·실기기 확인표 사용법).
docs/03 5절 실기기 확인표를 체크리스트로 README에 복사하고, 에뮬레이터로 확인한 항목만 표시한다.
```

## 검토 프롬프트 (아무 때나)
```text
지금까지 코드를 AGENTS.md "반드시 지킬 것"과 docs/01 완료 기준에 대조해 위반·누락을 목록으로 보고해라.
특히: 응급 경로가 네트워크·UI에 의존하는지, 음량 복구 누락, 권한 거부 시 동작, 비밀값·개인정보 로그.
고치지 말고 먼저 보고해라.
```
