# 보호자 앱 ↔ RPi5 연동 시험 준비 (2026-10-13)

이 문서는 **실행 절차**다. 준비만으로 푸시 수신·RPi5 경보·앱 확인을 검증했다고 기록하지 않는다. 실제 경보 시험은 등록된 모든 보호자 폰에 알림이 갈 수 있으므로 참여자에게 사전 고지하고, 실상황과 혼동되지 않는 시간에 1회만 수행한다.

## 1. 시작 전 확인

- RPi5 전원·LAN·인터넷, 보호자 Android 폰의 인터넷, 앱 설치·알림 권한을 확인한다. REST 등록·확인은 폰에서 RPi5에 닿아야 하므로 시연은 같은 LAN에서 한다. 집 밖 접속·인증은 미결정(D1·D5)이며 이 시험에서 공용 인터넷에 API를 열지 않는다.
- 앱과 `api/auth/firebase_key.json`이 같은 Firebase 프로젝트를 쓰는지 앱 담당자와 확인한다. 키·FCM 토큰을 채팅, 보고서, 명령 기록에 붙이지 않는다.
- RPi5의 현재 IP·커밋·이미지·설정은 10-05 기록을 현재값으로 가정하지 않는다. 특히 `VOICE_ENABLED=false`면 `voice_ok=null`이 정상이고, `APP_EXPECTED_NODES` 기본값은 아직 3대다.
- 앱이 data-only FCM을 처리하는 것을 확인한 뒤에만 RPi5의 `FCM_DATA_ONLY=true`를 적용한다. 현재 Compose 기본값은 `false`다. 설정 변경·API 재생성은 실제 시험 직전에 사용자와 함께 한다.

## 2. RPi5 읽기 전용 사전 점검

RPi5에서 `~/safewave`를 기준으로 실행한다. IP가 바뀌었다면 폰의 서버 주소도 함께 바꾼다.

```bash
git rev-parse --short HEAD
docker compose ps
curl -fsS http://127.0.0.1:8000/
curl -fsS http://127.0.0.1:8000/app/summary
curl -fsS http://127.0.0.1:8000/auth/tokens
curl -fsS http://127.0.0.1:8000/settings
docker compose exec -T api printenv FCM_DATA_ONLY APP_EXPECTED_NODES VOICE_ENABLED
```

진행 조건: API 응답, `fcm_ready=true`, 앱 기기의 `device_id`가 `/auth/tokens.devices`에 표시, M2 off. 노드 5대 시연이면 `nodes_expected=5`도 확인한다. 노트북의 Docker 설정·Firebase 키가 RPi5에 자동 복사되지는 않는다.

입력 오류의 상태코드는 실제 발송 없이 아래처럼 확인할 수 있다. 본문·코드는 `docs/02_API_SPEC.md` 6절과 대조한다. Redis/FCM을 일부러 중단하는 장애 시험은 이 단계에서 하지 않는다.

```bash
curl -i http://127.0.0.1:8000/alerts/bad-id
curl -i http://127.0.0.1:8000/alerts/99999999999999999999-99999999999999999999
curl -i 'http://127.0.0.1:8000/history?before=bad-id'
curl -i -X POST -H 'Content-Type: application/json' -d '{"token":""}' \
  http://127.0.0.1:8000/notify/test
```

## 3. 실제 연동 순서

1. 폰 앱을 열어 RPi5 주소로 토큰을 재등록한다. `/auth/tokens`에는 토큰 값이 아니라 `device_id`만 표시되는지 확인한다.
2. 앱의 `POST /notify/test` 버튼으로 **일반 시험 푸시**를 보내고, 서버 200 응답과 폰 실제 수신 시각을 따로 기록한다. 200은 폰 수신 증거가 아니다.
3. 앱이 data-only 푸시를 처리한다면 `FCM_DATA_ONLY=true`를 적용하고 API를 재생성한 뒤, 2절 값이 실제로 바뀌었는지 다시 확인한다. 앱 미지원이면 이 단계에서 중단한다.
4. 동시 경보가 없는 상태에서 승인된 16 kHz·mono·16-bit PCM “도와주세요” WAV로 아래 종단 시험을 **한 번** 수행한다. RPi5에 `scripts/dev/push_e2e.py` 최신본과 WAV를 준비한다. 이전 미추적 복사본을 쓰지 말고 파일을 비교한다.

