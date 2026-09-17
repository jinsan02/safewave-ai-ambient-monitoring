# SafeWave validation status

Last audited: 2026-09-17 (observations below; see "2026-09-17 device observations")
Previous full audit: 2026-09-02 at `324ae4354761b1caa13c260e532c067d5efd7e41` (`develop`)

## How to read this page

This page separates implementation from evidence. Status labels mean:

- **Implemented**: code or interface exists.
- **Automated test**: a repeatable test script exists; it is not sensor/model accuracy evidence.
- **Simulator**: synthetic or injected inputs exercised the path.
- **Device evidence**: a raw log tied to a Raspberry Pi 5 run exists in the repository.
- **Data needed**: accuracy/error needs labelled sensor or audio data.
- **Planned**: design or configuration exists without completion evidence.

The repository is a systems prototype, not a medical device. No item below should be read as
clinical validation.

## Evidence matrix

| Area | Implemented | Automated test | Simulator | RPi5 device evidence in Git | Data needed / planned | Current claim boundary |
|---|---|---|---|---|---|---|
| Docker Compose services | Yes | Compose configuration can be validated | Dummy injection supported | **No raw run log** | RPi5 full-stack run | Architecture and configuration are implemented; RPi5 completion is unverified here. |
| CSI UDP ingestion / Redis | Yes | Packet and integration paths exist | `dummy_inject.py` | **No raw run log in Git** | Live ESP32 capture | Wire/interface behavior can be reproduced without claiming sensing accuracy. |
| M1 interface | Yes, input `(1, M1_MAX_NODES, 64, 100)` and fall-score output; runtime uses 100Hz zero-fill, trained-node tail gate, and K=3/N=5 event aggregation | Runtime-input and risk-gate tests cover plumbing | Synthetic CSI | **No post-change accuracy log in this repo** | Labelled multi-person fall/non-fall CSI and RPi5 live capture | The export script in this repo creates an untrained simplified network. The integrated trained 3-node model and its accuracy evidence live in the separate pose handoff; this repository has not validated post-change RPi5 detection. |
| M2 interface | Yes, HR/RR output with ONNX or FFT fallback | Boundary/pipeline tests cover plumbing | Synthetic signals | **No paired reference log** | CSI paired with reference HR/RR | The export script creates an untrained network. No HR/RR MAE or clinical accuracy is established. |
| M3 interface | Partial | `test_m3_m4_experts.py` exists | Synthetic/injected audio | **No labelled device evaluation** | Correct feature extraction and labelled household audio | AST produces a generic AudioSet top class; the seven-class label is selected by a heuristic. Runtime pads/reshapes raw waveform instead of applying the expected log-Mel feature extractor, so AST semantics and seven-class accuracy are unverified. |
| M4 Korean STT | Yes when Whisper artifacts and dependencies load | Expert test exists | Injected/upstream text path | **No RPi5 WER log** | Representative Korean speech with transcripts | Export/runtime integration exists; no WER or real-room robustness result is established. |
| M5 risk decision | Yes, GGUF/ONNX backends and rule gate | Rule and pipeline tests exist | Synthetic scenario sets | **No RPi5 benchmark report** | Device latency and real-event outcomes | Qwen-LMOps evaluation figures are model/prompt evaluation, not SafeWave clinical or sensor accuracy. |
| API / WebSocket / MQTT | Yes | Integration paths exist | Dummy streams | **No sustained RPi5 report** | Load/stability run | Endpoint and message contracts are implemented. Operational availability is unverified. |
| Phase 2 TTS/STT/FCM | Yes | Logic paths exist | Can be injected | **No complete device evidence in Git** | Speaker, microphone, network and FCM credentials | Workflow is implemented; real-home completion/reliability is unverified. |
| Dashboard | Yes | No browser E2E suite found | Uses injected/live streams | **No RPi5 usability evidence** | Device/browser test | UI implementation is demonstrable; usability and operational performance are unverified. |
| RPi5 CPU/thread tuning | Configuration implemented | No reproducible benchmark test | N/A | Commit history records a pre-change observation, but **no post-change raw log/report is tracked** | Rerun `benchmark_template.md` on RPi5 | Treat thread counts and CPU allocation as tuning choices, not a verified reduction. |

## Model artifacts and reproducibility

`volumes/models/` is excluded from Git. This laptop currently contains local M1–M5 artifacts,
but a fresh GitHub clone does not receive them.

| Model | Local laptop state | Public-clone state | Interpretation |
|---|---|---|---|
| M1 | ONNX file present | absent | File presence proves loadability only. Export uses random initialization and no trained checkpoint. |
| M2 | ONNX file present | absent | File presence proves loadability only. Export uses random initialization and no trained checkpoint. |
| M3 | AST ONNX files present | absent | Pretrained AST export artifact; seven-class SafeWave accuracy is unmeasured. |
| M4 | Whisper ONNX encoder/decoder files present | absent | Export/runtime artifact; Korean WER and RPi5 latency are unmeasured. |
| M5 | Qwen 0.5B ONNX and 1.5B GGUF Q5 files present | absent | Default runtime targets 1.5B GGUF; `export_m5_qwen_onnx.py` is a legacy 0.5B exporter and does not create the default GGUF artifact. |

The export scripts therefore do not make a fresh clone production-ready. M1/M2 require trained
checkpoints and evaluation; M3/M4/M5 require model download/export plus licensing, checksum, and
hardware-specific verification.

