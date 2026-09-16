# M4 Whisper 인계 후보 · 이대경

팀장이 baseline 전체 측정 후 모델을 교체할 수 있도록 분리한 인계본입니다.
main 병합이나 운영 서비스 교체는 하지 않았습니다.

[코드와 사용 설명](https://github.com/Ldg48/rp5/tree/agent/m4-whisper-handoff-20260916/m4_whisper)

## 첨부파일

- `m4-ct2-baseline_fp32.zip`, `m4-ct2-baseline_int8.zip`: Zeroth 기준 모델.
- `m4-ct2-finetuned_fp32.zip`, `m4-ct2-finetuned_int8.zip`: 새 700-step LoRA 학습·병합 모델.
- `m4-onnx-baseline_fp32.zip`, `m4-onnx-finetuned_fp32.zip`: 별도 FP32 ONNX encoder/decoder 및 토크나이저.

CT2 실행은 `selection_regenerate`, 재생성 n=3을 사용합니다.
ONNX 그래프에는 이 Python 정책이 들어 있지 않으며, ONNX 단순 실행 예제는 비교표와 다른 디코딩입니다.

## 과거 CT2 평가 2,398개

| 모델 | CER | WER | 키워드 |
|---|---:|---:|---:|
| baseline FP32 | 46.43% | 97.48% | 69.93% |
| baseline INT8 | 37.24% | 86.66% | 70.14% |
| finetuned FP32 | 5.01% | 10.10% | 92.45% |
| finetuned INT8 | 4.28% | 10.17% | 92.16% |

이 수치를 ONNX나 새로운 기기에서 재측정한 것으로 해석하면 안 됩니다.
실제 반복 발화 보존, 미검출 짧은 반복, 무음 환각과 라즈베리파이 지연은 여전히 별도 확인이 필요합니다.

## 인수 방법

브랜치 문서의 다운로드 명령은 SHA-256을 검증하고 기존 모델을 덮어쓰지 않습니다.
모델을 모두 받을 필요 없이 사용할 형식만 선택하면 됩니다.
입력은 16 kHz mono float32 파형, 출력은 문장·구간 시각·guard 진단을 포함한 JSON입니다.
음성·개인 녹음·데이터셋·인증 토큰은 첨부하지 않았습니다.

주의: 연구용 통합 후보입니다. 음성 인식만으로 응급 여부나 자동 신고를 결정하지 마세요.
