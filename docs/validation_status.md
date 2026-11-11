# SafeWave validation status

Last evidence update: 2026-10-13 (source HEAD `5fc2fd3`; 2026-10-12 live laptop capture and 2026-10-13 three-environment replay, with local raw results outside Git)
Last broad code/document cross-check: 2026-10-08 (`fc4430e`)
Previous full audit: 2026-09-02 at `324ae4354761b1caa13c260e532c067d5efd7e41` (`develop`)

## How to read this page

This page separates implementation from evidence. Status labels mean:

- **Implemented**: code or interface exists.
- **Automated test**: a repeatable test script exists; it is not sensor/model accuracy evidence.
- **Simulator**: synthetic or injected inputs exercised the path.
- **Device evidence**: a raw log tied to a Raspberry Pi 5 run exists in the repository.
- **RPi5 measured (raw log outside Git)**: the run was made on the Raspberry Pi 5 and is summarized here, but its raw
  logs live only in ignored local folders (`reports/…` or `data/replay_bench/…`). It cannot be re-checked from a fresh clone, so it is weaker than device evidence.
- **Data needed**: accuracy/error needs labelled sensor or audio data.
- **Planned**: design or configuration exists without completion evidence.

The repository is a systems prototype, not a medical device. No item below should be read as
clinical validation.

## Evidence matrix

