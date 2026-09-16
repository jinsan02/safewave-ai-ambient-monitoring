# M4 새 파인튜닝 ONNX INT8 인계

담당: **이대경**. 팀 통합용 브랜치: `m4/lee-daegyeong-whisper-int8`.

**이번에 받을 것은 `m4-onnx-finetuned-int8.zip` 하나입니다.**
Zeroth 기반 새 파인튜닝 모델을 ONNX INT8로 변환했습니다. CT2 파일의 확장자만 바꾼 것이 아닙니다.
팀의 `ai/main.py`, 기존 M4, Docker 기본 설정은 바꾸지 않았습니다.

> 검증용 인계본입니다. 실제 음성 오인식과 무음 환각이 남아 있습니다.
> 기존 CT2 CER 4.28%, WER 10.17%, 키워드 인식률 92.16%는 **이 ONNX 버전의 성능 수치가 아닙니다.**
> 최종 배포 승인이나 Raspberry Pi 속도 검증을 뜻하지 않습니다.

## 1. 베이스에서 현재까지

```text
OpenAI Whisper Small
  -> seastar105/whisper-small-ko-zeroth (한국어 학습 baseline)
  -> AI Hub 낙상/도움요청 + 직접 녹음으로 새 LoRA 학습
  -> LoRA 병합 HF 가중치 (2026-09-15, step 700)
  -> 표준 ONNX FP32 내보내기
  -> ONNX Runtime 동적 INT8 양자화 (이번 전달본)
```

- 기존 8:2 모델에 이어서 학습한 모델, B 200-step, 후속 6모델 실험이 아닙니다.
- Train 5,595행: AI Hub 5,000 + 직접 녹음 119개를 5회 사용한 595행. 고유 음성 5,119개.
- Validation 1,000개. 기존 CT2 평가 2,398개: AI Hub 2,370 + 직접 녹음 28.
- LoRA r=16, alpha=32, dropout=0.05, q_proj/v_proj, 1 epoch, 700 step,
  batch 1, accumulation 8, LR 1e-4, warmup 50, seed 42.
- 이번 ONNX 인계에서 새 학습은 하지 않았습니다. 원본 가중치 해시는 `onnx_manifest.json`에 기록했습니다.
- 구조는 Whisper Small 그대로입니다. 상수 MatMul 가중치를 채널별 INT8로 양자화했습니다.
  모든 연산이 INT8인 것은 아니며 Conv, LayerNorm 등과 입출력은 FP32입니다.

## 2. 받기와 실행

저장소 루트에서 **별도 Python 3.10 환경**을 사용하세요. 기존 `ai/requirements.txt`와 버전이 다릅니다.

```powershell
git switch m4/lee-daegyeong-whisper-int8
py -3.10 -m venv .venv-onnx
.\.venv-onnx\Scripts\python.exe -m pip install torch==2.13.0 --index-url https://download.pytorch.org/whl/cpu
.\.venv-onnx\Scripts\python.exe -m pip install -r m4_whisper/requirements-onnx.txt
.\.venv-onnx\Scripts\python.exe -m m4_whisper.download --artifact onnx_finetuned_int8
.\.venv-onnx\Scripts\python.exe -m m4_whisper.onnx_runtime --model-dir m4_artifacts/onnx/finetuned_int8 --audio sample_16k_mono.wav --output result.json
```

Linux에서는 `python3.10 -m venv .venv-onnx`, 이후 `.venv-onnx/bin/python`을 사용합니다.
CPU 패키지 배포 여부는 대상 OS/아키텍처에 따라 다릅니다. ARM64/Raspberry Pi 설치와 속도는 이번에 검증하지 않았습니다.
실제 검증 환경은 Python 3.10.11, torch 2.13.0+cu132의 **CPU 계산**, ORT 1.23.2입니다.
새 CPU-only 가상환경 전체 설치는 별도 검증 사항입니다. CUDA 실행은 사용하지 않습니다.

다운로더는 압축 SHA-256을 검사하고 기존 폴더를 덮어쓰지 않습니다.
모델을 받은 뒤 실행은 `local_files_only=True`로 오프라인입니다. Git에는 대용량 가중치를 넣지 않았습니다.