```bash
docker cp scripts/dev/push_e2e.py rp5-ai-experts:/tmp/push_e2e.py
docker cp /path/to/approved_help.wav rp5-ai-experts:/tmp/approved_help.wav
docker exec -e REDIS_HOST=db rp5-ai-experts python3 /tmp/push_e2e.py \
  --wav /tmp/approved_help.wav --api http://api:8000 --node 1 \
  --timeout 120 --ack-wait 60 --out /tmp/push_e2e.json
docker cp rp5-ai-experts:/tmp/push_e2e.json /tmp/push_e2e.json
```

스크립트는 해당 노드·주입 시각의 **긴급 음성 규칙 경보**만 결과로 잡는다. `devices_notified`는 Redis의 `notify:sent` **발송 시도 표시 수**이지 FCM 수신·폰 표시 성공 수가 아니다. 결과 JSON에는 경보 사유에 음성 전사가 들어갈 수 있으므로 공유 전 확인한다.

5. 폰에 표시된 `msg_id`와 결과 JSON의 `msg_id`가 같은지 확인한다. 앱에서 `seen`, 이어서 필요하면 `called`를 누르고 `GET /alerts/{msg_id}`의 `acked_by`를 확인한다. `VOICE_ENABLED=false`라면 `voice_ok`는 판정하지 않는다. 90초 동일 노드 잠금 중 재주입하지 않는다.
6. 앱 로그/화면에서 잠금·무음·방해금지·앱 종료 상태를 **각각 별도 회차**로 시험한다(`docs/03_ANDROID_ALERT_RELIABILITY.md`). 한 회차의 성공을 다른 상태의 성공으로 일반화하지 않는다.

## 4. 남길 결과와 중단 조건

기록: RPi5 커밋·이미지/모델·`FCM_DATA_ONLY`·`APP_EXPECTED_NODES`·`VOICE_ENABLED`, 앱 버전·Android/One UI 버전, 입력 WAV 식별자, 등록 기기 수, 주입/경보/발송 시도/폰 수신/ack 시각, `msg_id`, 실패 단계와 HTTP 코드. 토큰·서비스 계정 키·전사 원문은 보고서에 넣지 않는다.

API 불통, `fcm_ready=false`, 등록 기기 0대, 앱의 data-only 미지원, 다른 실제 경보 진행 중이면 **긴급 음성 주입은 중단**한다. `notify:sent`만 보이거나 자동 주입된 ack는 앱 실기기 성공으로 세지 않는다.

## 5. 10-13 노트북 사전 확인 결과

- 노트북 Docker의 API를 최신 로컬 수정본으로 다시 빌드했고, 같은 Wi-Fi에서 `GET /app/summary` 200을 확인했다.
- 앱 기기 1대가 `POST /auth/register-token`으로 등록됐고, 서버의 `POST /notify/test`가 Firebase 메시지 ID를 반환했다.
- 위 결과는 **Firebase가 발송 요청을 접수했다는 증거**다. 휴대폰 화면 표시·알림음·백그라운드/종료 상태 수신은 이 대화에서 확인되지 않았다.
- 시험 서버는 노트북이었으며 당시 LAN 주소는 `192.168.0.11`이었다. DHCP 주소이므로 재사용 전에 다시 확인한다.
- RPi5의 현재 주소·배포 커밋·앱 등록·푸시 수신·ack·TTS는 이번 확인 범위가 아니다.