| Area | Implemented | Automated test | Simulator | RPi5 device evidence in Git | RPi5 measured (raw log outside Git) | Data needed / planned | Current claim boundary |
|---|---|---|---|---|---|---|---|
| Docker Compose services | Yes | Compose configuration can be validated | Dummy injection supported | **No raw run log** | 2026-09-17 and 2026-10-05 runs; 2026-10-13 same-input replay | Sustained live RPi5 full-stack run | Architecture and configuration are implemented; replay exposed an RPi5 audio bottleneck, not operational readiness. |
| CSI UDP ingestion / Redis | Yes | Packet and integration paths exist | `dummy_inject.py` | **No raw run log in Git** | 2026-10-05: 5 N_pose nodes at 50 Hz, 0% loss over 60 s; 2026-10-13: recorded board packets replayed over UDP | Sustained live ESP32 capture and board coverage diagnosis | Wire/interface behavior can be reproduced without claiming sensing accuracy. Board coverage fell again after reboot in the 10-12 laptop live capture; firmware cause remains a hypothesis. |
| M1 interface | Yes. Since 2026-10-05 the laptop and RPi5 deployments use N_pose board inference (`M1_SOURCE=board`: CSR! scores → `m1:score` → per-node K/N on the hub); the Compose default stays `onnx`. Hub ONNX path (`M1_SOURCE=onnx`): input `(1, M1_MAX_NODES, 64, 100)` and fall-score output; runtime uses 100Hz zero-fill, trained-node tail gate, and K=3/N=5 event aggregation | Runtime-input and risk-gate tests cover plumbing | Synthetic CSI | **No post-change accuracy log in this repo** | 2026-10-05 board path; 2026-10-13 replay of real staged input, with no 3/5 board vote | Labelled multi-person fall/non-fall CSI and RPi5 live capture | The replay did not establish M1 detection. Its test settings were score ≥0.7 and coverage ≥0.5 (Compose defaults remain 0.80/0.9); short idle false-alarm results are not long-term rates. |
| M2 interface | Yes, HR/RR output with ONNX or FFT fallback; **retired by the team, off by default** (`models.m2=false`) | Boundary/pipeline tests cover plumbing | Synthetic signals | **No paired reference log** | — (M2 retired, off by default) | CSI paired with reference HR/RR | The export script creates an untrained network. No HR/RR MAE or clinical accuracy is established. |
| M3 interface | Yes — `v34_homepos` 6-class ONNX (`silence/speech/impact/noise/alarm/unknown`) with gain normalisation and Kaldi log-mel inside the graph; silence gate on pre-gain `raw_peak`, `impact_alert` at `probs[impact] ≥ 0.6` | `test_m3_m4_experts.py` exists | Synthetic/injected audio | **No labelled device evaluation in Git** | 2026-10-05: 50 recorded falls + 16 false-alarm clips; 2026-10-13 replay | Labelled live-room household audio | Recorded-clip result was 46/50 falls on RPi5. In the replay, 53 M3 events were skipped under backlog and only one RPi5 result was comparable to the original; live-room accuracy and false-alarm rate are unverified. |
| M4 Korean STT | Yes when Whisper artifacts and dependencies load | Expert test exists | Injected/upstream text path | **No RPi5 WER log** | 2026-09-17: 28 clips; 2026-10-05: 50-clip subset; 2026-10-13 continuous replay | Representative Korean speech with transcripts | RPi5 replay processed 54% of audio events, with event→result p50 62.3 s and urgent phrases 1/4; original GPU output is not ground truth. No real-room WER or robustness result is established. |
| M5 risk decision | Yes, GGUF/ONNX backends and rule gate; M2-retired gate weights 50/25/25 as of `b716d3c` | Rule and pipeline tests exist | Synthetic scenario sets | **No RPi5 benchmark report in Git** | 2026-10-05: 30 synthetic cases; 2026-10-13 replay inference 14.7–18.3 s | Labelled real-event outcomes | Qwen-LMOps figures are model/prompt evaluation, not SafeWave clinical or sensor accuracy. Replay measures latency and alert ordering, not diagnostic accuracy. |
| API / WebSocket / MQTT | Yes | Integration paths exist | Dummy streams | **No sustained RPi5 report** | 2026-10-05: alert → FCM send on one device | Load/stability run | Endpoint and message contracts are implemented. Operational availability is unverified. |
| Phase 2 TTS/STT/FCM | Yes | Logic paths exist | Can be injected | **No complete device evidence in Git** | 2026-10-05 RPi5 FCM send attempt (voice check off); 2026-10-12 laptop GPU live app acknowledgement | RPi5 speaker, microphone, network and app acknowledgement | Laptop live app acknowledgement reached the server; 2026-10-13 replay used automatic acknowledgement. RPi5 voice check remained off and real-home reliability is unverified. |
| Dashboard | Yes; `monitor_rec.html` and `/monitor/recent` were added by `e4038d0` | No browser E2E suite found | Uses injected/live streams | **No RPi5 usability evidence in Git** | 2026-10-12–13 recording/replay used diagnostic view | Device/browser test | The diagnostic view includes M4 transcript text and is not an app/public data contract; usability and operational performance are unverified. |
| RPi5 CPU/thread tuning | Configuration implemented | No reproducible benchmark test | N/A | **No raw resource log tracked in Git** | 2026-10-05 snapshot; 2026-10-13 replay CPU 77% mean / 98% max, memory peak 7.3 GB used of 7.9 GB total, max 67°C | Compare candidate changes on the same replay input and a live sustained run | Current thread allocation is a tuning choice, not a verified improvement; the replay showed little headroom. |

## Model artifacts and reproducibility

`volumes/models/` is excluded from Git. This laptop currently contains local M1, M3, M4 and M5 artifacts,
but a fresh GitHub clone does not receive them.