## Results that must not be merged

- `docs/benchmark_template.md` is blank by design and contains no RPi5 result.
- Development-PC and RTX laptop latency reports are not Raspberry Pi 5 measurements.
- Local ignored reports are not public provenance and lack enough hardware metadata to become RPi5 evidence.
- Historical commit messages mention a Raspberry Pi 5 idle-CPU observation before the current
  tuning, while the same history requests a post-change remeasurement. The post-change result is
  therefore **evidence insufficient**.
- A synthetic rule/LLM score is not fall accuracy, vital-sign error, clinical sensitivity, or
  real-world reliability.

## 2026-09-17 device observations

Observations made over SSH/Redis while preparing the RPi5 baseline. No raw log is committed yet, so
these are **not** device evidence under the definitions above.

- Device: Raspberry Pi 5 Model B Rev 1.1, 8 GB, 4 cores, kernel 6.12.75+rpt-rpi-2712, NVMe root.
- Live ESP32-S3 CSI from nodes 1–3 at about 300 packets/s in total.
- A 5-second counter sample showed node 1 missing about 35% of its sequence range while nodes 2 and 3
  were near 0%. The lifetime `node:1:health` counter had accumulated an implausible value from
  earlier sequence jumps; compare deltas between two samples rather than the lifetime ratio.
- Before this update the RPi5 ran an image built from `a778480`; it reported repeated
  `csi_backlog_skipped` with three nodes. The post-update stack has not been measured yet.
- `docker stats` reports `0B` memory on this RPi5 because the memory cgroup is not enabled.
  Use host memory or process RSS for memory measurements.

Afternoon additions (same day, raw data in the ignored `reports/rpi5-20260917/`, summary in
`handoff/rpi5-20260917/`):

- M4 on the RPi5 (28 directly recorded clips of the teammate's fixed evaluation set, service decoding
  path, no repetition guard, 2 threads, other AI services stopped): fp32 `SungBeom/whisper-small-ko`
  CER 44.2% / WER 80.0% / keyword 46.4% / 8.45 s per clip / peak RSS 4,666 MB; teammate INT8 with a
  patched `generation_config.json` CER 15.2% / WER 27.5% / keyword 75.0% / 4.43 s / 2,259 MB.
  This set was used for model selection, so it is not an independent test result.
- Full stack (all models, speech every 15 s, M5 forced) for 120 s: the fp32 run restarted containers
  (OOM) and is invalid; the INT8 run completed with no restarts, but speech event-to-result delay was
  30.7 s p50 and ai-qwen RSS grew from 2.0 GB to 4.3 GB.
- **Before the local integration change, M1 never received a real CSI window** in any pipeline run that day:
  its score stayed at the all-zero-input output. The old CSI loop could not keep up with per-packet M1
  inference, skipped backlog, and reset node buffers. No M1 detection behaviour on the RPi5 is established.
- The local change now uses the trained-node gate and K/N aggregation, but the RPi5 has not run it yet.
  The RPi5 boots to the console (desktop GUI disabled) and uses the INT8 M4 by default.

### Measurement caveats found

- M3/M4 run in the audio worker thread, not in `process_experts`. `expert_latency_ms.env_sound`
  and `expert_latency_ms.speech_ko` in `ai:result` therefore measure an empty-input path (~0 ms)
  and must not be reported as M3/M4 latency. Use `audio:result` stream-ID time minus the payload
  `ts_ms` (event → result delay).
- On the development laptop (x86, CPU ORT), one injected audio event took about 2.75 s for M3+M4
  while events arrived every 2.0 s, so the delay grew linearly (3.2 s → 17.1 s) until injection
  stopped. If the RPi5 per-event time also exceeds the event interval, Phase 2 replies can miss
  the 15 s window. This is a laptop observation, not an RPi5 result.
- The local M1 warm-up now uses `(1, M1_MAX_NODES, 64, 100)`; this is a code change awaiting RPi5
  startup verification.

## Research outcome

The paper **“WiFi CSI와 음향 데이터의 다중 모달 융합을 통한 독거노인 낙상 감지 시스템 설계 방향 고찰”**
received a Bronze Award in the 2026 Korean Digital Contents Society Summer Conference undergraduate
paper competition on 2026-07-03. This is evidence of a research/presentation outcome connected to
the capstone direction. It is **not** evidence that SafeWave’s fall model accuracy, vital-sign
accuracy, clinical safety, or Raspberry Pi 5 performance was validated.

## Reproduction boundary

Reproducible from source without sensors or model artifacts:

- inspect Compose, Redis, MQTT, API, dashboard, rule-gate, and M1–M5 interfaces;
- run static/config checks and rule/pipeline tests when Python dependencies are available;
- inject synthetic CSI/audio to exercise data flow.

Requires hardware, external artifacts, credentials, or labelled data:

- live ESP32 CSI and microphone behavior;
- M1/M2 accuracy and error metrics;
- M3 class accuracy and M4 WER;
- M5 and full-stack Raspberry Pi 5 latency, memory, thermal, and stability results;
- TTS playback, FCM delivery, and real-home operation.

During the 2026-09-02 audit, `docker compose config --quiet` completed. The Docker daemon was not
running and the host’s Python launcher was inaccessible, so the Python test scripts could not be rerun. This audit does not
claim a fresh automated-test pass. The scripts remain present and should be rerun in a provisioned
environment before release.
