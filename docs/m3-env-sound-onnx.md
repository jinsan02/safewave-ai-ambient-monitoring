# M3 환경음 모델 — 베이스 AST → SafeWave 파인튜닝 ONNX

> 대상: `ai/experts/m3_ast_base.py` (M3 전문가 모델)
> 모델: `v34_homepos` (AST 6-class, end-to-end ONNX) · 2026-09-21

집 안 마이크 소리를 6종으로 분류하고, 그중 **impact(충격음, 낙상)** 확률로 알림을 올리는
모델이다. 이 문서는 **무엇이 바뀌었는지**, **가중치를 어디서 받는지**, **어떻게 호출하는지**
세 가지를 담는다.

---

## 1. 베이스 → 현재, 무엇이 바뀌었나

### 한 줄 요약

**범용 모델 + 키워드 매핑**에서 **낙상 전용으로 학습시킨 모델**로 바꿨다.

### 바뀐 내용

| | 이전 (베이스 AST) | 현재 (파인튜닝 ONNX) |
|---|---|---|
| 모델 | `MIT/ast-finetuned-audioset-10-10-0.4593` — AudioSet 527종 범용 분류기 | 위 모델을 SafeWave 데이터로 파인튜닝한 **6-class 전용 모델** |
| 라벨 결정 | 527종 출력을 **문자열 키워드로 6종에 매핑** (`"crash"`, `"bang"`, `"thump"` 등이 들어가면 impact) | 모델이 **6종을 직접 출력** |
| 실행 방식 | transformers + torch로 직접 추론 | **ONNX 런타임** (torch/transformers 불필요) |
| 전처리 | 런타임이 게인 정규화 후 feature extractor 호출 | **ONNX 그래프 안에 포함** (게인 정규화 + Kaldi log-mel) |
| 무음 판정 | 없음 | **raw_peak 게이트**로 규칙 판정 |
| 낙상 알림 | 없음 (라벨만 반환) | **impact 확률 + 임계값**으로 `impact_alert` |
| 모델 파일 | `volumes/models/ast_hf/` (HF 스냅샷, safetensors + config) | `volumes/models/ast_onnx/*.onnx` (단일 파일) |

### 왜 바꿨나

1. **키워드 매핑은 학습된 판단이 아니다.** 이전 방식은 AudioSet 라벨 이름에 `"thump"`, `"bang"`
   같은 단어가 들어가면 impact로 쳤다. 실제 집 안에서 나는 낙상 소리를 그 모델이 그렇게 부를
   보장이 없고, 반대로 물건 떨어뜨리는 소리도 똑같이 불린다.

2. **전처리가 학습과 어긋나면 성능이 통째로 무너진다.** 이 프로젝트는 과거 윈도우 길이
   불일치(4초 vs 3초)로 실제 성능이 무너진 적이 있다. 그래서 전처리를 **모델 안에 넣어**
   런타임이 재현할 여지 자체를 없앴다. 런타임이 하는 일은 "16kHz mono 3초로 맞춰 넣기"가 전부다.

3. **의존성이 줄었다.** M3만 놓고 보면 torch·transformers가 필요 없어졌다.
   (M4 Whisper가 아직 쓰고 있어 `requirements.txt`에서 빼지는 않았다.)

### 파인튜닝에 쓴 데이터

| 소스 | 쓰임 |
|---|---|
| AI-Hub 위급상황 음성/음향 | 낙상 충격음(impact), 고통 발화(speech) |
| SAFE (Kaggle fall-audio-detection) | 낙상 impact / 일상행동 |
| ESC-50 | alarm(사이렌·경보), impact(유리 파손), 환경음 |
| 자택 실환경 녹음 | 생활 소음 하드 네거티브 + 자택 낙상 양성 |

학습·평가 파이프라인은 별도 저장소(`ast-base`)에 있다.

### 측정 결과

