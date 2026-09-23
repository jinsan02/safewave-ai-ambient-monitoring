# 앱 저장소 구성 — `safewave-guardian-android`

단일 모듈 Android 앱. 패키지명은 D10 결정 전까지 `com.safewave.guardian`(예시).

```
safewave-guardian-android/
├─ AGENTS.md                      # 에이전트 규칙 (templates/AGENTS.md 복사)
├─ CLAUDE.md                      # Claude Code 사용 시 AGENTS.md와 같은 내용
├─ README.md                      # 빌드·설치·Firebase 설정 방법
├─ .gitignore                     # google-services.json, local.properties, *.keystore, /build
├─ docs/                          # 이 키트의 docs/ 복사 (명세 원본)
├─ mock/                          # 이 키트의 mock/ 복사 (목 응답·푸시 예시)
├─ settings.gradle.kts
├─ build.gradle.kts
├─ gradle/
│  └─ libs.versions.toml          # 버전 카탈로그
└─ app/
   ├─ build.gradle.kts
   ├─ google-services.json        # Git 제외. 서버와 같은 Firebase 프로젝트에서 받음
   └─ src/
      ├─ main/
      │  ├─ AndroidManifest.xml
      │  ├─ res/                  # 문자열(ko), 테마, 알람 사운드(raw/alarm.ogg), 아이콘
      │  └─ java/com/safewave/guardian/
      │     ├─ GuardianApp.kt              # Application: 채널 생성, AppContainer 초기화
      │     ├─ AppContainer.kt             # 수동 DI (Retrofit, Room, DataStore, Repository)
      │     ├─ MainActivity.kt             # Compose 진입, 내비게이션
      │     ├─ push/
      │     │  ├─ GuardianMessagingService.kt   # onMessageReceived, onNewToken
      │     │  ├─ FcmPayloadParser.kt           # data(문자열) → AlertEvent, 구·신 형식 호환
      │     │  ├─ AlertNotifier.kt              # 알림 생성(full-screen intent, FLAG_INSISTENT, 액션)
      │     │  └─ NotificationChannels.kt       # emergency_alarm, safety_alert, general
      │     ├─ alarm/
      │     │  ├─ EmergencyAlarmActivity.kt     # 잠금 화면 위 경보 화면
      │     │  ├─ AlarmPlayer.kt                # USAGE_ALARM 재생·반복·음량 복구
      │     │  └─ AlarmActionReceiver.kt        # 알림 버튼(확인) 처리
      │     ├─ data/
      │     │  ├─ remote/
      │     │  │  ├─ SafeWaveApi.kt             # Retrofit 인터페이스
      │     │  │  ├─ dto/                       # StatusDto, HistoryItemDto, NodeHealthDto ...
      │     │  │  ├─ ApiKeyInterceptor.kt       # X-API-Key(값 있을 때만)
      │     │  │  └─ MockInterceptor.kt         # debug 빌드: assets/mock/*.json 응답
      │     │  ├─ local/
      │     │  │  ├─ AppDatabase.kt, AlertEntity.kt, AlertDao.kt   # 최근 100건
      │     │  │  └─ SettingsStore.kt           # DataStore: 서버 주소, API 키, 기기 ID, 보호 대상 번호 ...
      │     │  └─ repo/
      │     │     ├─ AlertRepository.kt         # 푸시 저장 + /history 병합 + ack/신고
      │     │     ├─ StatusRepository.kt        # /status, /nodes/health, /system/health (→ /app/summary)
      │     │     └─ DeviceRepository.kt        # 토큰 등록·테스트 알림
      │     ├─ work/
      │     │  └─ TokenRefreshWorker.kt         # 30분 주기 재등록
      │     ├─ domain/
      │     │  ├─ AlertEvent.kt, RiskLevel.kt, SystemSummary.kt
      │     │  └─ PermissionChecker.kt          # 알림·FSI·배터리·방해금지 상태
      │     └─ ui/
      │        ├─ theme/                        # 고대비 색, 큰 글꼴
      │        ├─ components/                   # StatusBadge, WarningBanner, BigButton
      │        ├─ onboarding/                   # OnboardingScreen + ViewModel
      │        ├─ home/
      │        ├─ alerts/                       # 목록
      │        ├─ detail/
      │        ├─ sensors/
      │        └─ settings/
      ├─ debug/assets/mock/                     # mock/*.json 복사 (debug 전용)
      ├─ test/java/...                          # 단위: 파서, 병합, 지연 판정, 오류 매핑 (MockWebServer)
      └─ androidTest/java/...                   # UI: 온보딩, 경보 화면 버튼
```

## 규칙
- 서버 주소·API 키 하드코딩 금지(DataStore). debug 빌드에서만 목 모드 스위치.
- 네트워크는 Repository 뒤에만. ViewModel은 `StateFlow<UiState>`로 노출.
- `push/`·`alarm/`은 UI와 독립(앱이 꺼져 있어도 동작해야 함). 여기서 네트워크를 기다리지 않는다.
- 로그에 토큰·전화번호·전사 내용 금지.
