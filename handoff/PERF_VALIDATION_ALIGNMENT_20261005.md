# 성능 검증 3환경(노트북 GPU / 노트북 CPU / RPi5) — 미리 맞출 것 (2026-10-05, 로컬)

작성: 노진산(통합). 노트북 쪽은 실제 컨테이너·파일로 확인했고, RPi5 쪽은 `HANDOFF.md`(09-17 기록) 기준이다. **RPi5는 접속해서 다시 확인해야 한다.**

## 1. 지금 상태와 차이
| 항목 | 노트북 GPU | 노트북 CPU | RPi5 | 맞출 것 |
|---|---|---|---|---|
| 코드 | `f848629` = 이미지 코드 해시 일치 [확인] | 같음 [확인] | **`5b30f96`(09-17)**: 09-23~25 수정(M1 게이트, M3 v34, M4 후처리, M5 프로필 등)이 없음 | RPi5 `git pull` + 재빌드 |
| 이미지 | Python 3.10, ORT 1.22(CUDA), torch cu128, transformers 4.47.1, llama-cpp 0.3.35 | Python 3.12, ORT 1.18, transformers 4.47.1, llama-cpp 0.3.35 | ARM64 빌드(같은 Dockerfile cpu-runtime, ORT 1.18 예상) | 버전 기록. GPU만 ORT 1.22임을 표에 명시 |
| M1 모델 | `m1_wifi_pose.onnx` sha256 `899dde8f…` | 같음 | 09-17 교체 기록 있음 | **sha256 대조** |
| M3 모델 | `v34_homepos.onnx` `d06265e9…`(FP32) | 같음 | **미설치**(HANDOFF "RPi5에도 설치 필요") | 설치 + 대조. INT8(`feature/ast-base`)은 별도 줄로 할지 결정 |
| M4 모델 | `whisper_onnx_int8_ft_svc`(인코더 `95ffe24c…`) | 같음 | 같은 폴더가 `.env` 기본(HANDOFF) | sha256 대조 |
| M5 모델 | `qwen2.5-1.5b-instruct-q5_k_m.gguf` `b4666107…` | 같음 | 같은 파일 예상 | 대조 |
| M5 프롬프트 | **`laptop`**(약 3,170토큰, 캐시 256 MB) | **`laptop`** | 기본 **`rpi5`**(약 740토큰) | **비교 기준은 3곳 모두 `rpi5`**. 노트북 `laptop`은 추가 줄 |
| 스레드 | ORT 4 / M3 8 / M4 8 / M5 8(노트북 최대 성능) | 같음 | 기본 ORT 1 / M3 1 / M4 2 / M5 2 | 노트북 CPU를 **'RPi5 스레드값'**과 **'최대 성능'** 두 줄로. 지금 `compose.cpu.yml`은 최대 성능뿐 |
| 메모리 한도 | ai-experts·ai-qwen 6 g | 3 g(기본) | 3 g(기본) | 표에 명시 |
| 음성 확인 | `VOICE_ENABLED=true` | true | 기본 false | 성능 측정 때는 **3곳 모두 false**(TTS 대기가 지연에 섞임). 음성 흐름은 따로 잰다 |
| 운영 설정(`sys:settings`) | 비어 있음 → 코드 기본값(M2 끔, 임계 0.6) | 같음 | 미확인 | 측정 전 **같은 값을 명시적으로 넣고** 기록 |

## 2. 입력을 같게 (가장 중요)
| 입력 | 문제 | 맞추는 법 |
|---|---|---|
| CSI | ESP32는 **노트북 IP(192.168.1.11)로 보낸다**. RPi5는 받지 못한다. 실시간 CSI는 매번 다르다 | **기록 재생 도구**(npz/CSV → 788 B UDP, 원래 시각 간격 유지)로 3곳에 같은 CSI를 넣는다. 빈 방 113분 일부 + 영상 시험 기록. 지금은 합성 시뮬레이터(`sim_esp32.py`)뿐이라 **새로 만들어야 한다** |
| 소리(M3·M4) | 마이크·방 소음이 다르다 | 파일 주입으로 통일(`sim_esp32.py --audio-dir`, `bench_rpi5.py --audio`, `eval_m4_stt.py`, `m3_fall_repro.py`) |
| M5 | — | 같은 케이스 세트(held-out seed 4047·M2-off 5051)를 `eval_qwen_accuracy.py`로 |

## 3. 측정 조건
- **공통**
  - 예열(첫 추론) 제외, 같은 측정 시간·반복 수
  - 측정마다 커밋·모델 해시·환경변수·`docker stats`·호스트 메모리를 기록
- **지연 측정법(CLAUDE.md)**
  - M3·M4는 `audio:result` 스트림 ID 시각 − payload `ts_ms`(`expert_latency_ms` 쓰지 않음)
  - 패킷 손실은 두 시점의 `rx`·`last_seq` 증가량
- **시계**: 주입은 **측정 대상 장비 안에서** 한다. 노트북에서 RPi5 Redis로 넣으면 두 시계 차가 지연에 섞인다.
- **노트북**: 전원 연결, 고성능 전원 모드, 다른 무거운 프로그램 종료. GPU 측정 때는 CPU 이미지를 정지한다.
- **RPi5**
  - 재부팅 후 측정. 스왑이 가득했던 기록(09-17)이 있다.
  - 데스크톱 GUI 끔, 쿨러 확인, 시작 온도 기록
  - 측정 중 SSH 세션은 최소로 하고, 결과는 노트북으로 동기화한다.
- **정확도**: M3·M4는 같은 결과가 나오는지 **일치 여부**를 본다. M5는 GPU·CPU 결과가 다를 수 있으니 재측정한다. RPi5는 시간이 걸리므로 케이스 수를 미리 정한다(예: M4 200건 × 4.4 s ≈ 15분).

## 4. 결과 형식(시각화용)
- 모든 결과를 같은 JSON 키로 남긴다: `env`(laptop_gpu / laptop_cpu_rpi5eq / laptop_cpu_full / rpi5), `commit`, `models`, `env_vars`, 측정값
- 그림
  - 지연: 막대·분포
  - 모델 × 환경: 히트맵
  - 자원: 시간축 그래프
  - 소리·CSI: 스펙트로그램

## 5. 준비 작업 목록
1. CSI 기록 재생 도구(npz/CSV → UDP 788 B, 시각 간격 유지)
2. `compose.cpu_rpi5eq.yml`(RPi5 스레드·메모리 한도, `SLM_PROMPT_PROFILE=rpi5`, 음성 확인 끔)
3. 노트북 GPU·CPU 측정용 오버라이드에 `SLM_PROMPT_PROFILE=rpi5`·음성 확인 끔 변형
4. RPi5: `git pull`, M3 v34 설치, 모델 sha256 대조, 재빌드, `.env`·`sys:settings` 확인
5. 측정 묶음 스크립트(환경 이름만 바꿔 같은 순서로 실행) + 결과 모으기·그림 스크립트
6. 로컬에만 있는 도구(`m1_event_test.py`, `m1_false_alarm_analysis.py`, 고친 `validation_recorder.py`)를 RPi5에서 쓸지 결정 → 커밋하거나 복사