| 항목 | 결과 |
|---|---|
| 실환경 낙상 감지 (50회) | **46/50 (92%)** — 메인방 10/10, 부엌 문열림·문닫힘 각 10/10, 화장실 문열림 10/10, 화장실 문닫힘 6/10 |
| ONNX ↔ 원본(PyTorch) 일치 | 확률 최대 차이 `5.5e-06`, 라벨 48/48 일치 |
| 추론 지연 (CPU) | 윈도우 1개 약 1.0~1.5초 |

화장실 문닫힘이 낮은 것은 **타일 잔향** 때문으로, 학습 데이터의 합성 리버브가 실제 잔향을
덜 재현하는 알려진 항목이다.

---

## 2. 모델 가중치 (.onnx)

### 파일

| 파일 | 크기 | 설명 |
|---|---|---|
| `v34_homepos.onnx` | 330.3 MB | 모델 본체 (전처리 포함) |
| `model_spec.json` | 1.5 KB | 입출력·전처리 상수·권장 운영값 (기계 판독용) |
| `labels.json` | 0.1 KB | 라벨 id → 이름 |

```
sha256(v34_homepos.onnx) = d06265e9401a4fe824a8e6c5cee68f8200acdbf04b6374c3e02bc44e01cdb615
opset 17 · 파라미터 86.5 M (float32) · 세션 메모리 약 0.57 GB
```

### 받기 · 설치

`volumes/models/` 는 Git에 포함되지 않는다. Hugging Face 버킷에서 받는다.

```bash
hf sync hf://buckets/sobh6498/ast-finetuned-audioset-10-10-0.4593-bucket/ast-base/exports/ast_onnx ./ast_onnx
python scripts/setup_m3_ast_onnx.py --src ./ast_onnx
```

`setup_m3_ast_onnx.py` 는 `volumes/models/ast_onnx/` 로 복사한 뒤 **런타임 계약을 검증**한다 —
입력 이름·형상, 출력 3종, 클래스 수, 확률 합, `raw_peak` 일치, sha256. 이미 설치돼 있으면
`--verify-only` 로 검증만 다시 돌린다.

### 교체할 때

입출력 규격과 라벨 순서가 고정이므로 **`.onnx` 파일만 바꾸면 된다.** 코드 수정은 필요 없다.
파일명이 `m3_env_sound.onnx` / `v34_homepos.onnx` / `ast.onnx` 중 하나면 자동으로 찾고,
아니면 `M3_ENV_SOUND_ONNX` 로 파일명을 지정한다.

---

## 3. 구동 코드 — 입력과 출력

### 3.1 모델 입출력 (ONNX 그래프)

| 구분 | 이름 | 타입 | shape | 설명 |
|---|---|---|---|---|
| 입력 | `waveform` | float32 | `[batch, 48000]` | 16 kHz · mono · 3.0초 · 진폭 −1.0~1.0 |
| 출력 | `probs` | float32 | `[batch, 6]` | softmax 확률, 합 = 1 |
| 출력 | `logits` | float32 | `[batch, 6]` | softmax 이전 값 (보통 안 씀) |
| 출력 | `raw_peak` | float32 | `[batch]` | **증폭 전** 원본 최대 진폭. 무음 게이트 판정용 |

**라벨 순서는 고정이다. 인덱스를 바꾸면 안 된다.**

| id | 라벨 | 의미 |
|---|---|---|
| 0 | `silence` | 무음 |
| 1 | `speech` | 사람 음성·신음 |
| 2 | `impact` | **충격음 (낙상 감지 대상)** |
| 3 | `noise` | 정의상 유지되는 슬롯 (학습 데이터는 `unknown`에 통합) |
| 4 | `alarm` | 경보음 |
| 5 | `unknown` | 그 외 생활음 |

### 3.2 그래프에 포함된 전처리 — 런타임이 중복으로 하면 안 되는 것

1. **조용한 소리 게인 정규화** — `raw_peak < 0.15` 이면 peak가 0.9가 되도록 증폭 후 clip.
2. **Kaldi log-mel filterbank** — 25 ms 프레임 / 10 ms 홉 / hann(periodic=False) /
   preemphasis 0.97 / DC offset 제거 / 128 mel / 512 FFT / low_freq 20 Hz
   → 1024 프레임 zero-pad → `(x − (−4.2677393)) / (4.5689974 × 2)`.

