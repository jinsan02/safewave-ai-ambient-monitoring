# 인계 검증 기록

2026-09-16, Windows x86-64, Python 3.10.11, CPU 실행.
추가 학습·전체 재평가·운영 모델 교체는 하지 않았습니다.

## 확인한 것

- 기존 fresh-four 실험의 네 `model.bin` SHA-256이 [settings.py](settings.py)의 값과 일치.
- 반복 guard는 기존 `tools/whisper_repetition_guard.py`와 바이트 단위로 동일.
  SHA-256: `7ae6b89a38bdcca70abf7762e08319f19fa2c0446b376e7ed29c938603f75658`.
- 실제 로컬 음성 2개 × 4모델 = 인계 API 8회 + 직접 CT2 대조 8회.
  seed=42를 각 독립 비교 실행 시작에 설정하고, 문장과 세그먼트 토큰이 **8/8 일치**.
  `HF_HUB_OFFLINE=1`에서 실행. 모델 로딩 시 HF 접근이 필요하지 않았음.
- 첫 음성에서 finetuned FP32/INT8 출력은 모두 `넘어졌어요`.
- 두 번째 음성에서 finetuned FP32/INT8 모두 제한된 재생성 1회가 실제 적용됐음.
  반복 의심 감소와 정답 복원은 별개이며, 인식 오류가 남은 출력을 그대로 보존함.
- 모델별 상세 출력, 입력 오디오 해시, guard 적용 수: [verification.json](verification.json).

## ONNX 확인

- baseline/finetuned 각각 실제 `encoder.onnx`, `decoder.onnx`를 생성.
- ONNX checker와 ONNX Runtime CPU 로딩 통과.
- 작은 로컬 테스트 모델의 decoder prefix 길이 1, 4, 9, 23에서 PyTorch와 ORT 계산 비교.
- 실제 두 모델에서는 동일한 실제 음성 특징으로 encoder 계산을 비교하고,
  같은 encoder 출력에 decoder prefix 4, 8, 16을 넣어 다음 토큰 logits를 비교.
- 검증 허용오차: 실제 모델 `atol=5e-4, rtol=5e-3`. 작은 테스트 모델은 `atol=1e-5, rtol=1e-4`.
- 실제 모델 decoder 관측 최대 절대 차이: baseline 약 3.18e-5, finetuned 약 4.34e-5.
  검사한 세 prefix 모두 다음 토큰 argmax 일치. 전체 생성 경로의 동등성 보장은 아님.
- encoder 최대 절대 차이는 baseline 약 0.00115, finetuned 약 0.00311.
  상대 허용오차를 포함한 검사 통과이며 비트 단위 동일하다는 뜻은 아님.
- ONNX 실제 음성 실행 예제도 실행했으나, CT2와 디코딩 정책이 다름.
  같은 음성에서 CT2 finetuned INT8은 `넘어졌어요`, ONNX finetuned 단순 greedy 예제는
  `나마졌어요`를 출력하고 EOS로 종료. **ONNX 동일 성능을 확인한 결과가 아님.**
- 각 zip의 `export_manifest.json`에 원본 HF 가중치 해시, 그래프 I/O, 패키지 버전과 검증값 포함.

## 테스트 범위와 한계

최종 로컬 테스트: `python -m pytest tests -q`에서 **30개 통과**, 경고 7개.
이 중 인계 관련 테스트는 26개이며 기존 tests 디렉터리의 4개도 통과했습니다.
아카이브 6개의 크기·SHA-256·허용 파일 목록을 검사했습니다. 총 다운로드 크기는 약 3.68 GB이며,
실제 사용하는 모델만 선택해서 받을 수 있습니다. 음성 파일은 포함하지 않았습니다.

별도의 깨끗한 venv에서도 `requirements.txt` 설치와 `pip check`는 통과했습니다.
다만 실제 실행은 Windows 애플리케이션 제어 정책이 새 폴더의 CT2 `_ext` DLL을 차단해 실패했습니다.
기존 환경과 새 환경의 `_ext.cp310-win_amd64.pyd` 해시는 모두
`e69164c1dc6a45b9d92b24092bddea96eecb5ef9a4e4af5ca739f6571ef0a167`로 같았습니다.
기존 환경에서의 성공을 새 환경에서의 성공으로 바꿔 보고하지 않습니다.
보안 정책은 변경하지 않았습니다.

독립 코드 검토에서 최종 반복 의심 표시가 text만 검사하던 누락을 확인했습니다.
실제 반환 토큰도 함께 검사하도록 수정하고 token-only 반복 회귀 테스트를 추가했습니다.
기존 guard와 모델 출력은 변경하지 않았습니다. 저장된 원본 토큰으로 표시값을 재계산했습니다.

입력 형식·오류 전달·타임스탬프·설정·재생성 최대 1회·실패 출력 보존·다운로드 해시·
압축 경로 안전성·ONNX 동적 prefix를 테스트합니다. 테스트 통과는 음성 정확도 개선과 다릅니다.

ONNX exporter의 legacy export 경고와 고정 encoder 길이에 관한 TracerWarning,
기존 SWIG deprecation 경고가 발생합니다. encoder 길이는 의도적으로 3000 frame으로 고정했으며,
decoder 길이 변화는 별도 테스트했습니다. 이 경고를 무시하고 범용 동적 그래프라고 표시하지 않습니다.