압축 속 ONNX 3개는 **서로 다른 학습 모델 3개가 아니라 한 모델의 구성 요소**입니다.

| 파일 | 역할 |
|---|---|
| `encoder_model.onnx` | 로그멜 특징을 음성 정보로 변환 |
| `decoder_model.onnx` | 첫 토큰 생성과 캐시 준비 |
| `decoder_with_past_model.onnx` | 캐시를 재사용하며 다음 토큰 생성 |

세 파일과 토크나이저·설정 파일을 같은 폴더에 두세요. 그래프 합계는 FP32 약 1,844 MB에서 INT8 약 716 MB로 줄었습니다.
캐시용 그래프 사이에 가중치가 중복되고 일부 연산은 FP32라, 기존 CT2 INT8 약 238 MB와 크기가 같지 않습니다.

## 3. 공용 오디오 입력과 출력

마이크를 새로 열지 않습니다. 환경음 모델과 공유하는 원시 파형을 넘깁니다.

```python
from m4_whisper.onnx_runtime import OnnxSpeechRecognizer

model = OnnxSpeechRecognizer('m4_artifacts/onnx/finetuned_int8', cpu_threads=6)
# waveform: numpy.float32, shape (N,), mono, 16000 Hz, [-1, 1]
result = model.transcribe(waveform, 16000, window_id='chunk-001', start_seconds=0.0)
print(result['transcript_ko'])
```

- 1~480,000 sample, 최대 30초. 권장 입력 창은 기존 공용 버퍼의 짧은 발화 단위입니다.
- int16 PCM bytes, 스테레오, 다른 샘플링레이트, NaN/Inf는 조용히 변환하지 않고 오류를 냅니다.
- 모델 내부에서 80채널 로그멜 `[1, 80, 3000]`로 변환합니다. AST의 특징을 그대로 넣으면 안 됩니다.
- 호출당 이전 창 전사를 전달하지 않습니다. 겹치는 창의 중복 이벤트 처리는 통합 계층에서 별도 설계해야 합니다.
- 모델은 프로세스당 한 번 로드하고 순차 사용합니다. 프로세스 여러 개면 모델 메모리도 늘어납니다.

주요 JSON 필드:

| 필드 | 의미 |
|---|---|
| `text`, `transcript_ko` | 원문 전사. 중복 문자열 삭제 없음 |
| `status` | `ok`, `no_text`, `no_speech`. `ok`는 실행 성공이지 정답 보장 아님 |
| `window_start_seconds`, `window_end_seconds` | 입력 버퍼 범위. 실제 단어별 timestamp 아님 |
| `confidence` | `null`. 점수를 정답 확률로 꾸미지 않음 |
| `avg_logprob`, `no_speech_prob` | 진단 점수. 확정적인 정답/발화 확률로 사용 금지 |
| `guard` | 후보 구제, 재생성 횟수, 미해결 반복 상태 |
| `attempts` | 실제 생성된 후보, 토큰, 점수, 선택 경로 |

예외는 정상 빈 출력으로 바꾸지 않습니다. 통합 코드에서 오류 상태를 따로 처리하세요.
원본 오디오는 저장하지 않지만 JSON 전사에도 개인정보가 포함될 수 있으므로 운영 로그 저장 정책이 필요합니다.

## 4. 반복 방지와 CT2와의 차이

기존 `guard.py`의 반복 판정과 선택 함수를 재사용합니다. **ONNX 전용 연결 코드는 새 구현**입니다.

- 기본 정책 `selection_regenerate`: 비반복 유효 후보 우선, 없을 때만 재생성 1회.
- 첫 생성 n=0, 재생성 n=3 / temperature 0 / beam 5 / repetition_penalty 1.0.
- fallback temperature: 0, 0.2, 0.4, 0.6, 0.8, 1.0. 압축률 2.4, 평균 로그확률 -1.0.
- 샘플링에서 이미 반환된 5개 후보도 보존합니다. 확률 비교 전 temperature 배율을 되돌립니다.
- 압축률을 통과해도 반복 의심이면 다음 temperature를 시도할 수 있습니다. 최대 6회 + 재생성 1회.
- `additional_calls`는 **추가 재생성 호출만**, `generation_calls`는 전체 생성 호출 수,
  `calls_after_first_stock_stop`는 기본 점수 기준의 최초 종료 가능 시점 이후 호출 수입니다.