> 런타임에서 따로 볼륨 정규화·AGC·노이즈 서프레션을 걸면 학습 시 전처리와 어긋난다.
> **마이크 PCM을 16 kHz mono float32로 바꾸는 것까지만** 하고 넘긴다.

### 3.3 모듈 입력 (`EnvSoundAnalysisModel.infer`)

두 가지 형태를 다 받는다.

```python
model.infer(waveform_ndarray)                                  # 16kHz 로 간주
model.infer({"waveform": waveform, "sample_rate": 16000})      # 권장 — 리샘플 가능
```

길이는 맞춰서 넣지 않아도 된다. 모듈이 정리한다.

| 입력 길이 | 처리 |
|---|---|
| 3초 미만 | **앞쪽**(과거 방향)을 0으로 채운다 |
| 정확히 3초 | 그대로 |
| 3초 초과 | **가장 큰 진폭을 품는 3초**를 고른다 (`M3_WINDOW_MODE=peak`, 기본값) |
| 없음 / 빈 배열 | 추론하지 않고 `env_sound_source="no-audio"` 반환 |
| 샘플레이트 ≠ 16 kHz | 선형 보간 리샘플 (안전 경로. `audio-sensing` 기본값은 16 kHz) |

> **창 선택이 감지율을 좌우한다.** VAD 이벤트는 최대 6초인데(`AUDIO_MAX_EVENT_SECONDS`),
> 낙상은 '쿵' 이후 신음·뒤척임이 이어져 이벤트가 길어진다. 이때 마지막 3초만 자르면
> 정작 충격음이 창 밖으로 밀린다. 낙상 50개 실측:
>
> | 충격음 위치 (창 끝 기준) | 감지 |
> |---|---|
> | 0.3 ~ 1.5초 전 | 46/50 (92%) |
> | 2.2초 전 | 44/50 (88%) |
> | 2.9초 전 | 37/50 (74%) |
> | 3.5초 전 (창 밖) | 12/50 (24%) |
>
> 그래서 `main.py` 는 M3에 3초가 아니라 **6초**(`M3_AUDIO_MERGE_MS`)를 병합해 넘기고,
> 어느 3초를 볼지는 M3가 정한다.

### 3.4 모듈 출력

```json
{
  "env_sound_label": "impact",
  "env_sound_confidence": 0.71,
  "env_sound_source": "onnx",
  "activity": "impact",
  "activity_confidence": 0.71,

  "impact_prob": 0.7124,
  "impact_alert": true,
  "raw_peak": 0.184713,
  "silence_gated": false,
  "env_sound_probs": {
    "silence": 0.0012, "speech": 0.1803, "impact": 0.7124,
    "noise": 0.0001, "alarm": 0.0003, "unknown": 0.1057
  },
  "env_sound_model": "v34_homepos"
}
```

| 키 | 타입 | 설명 |
|---|---|---|
| `env_sound_label` | str | 6종 중 하나. 게이트에 걸리면 무조건 `silence` |
| `env_sound_confidence` | float | 위 라벨의 확률. 게이트 판정 시 `1.0` |
| `env_sound_source` | str | `onnx` / `heuristic`(모델 파일 없음) / `no-audio`(입력 없음) |
| `activity`, `activity_confidence` | | 기존 코드 호환용 별칭 |
| `impact_prob` | float | **충격음 확률.** 알림 임계값 튜닝은 이 값으로 한다 |
| `impact_alert` | bool | `impact_prob >= M3_IMPACT_THRESHOLD` 이고 게이트에 안 걸렸을 때 true |
| `raw_peak` | float | 증폭 전 원본 최대 진폭 |
| `silence_gated` | bool | 게이트 규칙으로 silence 확정됐는지 |
| `env_sound_probs` | dict | 6종 전체 확률 (임계값 재계산·디버깅용) |
| `env_sound_model` | str | 실제로 로드된 모델 파일명 |

`main.py` 가 Redis(`ai:m3:latest`)에 쓸 때 시각 필드를 덧붙인다.

