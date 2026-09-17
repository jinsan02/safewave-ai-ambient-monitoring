# 이전 CT2 인계 기록 (현재 ONNX 실행 안내 아님)

현재 팀 저장소의 ONNX INT8 인계 안내는 [README.md](README.md)를 따르세요.
아래 내용은 이전 Ldg48/rp5 인계 당시의 실행·측정 기록입니다.

담당: **이대경**. 인계 기준: **2026-09-15 새 파인튜닝 4모델 실험**.

이 폴더는 팀 통합용 독립 패키지입니다. 기존 `ai/main.py`,
`ai/experts/m4_occupancy.py`, 마이크 스크립트 및 운영 기본 모델을 교체하지 않습니다.
현재 저장소의 `m4_occupancy.py`는 재실 감지용 코드이므로 이 음성 모델과 다른 모듈입니다.
팀장이 baseline을 먼저 측정한 뒤 모델을 교체하고 브랜치를 병합하는 순서입니다.

## 1. 무엇이 바뀌었나

```text
Whisper Small
  → seastar105의 Zeroth Korean 파인튜닝 모델: baseline
  → 우리 낙상·도움 요청 데이터로 새 LoRA 학습 1회
  → LoRA 병합: 이번 finetuned 모델
  → 각 가중치를 CT2 FP32 / INT8로 변환: 비교 대상 4개
```

- 출발점: `seastar105/whisper-small-ko-zeroth`의 로컬 원본 가중치.
- 이전 사용자 녹음 모델이나 B 200-step에서 재개하지 않은 **새 학습**입니다.
- Train: AI Hub 5,000 + 직접 녹음 119개를 5회 배치한 595행 = 5,595행.
- Train 고유 음성 경로는 5,119개입니다. 녹음 119개가 595개로 새로 늘어난 것은 아닙니다.
- Validation: 1,000개. Evaluation: AI Hub 지정 미학습분 2,370 + 직접 녹음 28 = 2,398개.
- 평가 분류: `fall_related` 1,199개, `help_direct` 1,199개.
- LoRA: r=16, alpha=32, dropout=0.05, q_proj/v_proj.
- 학습: 1 epoch, 700 optimizer step, batch 1 × accumulation 8, LR=1e-4,
  warmup 50, seed 42, GPU FP16 계산. 최종 step 700 병합 모델을 선택했습니다.
- 모델 구조는 Whisper Small 그대로입니다. INT8은 구조를 축소한 것이 아니라
  가중치 숫자의 정밀도를 낮춘 것입니다.

이번 인계 작업에서는 **추가 학습이나 가중치 수정 없이** 바로 그 모델을 포장했습니다.
최근 6모델 계보 실험이나 종료 토큰 후속 실험의 가중치는 섞지 않았습니다.

## 2. 기존 측정 결과

네 모델 모두 faster-whisper / CT2 / CPU, 동일한 2,398개 평가 데이터,
`selection_regenerate`, 재생성 n=3을 사용했습니다.

| 모델 | CER ↓ | WER ↓ | 키워드 인식률 ↑ |
|---|---:|---:|---:|
| Zeroth baseline FP32 | 46.43% | 97.48% | 69.93% |
| Zeroth baseline INT8 | 37.24% | 86.66% | 70.14% |
| 새 파인튜닝 FP32 | 5.01% | 10.10% | 92.45% |
| 새 파인튜닝 INT8 | 4.28% | 10.17% | 92.16% |

이 표는 **과거 전체 평가 결과를 재사용한 것**이며, 인계 과정에서 다시 2,398개를
평가한 결과가 아닙니다. 원수치와 오류 개수는 [comparison.csv](comparison.csv)를 참조하세요.

- CER: 소문자화, 공백·문장부호 제거 후 문자 단위 편집 거리.
- WER: 앞뒤 공백 제거 후 공백으로 나눈 어절 단위. CER와 달리 문장부호를 별도로 제거하지 않습니다.
- 두 지표 모두 전체 대치+삭제+삽입 합을 전체 정답 길이로 나눕니다. 문장별 평균이 아닙니다.
- 키워드: `matched_keyword`가 있으면 해당 문구, 없으면 전체 정답 문구가
  정규화된 출력에 포함되는지 봅니다. 응급상황 분류 정확도나 정답 확률이 아닙니다.