| Model | Local laptop state | Public-clone state | Interpretation |
|---|---|---|---|
| M1 | ONNX file present | absent | File presence proves loadability only. Export uses random initialization and no trained checkpoint. |
| M2 | Local ONNX folder removed 2026-10-13 at user request | absent | **Retired by the team; off by default.** The export script remains historical and creates an untrained network. |
| M3 | `ast_onnx/v34_homepos.onnx` present (laptop and RPi5) | absent | Teammate's fine-tuned 6-class model with in-graph preprocessing; install/verify with `scripts/setup_m3_ast_onnx.py`. Recorded-clip results only. |
| M4 | FP32 `whisper_onnx`, original INT8 `whisper_onnx_int8_ft`, and service INT8 `whisper_onnx_int8_ft_svc` present. Local `.env` names FP32, while laptop CPU/GPU Compose overrides and the RPi5 measured configuration name service INT8 | absent | **Measured on the RPi5, raw logs outside Git**: 2026-09-17 28 clips (INT8 CER 15.2%, 4.43 s/clip), 2026-10-05 50-clip subset (CER 8.7%, keyword 84%, p50 5.9 s). Both sets come from the teammate's evaluation set (the 28 clips were also used for model selection). Representative real-room speech is unverified. |
| M5 | Qwen 1.5B GGUF Q5 file and `qwen_15b` tokenizer present; no 0.5B ONNX weight found in this laptop's `volumes/models/` on 2026-10-13 | absent | Default runtime targets 1.5B GGUF; `export_m5_qwen_onnx.py` is a legacy 0.5B exporter and does not create the default GGUF artifact. |

The export scripts therefore do not make a fresh clone production-ready. M1 requires a trained
checkpoint and evaluation (M2 is retired); M3/M4/M5 require model download/export plus licensing, checksum, and
hardware-specific verification.

## Results that must not be merged

- `docs/benchmark_template.md` is blank by design and contains no RPi5 result.
- Development-PC and RTX laptop latency reports are not Raspberry Pi 5 measurements.
- Local ignored reports are not public provenance; even when they include hardware metadata, a fresh clone cannot audit the raw run.
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

## 2026-10-05 three-environment validation (laptop GPU / laptop CPU / RPi5)

These are RPi5 measurements, but under the definitions above they are **not device evidence**: raw logs are kept outside Git in `reports/laptop/val_20261005/` (laptop copy, including the RPi5 results) and RPi5 `~/safewave/reports/rpi5-20261005/`. Code `5ce24da`; the RPi5 audio-path re-check used an overlay of `3552027`. Same evaluation samples on every device.

| Item | RPi5 measured (raw log outside Git) | Boundary |
|---|---|---|
| M3 v34 (FP32) | Fall clips 46/50 (all misses in `bathroom_door_closed`), false-alarm examples 5/16, inference p50 6.0 s | Recorded clips, not live room audio |
| M4 INT8 STT | 50-clip subset CER 8.7%, keyword 84%, p50 5.9 s | Subset of the 2,398-clip set |
| M5 Qwen2.5-1.5B Q5 | 30 random cases (M2 off, rpi5 prompt): 16 exact, 0 under-triage, p50 10.8 s | Synthetic cases, rubric labels |
| Voice emergency: inject → alert record (`ai:emergency`) | 16.8 s before `3552027`, 5.4 s after; 5.3 s when two more sounds were queued right after | Injected recording, one phrase |
| Voice emergency: inject → FCM send attempt | 16.9 s before, 5.5 s after (alert → send 91 / 106 ms); not measured in the queued case | Send time = per-device dedupe key written, not phone delivery; one device; app acknowledgement never received |
| N_pose board M1 | 5 nodes at 50 Hz, board inference 14 ms; 5-min run: 0 alerts, but only 1–14% of windows pass coverage ≥ 0.9 | No fall performed; detection rate unmeasured |
| Boundary checks | 18/18 | Rule logic only |

The laptop GPU/CPU numbers in the same folder are reference baselines and must not be copied into `benchmark_template.md`.

## 2026-10-12–13 live capture and same-input replay