| 필드 | 의미 |
|---|---|
| `audio_ts_ms` | 분석 오디오 구간 **끝** 시각 (Unix ms) |
| `audio_ts_start_ms` | 구간 시작 추정 (`audio_ts_ms - audio_duration_ms`) |
| `audio_duration_ms` | 병합된 waveform 길이 |
| `audio_window_ms` | 모델이 실제 분석한 창 (3000) |
| `audio_merge_ms` | M3에 넘긴 병합 구간 (기본 6000) |

> "몇 시에 소리 났는지"는 `audio_ts_ms` 또는 `audio_ts_start_ms`를 쓴다.
> 스냅샷 최상위 `ts_ms`는 CSI 기준이라 수백 ms~수 초 차이날 수 있다.

### 3.5 판정 규칙

```
무음 게이트 : raw_peak < 0.005      → 모델 결과 무시하고 silence 확정
낙상 알림   : probs[impact] >= 0.6  → impact_alert = true
```

- **게이트(0.005)** — 학습 데이터에 silence 클래스가 없어 모델이 "조용함"을 직접 예측하지
  않는다. 그래서 증폭 전 원본 peak으로 규칙 판정한다. 스윕 실측으로 정한 값이다.
- **임계값(0.6)** — 올리면 알림이 줄고 내리면 늘어난다. 환경 변수라 재배포 없이 조정된다.

### 3.6 환경 변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `M3_ENV_SOUND_MODEL` | `ast_onnx` | `MODEL_PATH` 아래 모델 디렉터리 |
| `M3_ENV_SOUND_ONNX` | (자동 탐색) | 디렉터리 안에서 쓸 `.onnx` 파일명 고정 |
| `M3_SILENCE_GATE` | `0.005` | 무음 게이트 (0이면 끔) |
| `M3_IMPACT_THRESHOLD` | `0.6` | 낙상 알림 임계값 (0이면 끔) |
| `M3_WINDOW_MODE` | `peak` | 3초 초과 입력에서 분석할 3초 선택. `peak` / `latest` |
| `M3_AUDIO_WINDOW_MS` | `3000` | 모델 창 길이 |
| `M3_AUDIO_MERGE_MS` | `6000` | M3에 넘길 병합 길이 |
| `M3_SAMPLE_RATE` | `16000` | 입력 샘플레이트 |

### 3.7 최소 사용 예

```python
from experts.m3_ast_base import EnvSoundAnalysisModel

m3 = EnvSoundAnalysisModel("/app/models/ast_onnx")

result = m3.infer({"waveform": pcm_float32, "sample_rate": 16000})

if result["impact_alert"]:
    notify_fall(confidence=result["impact_prob"])
```

---

## 4. 확인 방법

```bash
# 모델 계약 검증 (설치 후 1회)
python scripts/setup_m3_ast_onnx.py --verify-only

# 단위 테스트 — 모델이 없으면 ONNX 테스트는 자동으로 건너뛴다
python -m unittest tests.test_m3_onnx -v
```

정상이면 무음 입력은 `silence (silence_gated=true)`, 충격음 입력은 `impact_alert=true` 가 나온다.

## 5. 자주 나오는 질문

**Q. 3초보다 짧은 오디오를 넣어도 되나?**
→ 된다. 모듈이 앞쪽을 0으로 채운다. 다만 소리가 너무 짧으면 판단 근거가 줄어든다.

**Q. `noise` 라벨은 언제 나오나?**
→ 학습 데이터에서 `unknown`으로 통합했기 때문에 실제로는 거의 안 나온다. 출력 레이어는
6-class를 유지하고 있어 인덱스만 자리를 차지한다.

**Q. 모델 파일이 없으면 어떻게 되나?**
→ 죽지 않고 파형 휴리스틱으로 동작하며 `env_sound_source`가 `heuristic`이 된다.
운영에서 이 값이 보이면 모델이 로드되지 않은 것이다.

**Q. GPU를 쓰나?**
→ `ORT_USE_GPU=1` 이면 사용 가능한 프로바이더를 순서대로 시도한다(`ai/utils`). 기본은 CPU다.