- 기존 guard의 최종 반복 의심 검출은 baseline FP32 16건, INT8 8건,
  finetuned 두 모델 0건이었습니다. **미검출된 두 번 반복 등이 남았으므로 완전 해결이 아닙니다.**
- 직접 녹음 28개에서는 새 두 모델 모두 WER 20.00%, 키워드 78.57%였습니다.
- 이 평가 세트는 개발 판단에 사용되었습니다. 최종 일반화 성능 확인에는 별도 음성이 필요합니다.
- 학습/평가 ID·경로 중복은 검사했지만, 이 실험에서 전체 내용 해시 중복 및
  원제작자 Zeroth 학습 이력을 완전히 검증한 것은 아닙니다.

## 3. CT2와 ONNX를 구분해서 사용

| 산출물 | 실행 방식 | 용도 | 위 비교표 적용 |
|---|---|---|---|
| CT2 4개 디렉터리 | 이 패키지의 CT2 코드 | baseline 측정 후 모델 교체 | 해당 방식의 과거 측정값 |
| ONNX baseline FP32 | ONNX Runtime | 그래프 통합·계산 확인 | 적용 불가 |
| ONNX finetuned FP32 | ONNX Runtime | 그래프 통합·계산 확인 | 적용 불가 |

ONNX는 HF 원본/병합 가중치에서 별도 변환한 **FP32** 파일입니다.
CT2 INT8의 이름만 바꾸거나 ONNX INT8이라고 표시하지 않았습니다.
ONNX 변환 자체는 경량화가 아니며, 그래프/가중치 저장 방식 때문에 CT2보다 클 수 있습니다.

Whisper는 다음 두 그래프가 **모두** 필요합니다.

| 파일 | 입력 | 출력 |
|---|---|---|
| `encoder.onnx` | float32 `[1, 80, 3000]` Log-Mel | float32 `[1, 1500, 768]` |
| `decoder.onnx` | int64 `[1, T]` 토큰 prefix + encoder 출력, 1≤T≤448 | float32 `[1, 51865]` 다음 토큰 logits |

토크나이저, 특징 추출, 토큰 생성 반복문도 필요합니다. WAV를 ONNX 입력에 바로 넣거나
logits의 최댓값을 응급 점수로 쓰면 안 됩니다. 두 그래프는 batch=1, FP32, cache 없는 형태입니다.

`selection_regenerate`는 Python 실행 정책이며 ONNX 가중치 안에 들어 있지 않습니다.
`onnx_demo.py`는 greedy/no-timestamp로 그래프가 실제 실행되는지 보여주는 **별도 예제**입니다.
CT2 beam/fallback/guard를 이식한 것이 아니므로 출력과 성능이 달라집니다.

## 4. 다운로드와 baseline 실행

브랜치: `agent/m4-whisper-handoff-20260916`

