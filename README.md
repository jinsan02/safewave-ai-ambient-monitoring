# SafeWave-AI: Ambient Monitoring for Solo Safety

Real-time CSI monitoring pipeline for Raspberry Pi 5 style deployment:

- `sensing`: UDP CSI ingest + preprocessing, writes to Redis stream `csi:raw`
- `ai`: expert inference (M1-M4) + risk fusion, writes to Redis stream `ai:result`
- `api`: FastAPI REST + WebSocket for app integration
- `db`: Redis stream hub

The stack is containerized with Docker Compose and runs on Windows/macOS/Linux.

## Project Layout

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

## Repository Naming

Recommended repository name:

- `safewave-ai-ambient-monitoring`

You can keep folder name as `rp5` locally, but use a clear portfolio-friendly name on GitHub.

## Prerequisites

1. Docker Desktop 4.x+ (Compose v2 included)
2. Git
3. Optional local Python for running `sensing/simulator.py` outside containers

## Clone And Start (Quick Start)

1. Clone

```powershell
git clone <your-repo-url>
cd <repo-name>
```

2. Check `.env`

Required keys are already prepared in this project. Verify at least:

- `REDIS_HOST=db`
- `REDIS_PORT=6379`
- `MODEL_PATH=/app/models`
- `FIREBASE_KEY_PATH=/app/auth/firebase_key.json`

3. Ensure volume folders exist

```powershell
mkdir volumes\data -ErrorAction SilentlyContinue
mkdir volumes\models -ErrorAction SilentlyContinue
mkdir volumes\logs -ErrorAction SilentlyContinue
mkdir api\auth -ErrorAction SilentlyContinue
```

Place Firebase service account key file as:

- `api/auth/firebase_key.json`

This path is mounted into API container as `/app/auth/firebase_key.json`.

4. Build and run

```powershell
docker compose up -d --build
```

5. Check health

```powershell
docker compose ps
Invoke-RestMethod http://localhost:8000/
```

Expected API response:

```json
{"service":"rp5-api","status":"ok"}
```

## Docker Network Notes

- `sensing` runs on bridge network and publishes UDP `5005:5005/udp`
- `sensing`, `ai`, `api` use `REDIS_HOST=db`
- Redis data persists in `./volumes/data`
- API mounts Firebase auth directory: `./api/auth:/app/auth:ro`

## Run Simulator (End-to-End Test)

With stack already up, open a new terminal:

```powershell
cd sensing
python simulator.py --host 127.0.0.1 --port 5005 --nodes 4 --rate 10 --scenario auto
```

Then verify data flow:

```powershell
Invoke-RestMethod http://localhost:8000/status | ConvertTo-Json -Depth 5
Invoke-RestMethod http://localhost:8000/logs?n=10 | ConvertTo-Json -Depth 5
Invoke-RestMethod http://localhost:8000/nodes/health | ConvertTo-Json
```

## Dashboard (monitor.html)

Open dashboard file:

```powershell
Start-Process "C:\rp5\monitor.html"
```

Address behavior in `monitor.html`:

- When opened as `file://`, API defaults to `http://localhost:8000`
- When served over `http(s)://`, dashboard auto-detects current host and uses `http(s)://<host>:8000`
- For external device access, use server IP (for example `192.168.0.25`) instead of localhost

Optional URL override parameters:

- `?api=http://<server-ip>:8000`
- `?ws=ws://<server-ip>:8000/ws/monitor`

Example:

```text
http://192.168.0.25/monitor.html?api=http://192.168.0.25:8000
```

## API Endpoints For App Integration

Monitoring:

- `GET /status`
- `GET /logs?n=60`
- `GET /history?n=100&level=warning`
- `GET /charts/minute?minutes=10`
- `WS /ws/monitor`

Control and management:

- `GET /settings`
- `POST /settings`
- `GET /nodes/health`
- `POST /auth/register-token`
- `GET /auth/tokens`
- `POST /notify/test`
- `POST /notify/send`
- `POST /notify/check`

Example `POST /settings` body:

```json
{
    "risk_threshold": 0.8,
    "active_nodes": [1, 2, 3, 4],
    "ai_enabled": true
}
```

## Optional Assets

1. ONNX models

- Place model files in `volumes/models`
- If files are missing, fallback logic is used (still runs)

2. Firebase service account key

- Place key file at path mapped to `FIREBASE_KEY_PATH`
- Without key, notify endpoints return error for actual FCM send

## Common Troubleshooting

1. `/status` returns 204

- AI stream is empty. Start simulator and check `sensing` logs.

2. `ws://localhost:8000/ws/monitor` fails

- Confirm API image includes `websockets` package.
- Rebuild API: `docker compose up -d --build api`

3. Redis connection errors right after restart

- Brief startup race can happen while Redis restarts.
- Retry after a few seconds; services auto-reconnect.

4. No node marked online

- Ensure simulator sends packets to `127.0.0.1:5005`
- Check `docker compose ps` and `docker logs rp5-sensing`

## Stop / Reset

Stop only:

```powershell
docker compose down
```

Stop and remove volumes (full reset):

```powershell
docker compose down -v
```

## GitHub Publish Checklist

Before first push, verify:

1. `docker compose up -d --build` completes without errors
2. `http://localhost:8000/status` returns live JSON
3. `monitor.html` opens and WebSocket connects
4. `.gitignore` includes secrets and runtime artifacts
5. Firebase key file is not committed (`api/auth/firebase_key.json`)
6. Large model files (`*.onnx`) are excluded or managed via Git LFS

Suggested first push commands:

```powershell
git init
git add .
git commit -m "Initial release: SafeWave-AI monitoring stack"
git branch -M main
git remote add origin <your-github-repo-url>
git push -u origin main
```