The 2026-10-12 laptop GPU live capture was a staged fall and spoken requests for help, not a labelled population study. A 4 min 47 s segment (five boards; 71,641 CSI packets, 7,162 board scores, 101 audio fragments) was replayed at its original cadence on the laptop GPU, laptop CPU, and RPi5. CSI and board scores were replayed over UDP; audio was inserted into `audio:events`. Thus the RPi5 run is a **device load/latency measurement using recorded real input**, not a fresh live RPi5 sensor observation. The local raw capture, per-run `run.json`, resource samples, comparison, and method notes are under ignored `data/replay_bench/final_3env/`. Source at this documentation audit is `5fc2fd3`, but the run metadata's `laptop_commit` field is `c4c0896` for laptop GPU/CPU and `5fc2fd3` for RPi5; that field alone does not verify the RPi5 checkout or container image. The input was held constant, **not a proven identical-code comparison**. Verify each deployed revision, model file and environment before attributing differences solely to hardware.

| Metric | Original laptop GPU live | Laptop GPU replay | Laptop CPU replay | RPi5 replay |
|---|---:|---:|---:|---:|
| Audio processed | 100 events | 100% | 100% | **54%**; remaining events exceeded the 60 s backlog limit |
| Audio event→`audio:result` p50/p95 | 0.80 / 1.08 s | 0.89 / 1.20 s | 1.96 / 4.09 s | **62.3 / 65.4 s** |
| Urgent phrases observed | 4/4 | 4/4 | 4/4 | **1/4**, about one minute late |
| M5 inference | 1.15 s (record-derived) | 1.04 s | 7.8–13.6 s | 14.7–18.3 s |
| CPU mean / max | — | 8% / 25% | 23% / 100% | 77% / 98% |
| Peak memory used as reported | — | 6.0 GB | 5.6 GB | 7.3 GB of 7.9 GB total |

The replay GPU reproduced the original alert timestamps within ±0.1 s. The laptop CPU and RPi5 had different alert ordering/timing; these are pipeline effects, not direct model-accuracy scores. M3 label and M4 exact-transcript agreement used the original GPU output as reference, **not human ground truth**. The board M1 3-of-5 vote never formed in this segment, so the staged fall was not evidence of M1 detection. Experimental M1 settings were score ≥0.7, coverage ≥0.5, K=3/N=5; Compose defaults remain 0.80 and 0.9. A five-minute idle check at threshold 0.7 saw no false alarm, too short to establish a long-run rate.

The RPi5 pre-/post-reboot audio p50 was 62.0/62.3 s, so reboot alone did not clear the bottleneck in this input. The RPi5 clock was about 2.6 s ahead of the laptop and comparison aligned using the first audio event. Late RPi5 audio and alert records were backfilled from Redis after the recorder stopped (`run.json` marks `backfilled_from_redis`). RPi5 `VOICE_ENABLED=false`. The 2026-10-05 single-phrase RPi5 alert time of 5.4 s must not be presented as the sustained-conversation result.

Separately, the laptop GPU **live** app test recorded urgent speech→rule alert in 0.92 s, FCM send attempt in 1.00 s, and acknowledgement at the server in 6.73 s. The replay inserted acknowledgement automatically after six seconds; it is not another real app acknowledgement, and the laptop result does not establish RPi5 phone-delivery or voice-check reliability.

## Superseded descriptions (kept for history)

- **M3 before 2026-09-23** (audit of 2026-09-02): the runtime used a generic AST ONNX that returned an AudioSet top class;
  a seven-class label (`silence/speech/music/impact/noise/alarm/unknown`) was chosen by a waveform heuristic and only mixed
  with the AST confidence; the raw waveform was padded/reshaped instead of passing through a log-Mel extractor, so AST
  semantics and seven-class accuracy were unverified. The `v34_homepos` merge on 2026-09-23 replaced this path.

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
- M1 accuracy (M2 is retired);
- M3 class accuracy and M4 WER;
- M5 and full-stack Raspberry Pi 5 latency, memory, thermal, and stability results;
- TTS playback, FCM delivery, and real-home operation.

During the 2026-09-02 audit, `docker compose config --quiet` completed. The Docker daemon was not
running and the host’s Python launcher was inaccessible, so the Python test scripts could not be rerun. This audit does not
claim a fresh automated-test pass. The scripts remain present and should be rerun in a provisioned
environment before release.
