# 프레임워크 결정 — 추천: Kotlin + Jetpack Compose (네이티브 Android)

## 우리 앱의 핵심 요구
이 앱의 가치는 화면이 아니라 **"폰이 잠겨 있고, 무음이고, 앱이 꺼져 있어도 응급을 확실히 알리는 것"**이다.
그래서 판단 기준은 OS 알림 기능을 얼마나 직접, 확실하게 다룰 수 있는지다.

| 필요한 OS 기능 | 설명 |
|---|---|
| FCM data 메시지 백그라운드 처리 | 앱이 꺼져 있어도 `onMessageReceived`에서 직접 알림을 만든다 |
| Full-screen intent | 잠금 화면 위 전체 화면 경보. Android 14부터 특별 권한(사용자 허용) |
| 알람 오디오 | `USAGE_ALARM`으로 무음 모드와 무관하게 사이렌, 확인할 때까지 반복 |
| 알림 채널·방해금지 예외 | 채널 중요도, `setBypassDnd`, 권한 상태 확인과 설정 화면 이동 |
| 배터리 최적화 예외, WorkManager | 토큰 주기 재등록, 삼성 절전 정책 대응 |
| 잠금 화면 Activity | `setShowWhenLocked`, `setTurnScreenOn` |

## 비교

| 기준 | **Kotlin + Compose** | Flutter | React Native |
|---|---|---|---|
| 위 OS 기능 접근 | **직접**(공식 API 그대로) | 플러그인 + 일부는 결국 Kotlin 네이티브 코드(MethodChannel) 필요 | 네이티브 모듈 작성 필요 |
| 앱 종료 상태 FCM 처리 | `FirebaseMessagingService` 그대로 | 백그라운드 isolate, 플러그인 제약 | Headless JS, 제약 많음 |
| 전체 화면 알람 화면 | Activity 하나 | 플러그인 설정 + 네이티브 Activity 속성 | 네이티브 작업 |
| 디버깅 | 한 언어·한 도구(Android Studio) | Dart + Kotlin 두 층 | JS + Kotlin 두 층 |
| iOS 확장 | 불가(따로 개발) | 가능 | 가능 |
| 대상 | 플립4 한 기종 | 과한 범위 | 과한 범위 |
| 저장소 규칙 | 해당 없음(별도 저장소) | 해당 없음 | **JS/TS** — 팀 규칙(서버 저장소 JS 금지)과 결이 다름 |
| AI 에이전트 코드 품질 | 공식 문서·예제가 가장 많음 | 좋음 | 좋음 |

조사 근거
- Android 14부터 full-screen intent는 통화·알람 앱만 기본 허용되고, 그 외 앱은 `canUseFullScreenIntent()`로 확인 후
  `ACTION_MANAGE_APP_USE_FULL_SCREEN_INTENT`로 사용자 허용을 받아야 한다.
  ([Android 14 behavior changes](https://developer.android.com/about/versions/14/behavior-changes-14),
  [Play Console 도움말](https://support.google.com/googleplay/android-developer/answer/13392821?hl=en),
  [AOSP FSI limits](https://source.android.com/docs/core/permissions/fsi-limits))
- FCM은 notification+data 메시지를 백그라운드에서 시스템 트레이가 처리하고 `onMessageReceived`를 부르지 않는다.
  data 전용 메시지만 앱 상태와 관계없이 `onMessageReceived`가 호출된다.
  ([Firebase: Receive messages in an Android app](https://firebase.google.com/docs/cloud-messaging/android/receive-messages))
- Flutter에서도 종료 상태 전체 화면 알람은 보통 Kotlin 네이티브 코드와 MethodChannel로 구현한다.
  ([flutter_local_notifications](https://pub.dev/packages/flutter_local_notifications),
  [Flutter Alarm Manager POC](https://github.com/Applinx-Tech/Flutter-Alarm-Manager-POC))

## 결론
**Kotlin + Jetpack Compose.** 핵심 기능이 전부 Android 알림 API라서, 크로스플랫폼을 써도 그 부분은 결국 Kotlin으로
짜야 한다. 대상이 Android 한 기종이고 기간이 4주라 층을 하나로 두는 게 가장 빠르고 디버깅도 쉽다.
iOS가 필요해지면(현재 계획 없음) 그때 별도로 판단한다.

## 추천 구성 (가볍게)

| 영역 | 선택 | 이유 |
|---|---|---|
| 언어·UI | Kotlin, Jetpack Compose, Material 3 | 표준 |
| 구조 | 단일 모듈, MVVM(ViewModel + StateFlow) + Repository | 4주 프로젝트에 모듈 분리는 과함 |
| DI | 수동 DI(`AppContainer`) | Hilt는 설정 비용 대비 이득 작음. 커지면 도입 |
| 네트워크 | Retrofit + OkHttp + kotlinx.serialization | 표준, 목 인터셉터 쉬움 |
| 푸시 | Firebase Cloud Messaging(BoM) | 서버와 같은 Firebase 프로젝트 |
| 로컬 저장 | Room(받은 알림), DataStore(설정) | |
| 백그라운드 | WorkManager(토큰 재등록) | |
| 빌드 | Gradle Kotlin DSL, version catalog(`libs.versions.toml`) | |
| SDK | minSdk 29, target·compile 최신 안정 | 플립4는 Android 14 이상(업데이트 상태 확인) |
| 테스트 | JUnit, MockWebServer, Compose UI test | |