[가중치 다운로드: GitHub 사전 릴리스](https://github.com/Ldg48/rp5/releases/tag/m4-whisper-handoff-20260916)

가중치는 큰 파일이라 Git 기록에 직접 넣지 않고 사전 릴리스에 첨부합니다.
코드와 SHA-256 목록은 브랜치에 있습니다. 다운로더는 해시를 확인하고 기존 폴더를 덮어쓰지 않습니다.

아래 명령은 저장소 루트에서 실행합니다. Python 3.10 환경에서 검증했습니다.

```powershell
git clone --branch agent/m4-whisper-handoff-20260916 https://github.com/Ldg48/rp5.git rp5-m4
cd rp5-m4
python -m venv .venv
.\.venv\Scripts\python -m pip install -r m4_whisper/requirements.txt
.\.venv\Scripts\python -m m4_whisper.download --artifact ct2_baseline_fp32
.\.venv\Scripts\python -m m4_whisper --variant baseline_fp32 --audio C:/audio/test.wav
```

baseline 전체 측정이 끝난 뒤에만 모델을 바꿉니다. **같은 오디오와 설정을 유지**하세요.

```powershell
.\.venv\Scripts\python -m m4_whisper.download --artifact ct2_finetuned_fp32
.\.venv\Scripts\python -m m4_whisper --variant finetuned_fp32 --audio C:/audio/test.wav
.\.venv\Scripts\python -m m4_whisper.download --artifact ct2_baseline_int8
.\.venv\Scripts\python -m m4_whisper.download --artifact ct2_finetuned_int8
.\.venv\Scripts\python -m m4_whisper --variant finetuned_int8 --audio C:/audio/test.wav
```

Linux에서는 `.venv/bin/python`을 사용합니다. 모델 인계본은 토크나이저와 전처리 설정을
포함하므로 모델 다운로드가 완료된 이후에는 HF 로그인이나 인터넷이 필요하지 않습니다.
라즈베리파이 ARM에서 패키지 설치·메모리·지연·발열 검증은 별도로 해야 합니다.

Windows에서 `DLL load failed ... 애플리케이션 제어 정책` 오류가 나면 모델 오류와 구분해야 합니다.
이 PC에서도 완전히 새 venv는 CT2 DLL이 차단됐지만 기존 승인된 Python 설치에서는 실행됐습니다.
보안 기능을 끄거나 차단을 임의 우회하지 말고 관리자에게 해당 실행 환경 승인을 요청하세요.
이 경우 기존 검증 환경의 `python -m m4_whisper ...`로는 실행을 확인했습니다.

ONNX 파일만 받을 때:

```powershell
.\.venv\Scripts\python -m pip install -r m4_whisper/requirements-onnx.txt
.\.venv\Scripts\python -m m4_whisper.download --artifact onnx_baseline_fp32
.\.venv\Scripts\python -m m4_whisper.download --artifact onnx_finetuned_fp32
.\.venv\Scripts\python -m m4_whisper.onnx_demo --model-dir m4_artifacts/onnx/finetuned_fp32 --audio C:/audio/test.wav
```

## 5. 팀 코드에서 사용할 입력과 출력

마이크는 **공용 캡처 쪽에서 한 번만** 엽니다. M4는 마이크를 열지 않고 잘라낸 파형을 받습니다.
AST와 공유하는 것은 원본 파형이지 80-bin Log-Mel이 아닙니다.

```python
import ctranslate2
from m4_whisper.runtime import SpeechRecognizer

ctranslate2.set_random_seed(42)  # 프로세스 시작에 한 번만
speech = SpeechRecognizer("m4_artifacts/ct2/baseline_fp32", "baseline_fp32")

# 실제 공용 버퍼에서 얻은 스냅샷을 전달. 모델은 루프 밖에서 한 번만 로드.
# audio_window: numpy.ndarray, dtype=float32, shape=(N,), 16000 Hz, mono
result = speech.transcribe(audio_window, 16000, window_id="window-001", start_seconds=25.0)
```

입력 계약:

- `audio_window`: 유한한 값, 범위 [-1, 1], float32, 1차원. 기본 운용 권장 길이 5초.
- 허용 길이: 1~480,000 sample, 최대 30초. 잘못된 샘플레이트·스테레오·int16·NaN은 오류입니다.
- int16 PCM을 직접 받았다면 캡처 계층에서 `pcm.astype(np.float32) / 32768.0`으로 변환합니다.
- WAV CLI는 디코딩만 합니다. 스테레오 결합·리샘플·잡음 제거·볼륨 조절을 몰래 하지 않습니다.
- 호출 시 작은 스냅샷 복사로 버퍼를 보호합니다. 캡처 콜백 안에서 추론하지 마세요.
- `start_seconds`는 팀이 정한 공통 시간축의 시작 시각입니다. 자동으로 현재 시간을 넣지 않습니다.

출력 예시(필드 일부, 설명용):

```json
{
  "schema_version": 1,
  "component": "M4_speech",
  "status": "ok",
  "window_id": "window-001",
  "window_start_seconds": 25.0,
  "window_end_seconds": 30.0,
  "model": "finetuned_int8",
  "text": "넘어졌어요",
  "segments": [{"text": "넘어졌어요", "start_seconds": 25.0, "end_seconds": 27.0}],
  "guard": {"mode": "selection_regenerate", "regenerations": 0, "final_repetition_suspected": false}
}
```

- 문장과 실제 생성 토큰을 보존합니다. 문자열 중복 삭제를 하지 않습니다.
- `status=ok`: 출력이 있다는 뜻일 뿐 정답·응급 여부가 확인됐다는 뜻은 아닙니다.
- `status=no_text`: 모델이 문장을 내지 않았다는 뜻입니다. 실제 무음이라고 확정하지 않습니다.
- 실행 오류: Python API는 예외, CLI는 `status=error` JSON + 종료 코드 1. 빈 성공 결과로 숨기지 않습니다.
- `avg_logprob`, `no_speech_prob`는 진단값이며 정답 확률/응급 확률로 쓰지 않습니다.
- segment 시각은 모델의 추정치입니다. 실제 window 밖이면 `timestamp_outside_window=true`로 표시하며
  임의 보정하지 않습니다. 환경음 결합에는 먼저 실제 `window_start/end_seconds`를 사용하세요.
- guard의 미해결 candidate 수와 최종 출력 반복 의심은 다릅니다. 비음성 skip된 후보도 있을 수 있습니다.
- `temperature_metadata`는 wrapper 반환 메타데이터입니다. 모든 시도의 실제 temperature 목록은 아닙니다.
- 겹치는 버퍼 두 개에서 같은 말이 나오는 것은 생성 루프와 별개입니다. 팀 융합 계층에서 window ID와
  시간으로 이벤트를 관리해야 하며, 이 모듈이 실제 반복 발화를 지우지는 않습니다.
- 하나의 인스턴스는 순차 처리합니다. 여러 모델을 동시에 올리면 가중치 메모리도 각각 필요합니다.

## 6. 반복 방지 설정

| 항목 | 고정값 |
|---|---|
| 반복 방지 | selection_regenerate |
| 첫 생성 n-gram | 0 |
| 재생성 n-gram | 3 |
| 재생성 temperature / beam | 0.0 / 5 |
| repetition_penalty | 1.0 |
| 추가 재생성 | 의심 구간당 최대 1회 |
| fallback temperature | 0, 0.2, 0.4, 0.6, 0.8, 1.0 |
| VAD / without_timestamps | false / false |
| max_new_tokens | None |

나머지 값은 [settings.py](settings.py)와 [fixed_settings.json](fixed_settings.json)에 있습니다.
`--repetition-policy off`로 비활성화할 수 있지만, 그러면 위 표와 조건이 달라집니다.
이 패키지의 기본값은 요청된 인계 조건이며 기존 운영 스크립트 기본값을 바꾼 것이 아닙니다.

`n=3`은 세 글자가 아니라 **세 토큰 묶음**의 재등장 제한입니다.
guard의 `generation_cap_tokens=224`는 후보 구제 적격성 검사에 쓰이는 길이 기준입니다.
CT2의 전체 context 한도 448과 다르며, 이 값을 생성 종료 한도라고 설명하면 안 됩니다.

## 7. 유의사항 및 인수 순서

1. baseline 모델로 팀 전체 시스템을 먼저 측정하고 입력 파일·설정·기기·버전을 저장합니다.
2. 같은 정밀도끼리 baseline ↔ finetuned를 비교한 뒤 INT8 선택을 판단합니다.
3. 정상 발화, 실제 반복 발화, 작은 목소리, 잡음, 무음을 별도로 확인합니다.
4. 라즈베리파이에서 실시간 처리 가능 여부를 확인하고 팀장이 모델 교체를 결정합니다.
5. ONNX가 최종 실행 규격이면 beam/fallback/guard 구현과 별도 성능 측정이 추가로 필요합니다.

현재 guard는 휴리스틱입니다. 실제로 여러 번 말한 문장을 오탐하거나 짧은 반복을 놓칠 수 있습니다.
무음에서 문장을 생성하거나 뭉개진 발음을 틀리게 인식할 수도 있습니다.
이 결과만으로 낙상 확정이나 자동 신고를 실행하지 말고 다른 센서/상태와 결합하세요.

원래 표의 평균 처리 시간은 이 PC에서 네 모델을 동시에 실행한 값입니다.
라즈베리파이 지연이나 ONNX 속도로 인용하지 마세요. CT2 INT8이 모든 음성에서 FP32보다 정확하다는 뜻도 아닙니다.

라이선스와 데이터 권리 주의는 [MODEL_NOTICE.md](MODEL_NOTICE.md),
실제 인계 검증은 [VERIFICATION.md](VERIFICATION.md)를 확인하세요.

원본 음성·개인 녹음·학습용 데이터셋·토큰·계정 비밀은 업로드하지 않습니다.
이 브랜치는 검토/인수용이며 main 병합이나 운영 배포를 자동 수행하지 않습니다.