- 비음성 skip은 `no_speech_prob > 0.6 AND avg_logprob < -1.0`. 반복을 없애려고 빈 출력으로 바꾸지 않습니다.
- 실패한 재생성은 성공으로 집계하지 않고 원래 후보와 미해결 상태를 보존합니다.
- `--repetition-policy off`로 비활성화할 수 있습니다. 실제 반복 발화가 오탐될 수 있습니다.

CT2와 다른 점: ONNX Runtime이 신경망 계산을 수행하고 Optimum/Transformers가 생성 루프를 수행합니다.
윈도 단위 **timestamp 없는** 한국어 전사이며, CT2 timestamp 청크 분할·샘플링·점수·beam 구현과 동일하지 않습니다.
448토큰 문맥 한도를 유지하며 짧은 출력 제한으로 반복을 숨기지 않습니다.
따라서 같은 가중치와 n=3이어도 CT2 출력 및 성능과 같다고 할 수 없습니다.

## 5. 검증 결과와 한계

검증 기록: [ONNX_VERIFICATION.md](ONNX_VERIFICATION.md), [onnx_verification.json](onnx_verification.json).

- 기존 평가 음성 3개를 manifest 순서로 고정 선택, 추가로 5초 디지털 무음과 첫 파일 동일 seed 재실행.
- 3개 중 2개 정답 문자열 일치. 1개는 `넘어졌어요 -> 나마졌어요`.
- 디지털 무음에서 `MBC 뉴스 이덕영입니다`를 생성했습니다. **무음 환각 미해결**입니다.
- 이 작은 샘플은 ONNX 전체 CER/WER, 응급 호출 정확도, 실제 반복 발화 보존을 입증하지 않습니다.
- 모델 구조 검사, FP32 변환 수치 비교, 캐시 디코더 일치와 동일 seed 재현을 확인했습니다.
- Raspberry Pi 메모리·지연, 전체 2,398개 평가, Docker 통합, 실제 반복 음성 보존은 아직 검증하지 않았습니다.

이 브랜치를 먼저 검토하고 팀이 기본 모델 전체 측정 후 교체 여부를 결정하세요.
기존 `ai/experts/m4_whisper_small.py`에 폴더만 지정하면 **이 반복 방지 wrapper를 거치지 않습니다**.
기존 `stt_confidence` 기반 응급 지수에 `confidence=null`을 그대로 넣어도 안 됩니다.
기본 서비스·의존성을 자동 변경하지 않았으므로 별도 인터페이스 연결과 점수 정책 합의가 필요합니다.

## 6. 기존 CT2 비교표 (참고용)

평가 2,398개, CT2 CPU, selection_regenerate/n=3. ONNX 측정값 아님.

| 모델 | CER | WER | 키워드 인식률 |
|---|---:|---:|---:|
| Zeroth baseline FP32 | 46.43% | 97.48% | 69.93% |
| Zeroth baseline INT8 | 37.24% | 86.66% | 70.14% |
| 새 파인튜닝 FP32 | 5.01% | 10.10% | 92.45% |
| 새 파인튜닝 INT8 | 4.28% | 10.17% | 92.16% |

이전 CT2 실행 코드는 비교 기록으로 보존했습니다. [CT2_REFERENCE.md](CT2_REFERENCE.md),
`VERIFICATION.md`, `verification.json`, `RELEASE_NOTES.md`, `delivery_receipt.json`은 이전 CT2/FP32 데모 인계 기록입니다.
현재 전달 파일은 ONNX INT8 하나만 받으면 됩니다.

## 참고

- [Optimum 다중 그래프 동적 양자화](https://huggingface.co/docs/optimum-onnx/en/onnxruntime/usage_guides/quantization)
- [ONNX Runtime 양자화](https://onnxruntime.ai/docs/performance/model-optimizations/quantization.html)
- 데이터 이용·동의 및 라이선스 주의: [MODEL_NOTICE.md](MODEL_NOTICE.md).