확인하지 않은 것: Raspberry Pi ARM 실제 실행·실시간 지연·발열·새로운 2,398개 재평가·
검수된 실제 반복 발화 전반의 보존·무음 환각 완전 해결·ONNX beam/fallback/guard 이식.

## 실제 실행 명령

작업 디렉터리: `C:\rp5-m4-handoff`. 원본 저장소는 `C:\rp5` 그대로 보존.
대규모 파일은 `m4_artifacts/`에만 생성하고 Git에서 제외.

```powershell
git worktree add -b agent/m4-whisper-handoff-20260916 C:/rp5-m4-handoff origin/main
python -m venv --system-site-packages .venv
.\.venv\Scripts\python -m pip install onnx==1.19.1

.\.venv\Scripts\python -m m4_whisper.export_onnx --source C:/rp5/diagnostics/whisper_fresh_four_model_guard_20260915/models/fresh_finetuned_hf_merged --output m4_artifacts/onnx/finetuned_fp32 --audio C:/Users/USER/Desktop/Fold/github/whisper_eval_7_3_20260729/audio/new_recordings/new_eval_0007.wav
.\.venv\Scripts\python -m m4_whisper.export_onnx --source C:/rp5/whisper/models/whisper-small-ko-zeroth --output m4_artifacts/onnx/baseline_fp32 --audio C:/Users/USER/Desktop/Fold/github/whisper_eval_7_3_20260729/audio/new_recordings/new_eval_0007.wav

python -m m4_whisper.build_artifacts --experiment-models C:/rp5/diagnostics/whisper_fresh_four_model_guard_20260915/models --tokenizer C:/Users/USER/.cache/huggingface/hub/models--openai--whisper-tiny/snapshots/169d4a4341b33bc18d8881c4b69c2e104e1cc0af/tokenizer.json --preprocessor C:/rp5/whisper/models/whisper-small-ko-zeroth/preprocessor_config.json

$env:HF_HUB_OFFLINE='1'
python -m m4_whisper.verify_handoff --audio C:/Users/USER/Desktop/Fold/github/whisper_eval_7_3_20260729/audio/new_recordings/new_eval_0007.wav --audio 'C:/Users/USER/Desktop/Fold/github/whisper_curated_processed/audio/train/fall_related/10.낙상_1252541_label.wav'
.\.venv\Scripts\python -m m4_whisper.onnx_demo --model-dir m4_artifacts/onnx/finetuned_fp32 --audio C:/Users/USER/Desktop/Fold/github/whisper_eval_7_3_20260729/audio/new_recordings/new_eval_0007.wav

.\.venv\Scripts\python -m pytest tests/test_m4_handoff.py tests/test_m4_artifacts.py tests/test_m4_onnx.py -q
git diff --check

# 독립 환경 설치 확인: 설치와 pip check 통과, 실행은 OS 정책 차단
python -m venv m4_artifacts/runtime-env
.\m4_artifacts\runtime-env\Scripts\python -m pip install -r m4_whisper/requirements.txt
.\m4_artifacts\runtime-env\Scripts\python -m pip check
.\m4_artifacts\runtime-env\Scripts\python -m m4_whisper --variant finetuned_int8 --audio C:/Users/USER/Desktop/Fold/github/whisper_eval_7_3_20260729/audio/new_recordings/new_eval_0007.wav
```

로컬 경로는 재현 기록이며, 해당 오디오는 업로드하지 않습니다.
팀원은 인계 받은 자체 오디오 경로로 교체해야 합니다.

일반 팀원은 export를 재실행할 필요 없이 릴리스 가중치와 runtime requirements만 설치합니다.
export 환경은 기존 패키지를 일괄 업그레이드하지 않기 위해 별도 venv에 추가 의존성만 설치했습니다.

## 업로드 검증

브랜치 `agent/m4-whisper-handoff-20260916`를 origin에 push했습니다. main은 변경하지 않았습니다.
사전 릴리스 `m4-whisper-handoff-20260916`의 모델 zip 6개 모두 GitHub에서 보고한 크기와
SHA-256이 로컬 catalog와 일치했습니다. 세부 증거는 [delivery_receipt.json](delivery_receipt.json)에 있습니다.

게시 후 공개 URL로 finetuned INT8을 다시 내려받아 압축 해시를 확인하고,
다운로드한 모델에서 오프라인 CT2 실행 결과 `넘어졌어요`를 확인했습니다.

```powershell
git push -u origin agent/m4-whisper-handoff-20260916
gh release create m4-whisper-handoff-20260916 --repo Ldg48/rp5 --target agent/m4-whisper-handoff-20260916 --title "M4 Whisper handoff - Lee Daegyeong (2026-09-16)" --notes-file m4_whisper/RELEASE_NOTES.md --prerelease --draft --latest=false
$assets = Get-ChildItem -LiteralPath m4_artifacts/release -Filter '*.zip' -File | ForEach-Object { $_.FullName }
gh release upload m4-whisper-handoff-20260916 --repo Ldg48/rp5 @assets
gh release edit m4-whisper-handoff-20260916 --repo Ldg48/rp5 --draft=false --prerelease --latest=false
python -m m4_whisper.download --artifact ct2_finetuned_int8 --root m4_artifacts/download_check
python -m m4_whisper --variant finetuned_int8 --model-root m4_artifacts/download_check/ct2 --audio C:/Users/USER/Desktop/Fold/github/whisper_eval_7_3_20260729/audio/new_recordings/new_eval_0007.wav
```
