# Android 응급 알림 신뢰성 — 구현 방법과 실기기 확인 항목

원칙: OS를 "우회"하지 않는다. **공식 API + 사용자에게 허용받기 + 허용이 없을 때 단계적으로 약하게 동작**.
모든 항목은 플립4 실기기에서 확인한 뒤에만 완료로 적는다.

## 1. 수신 흐름

```
FCM data(high) ─▶ GuardianMessagingService.onMessageReceived   (앱이 꺼져 있어도 호출, 처리 시간 짧음)
   ├─ FcmPayloadParser: 문자열 → AlertEvent (type, msg_id, ts_ms, node_id, summary, ...)
   ├─ AlertRepository.save(event)            (Room, 동기 처리 금지 → 짧은 코루틴/WorkManager expedited)
   └─ AlertNotifier.showEmergency(event)
        ├─ emergency_alarm 채널, CATEGORY_ALARM, PRIORITY_MAX, VISIBILITY_PUBLIC, FLAG_INSISTENT
        ├─ setFullScreenIntent(EmergencyAlarmActivity)   ← canUseFullScreenIntent()일 때
        └─ 액션 버튼: 확인 / 전화
EmergencyAlarmActivity (showWhenLocked, turnScreenOn)
   └─ AlarmPlayer: MediaPlayer(AudioAttributes USAGE_ALARM), 반복, 진동 패턴, 최대 N분
```

## 2. 기능별 구현

| 요구 | 구현 | 허용이 없을 때 |
|---|---|---|
| 앱 종료 상태 수신 | 서버 data 전용 high priority(S0). `onMessageReceived`에서 직접 알림 생성 | 서버가 notification 형식이면 시스템 기본 알림만 뜸(탭하면 앱이 extras로 상세 표시) |
| 잠금 화면 전체 화면 | `setFullScreenIntent` + Activity `setShowWhenLocked(true)`, `setTurnScreenOn(true)`, `KeyguardManager.requestDismissKeyguard`는 버튼 동작 시에만 | Android 14+에서 `NotificationManager.canUseFullScreenIntent()`가 false면 `ACTION_MANAGE_APP_USE_FULL_SCREEN_INTENT`로 안내. 거부 시 헤드업 알림 + 반복 소리 |
| 무음·진동 모드에서 소리 | 채널 사운드와 Activity 재생 모두 `AudioAttributes.USAGE_ALARM`(알람 스트림은 벨소리 무음과 별개). 알람 음량이 0이면 경보 동안 `STREAM_ALARM` 음량을 올리고 종료 시 복구 | 사용자가 알람 음량을 막을 방법이 있는 기기는 진동+화면으로 동작 |
| 확인할 때까지 반복 | 알림 `FLAG_INSISTENT`(열 때까지 소리 반복) + Activity의 반복 재생. 최대 시간(기본 5분) 후 정지하고 "미확인" 상태로 남김 | — |
| 방해금지 모드 | 채널 `setBypassDnd(true)`는 앱이 알림 정책 접근 권한을 받았거나 사용자가 방해금지 예외로 지정해야 효과. 온보딩에서 삼성 "설정 > 알림 > 방해 금지 > 앱 추가" 안내. `CATEGORY_ALARM`은 방해금지의 "알람 허용"에 해당 | 예외가 없으면 방해금지 중에는 무음 알림 → 홈 배너로 계속 경고 |
| 화면 깨우기 | `setTurnScreenOn`(Activity). **WakeLock 직접 사용하지 않음** | — |
| 절전·배터리 | `ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS`(사이드로딩 배포 전제), 삼성 "절전 예외 앱"·"사용하지 않는 앱 절전" 해제 안내 | high priority FCM은 Doze에서도 대개 전달되나 삼성 "딥 슬립" 앱은 막힐 수 있음 → 주기 재등록·heartbeat로 감지 |
| 권한·채널 꺼짐 감지 | 앱 시작·재개 시 `areNotificationsEnabled`, 채널 중요도, `canUseFullScreenIntent`, 배터리 예외 여부 확인 → 홈 배너 | — |
| 플립4 커버 화면 | 접힌 상태에서는 커버 화면에 알림 요약·소리·진동. 펼치면 경보 Activity. 커버 화면에서 Activity 실행은 기본 불가(삼성 실험 기능) | 알림 제목·본문을 짧게(커버 화면 폭) |

## 3. 권한·매니페스트

```
POST_NOTIFICATIONS            (Android 13+, 런타임 요청)
USE_FULL_SCREEN_INTENT        (Android 14+ 특별 권한, 사용자 허용)
VIBRATE, WAKE_LOCK            (FCM 라이브러리가 사용)
INTERNET, ACCESS_NETWORK_STATE
REQUEST_IGNORE_BATTERY_OPTIMIZATIONS   (배포 방식이 사이드로딩일 때만)
ACCESS_NOTIFICATION_POLICY    (방해금지 예외를 채널로 요청하는 경우, 사용자 허용)
CALL_PHONE 는 쓰지 않는다 → ACTION_DIAL(번호가 채워진 전화 앱 열기)
```
EmergencyAlarmActivity: `android:showWhenLocked="true"`, `android:turnScreenOn="true"`, `launchMode="singleTask"`,
`excludeFromRecents="true"`, 별도 `taskAffinity`.

## 4. 쓰지 않는 것 (이유)

| 기법 | 이유 |
|---|---|
| 접근성 서비스 오버레이 | 접근성 목적 외 사용은 정책 위반, Android 13+ 사이드로딩 앱은 기본 차단. 우회 안내는 보안상 부적절 |
| `setRingerMode`로 무음 해제 | 사용자 설정 변경, 방해금지 중에는 권한 없이 실패 |
| 상시 포그라운드 서비스(`specialUse`) | 푸시 구조에 불필요, 배터리 소모, 심사 부담 |
| `ROLE_EMERGENCY`, 정확 알람 권한 | 일반 앱이 받을 수 없음 / 불필요 |
| `SYSTEM_ALERT_WINDOW` | 전체 화면 알림으로 충분 |

## 5. 실기기 확인표 (플립4, One UI 버전 기록)

| # | 상태 | 기대 결과 | 결과 |
|---|---|---|---|
| 1 | 앱 실행 중 | 경보 화면 + 사이렌 | |
| 2 | 앱 백그라운드, 화면 켜짐 | 전체 화면 경보(또는 헤드업) + 사이렌 | |
| 3 | 앱 강제 종료 후 잠금 | 화면 켜짐 + 경보 화면 + 사이렌 | |
| 4 | 무음 모드 | 사이렌 울림 | |
| 5 | 방해금지(앱 예외 허용) | 사이렌 울림 | |
| 6 | 방해금지(예외 없음) | 무음 알림 + 앱 열면 배너 | |
| 7 | 접힘(커버 화면) | 커버 알림 + 소리, 펼치면 경보 화면 | |
| 8 | 전체 화면 권한 거부 | 헤드업 + 반복 소리 | |
| 9 | 절전 모드·하룻밤 방치 후 | 수신 | |
| 10 | 재부팅 직후(앱 미실행) | 수신 | |
| 11 | 기기 2대 | 동시 수신 | |
| 12 | 서버 1시간+ 정지 후 재시작 | 30분 안에 재등록되어 수신 | |
