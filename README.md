# SafeWave-AI: 독거인 안전 모니터링 시스템

라즈베리 파이 5 기반 실시간 CSI 모니터링 파이프라인:

- `sensing`: UDP CSI 수신 + 전처리, Redis 스트림 `csi:raw`에 저장
- `ai`: 전문가 모델 추론 (M1-M4) + 위험도 융합, Redis 스트림 `ai:result`에 저장
- `api`: FastAPI REST + WebSocket 앱 통합
- `db`: Redis 스트림 허브

이 스택은 Docker Compose로 컨테이너화되어 Windows/macOS/Linux에서 동작합니다.

## 프로젝트 구조

```text
<repo-name>/
    docker-compose.yml
    .env
    README.md
    sensing/
        main.py
        simulator.py
        filters/
    ai/
        main.py
        experts/
        logic/
    api/
        main.py
        notifier.py
    db/
        redis.conf
    volumes/
        data/
        models/
        logs/
```

## 저장소 이름 추천

GitHub 저장소 이름 추천:

- `safewave-ai-ambient-monitoring`

로컬에서는 `rp5` 폴더명을 유지하되, GitHub에서는 포트폴리오에 적합한 이름을 사용합니다.

## 필수 요구사항

1. Docker Desktop 4.x+ (Compose v2 포함)
2. Git
3. 선택사항: 로컬 Python (컨테이너 외부에서 `sensing/simulator.py` 실행 시)

## 시작하기 (빠른 시작)

1. 클론

```powershell
git clone <your-repo-url>
cd <repo-name>
```

2. `.env` 확인

필수 설정이 이미 준비되어 있습니다. 다음 항목을 확인하세요:

- `REDIS_HOST=db`
- `REDIS_PORT=6379`
- `MODEL_PATH=/app/models`
- `FIREBASE_KEY_PATH=/app/auth/firebase_key.json`

3. 볼륨 폴더 생성

```powershell
mkdir volumes\data -ErrorAction SilentlyContinue
mkdir volumes\models -ErrorAction SilentlyContinue
mkdir volumes\logs -ErrorAction SilentlyContinue
mkdir api\auth -ErrorAction SilentlyContinue
```

Firebase 서비스 계정 키 파일 배치:

- `api/auth/firebase_key.json`

이 경로는 API 컨테이너에 `/app/auth/firebase_key.json`으로 마운트됩니다.

4. 빌드 및 실행

```powershell
docker compose up -d --build
```

5. 상태 확인

```powershell
docker compose ps
Invoke-RestMethod http://localhost:8000/
```

예상 API 응답:

```json
{"service":"rp5-api","status":"ok"}
```

## Docker 네트워크 참고사항

- `sensing`은 브릿지 네트워크에서 실행되며 UDP `5005:5005/udp` 포트 발행
- `sensing`, `ai`, `api`는 `REDIS_HOST=db` 사용
- Redis 데이터는 `./volumes/data`에 저장
- API는 Firebase 인증 디렉토리 마운트: `./api/auth:/app/auth:ro`

## 시뮬레이터 실행 (엔드투엔드 테스트)

스택이 이미 실행 중일 때, 새 터미널을 열고:

```powershell
cd sensing
python simulator.py --host 127.0.0.1 --port 5005 --nodes 4 --rate 10 --scenario auto
```

데이터 흐름 확인:

```powershell
Invoke-RestMethod http://localhost:8000/status | ConvertTo-Json -Depth 5
Invoke-RestMethod http://localhost:8000/logs?n=10 | ConvertTo-Json -Depth 5
Invoke-RestMethod http://localhost:8000/nodes/health | ConvertTo-Json
```

## 대시보드 (monitor.html)

대시보드 파일 열기:

```powershell
Start-Process "C:\rp5\monitor.html"
```

`monitor.html`의 주소 동작:

- `file://`로 열 때: API는 `http://localhost:8000`으로 기본 설정
- `http(s)://`로 제공될 때: 대시보드는 현재 호스트 자동 감지 및 `http(s)://<host>:8000` 사용
- 외부 기기 접근 시: localhost 대신 서버 IP 사용 (예: `192.168.0.25`)

선택사항 URL 오버라이드 매개변수:

- `?api=http://<server-ip>:8000`
- `?ws=ws://<server-ip>:8000/ws/monitor`

예시:

```text
http://192.168.0.25/monitor.html?api=http://192.168.0.25:8000
```

## 앱 통합용 API 엔드포인트

모니터링:

- `GET /status`
- `GET /logs?n=60`
- `GET /history?n=100&level=warning`
- `GET /charts/minute?minutes=10`
- `WS /ws/monitor`

제어 및 관리:

- `GET /settings`
- `POST /settings`
- `GET /nodes/health`
- `POST /auth/register-token`
- `GET /auth/tokens`
- `POST /notify/test`
- `POST /notify/send`
- `POST /notify/check`

`POST /settings` 요청 본문 예시:

```json
{
    "risk_threshold": 0.8,
    "active_nodes": [1, 2, 3, 4],
    "ai_enabled": true
}
```

## 선택사항 리소스

1. ONNX 모델

- 모델 파일을 `volumes/models`에 배치
- 파일이 없으면 폴백 로직 사용 (계속 동작)

2. Firebase 서비스 계정 키

- 키 파일을 `FIREBASE_KEY_PATH`로 매핑된 경로에 배치
- 키가 없으면 알림 엔드포인트는 실제 FCM 전송 시 오류 반환

## 일반 문제 해결

1. `/status`가 204 반환

- AI 스트림이 비어 있음. 시뮬레이터 시작 후 `sensing` 로그 확인

2. `ws://localhost:8000/ws/monitor` 연결 실패

- API 이미지에 `websockets` 패키지 포함 확인
- API 재빌드: `docker compose up -d --build api`

3. 재시작 직후 Redis 연결 오류

- Redis 재시작 중 짧은 시동 경쟁 발생 가능
- 몇 초 후 재시도; 서비스는 자동 재연결

4. 온라인 노드 없음

- 시뮬레이터가 `127.0.0.1:5005`로 패킷 송신 확인
- `docker compose ps` 및 `docker logs rp5-sensing` 확인

## 중지 / 초기화

중지만:

```powershell
docker compose down
```

중지 및 볼륨 제거 (완전 초기화):

```powershell
docker compose down -v
```

## GitHub 발행 체크리스트

첫 푸시 전 확인사항:

1. `docker compose up -d --build` 오류 없이 완료
2. `http://localhost:8000/status`가 실시간 JSON 반환
3. `monitor.html` 열림 및 WebSocket 연결
4. `.gitignore`에 보안 정보 및 런타임 아티팩트 포함
5. Firebase 키 파일 미커밋 (`api/auth/firebase_key.json`)
6. 대용량 모델 파일 (`*.onnx`) 제외 또는 Git LFS로 관리

첫 푸시 제안 명령어:

```powershell
git init
git add .
git commit -m "초기 배포: SafeWave-AI 모니터링 스택"
git branch -M main
git remote add origin <your-github-repo-url>
git push -u origin main
```