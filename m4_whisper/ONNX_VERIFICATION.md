# ONNX INT8 검증 기록

2026-09-16, 담당 이대경의 새 파인튜닝 모델 인계. **CT2 결과 재현 검증이 아닌 별도 ONNX 실행 검증**입니다.

## 수행한 작업

1. HF 원본 SHA-256 `cebed9f4c930309e28a79537f6dcb0454fb22564b21fa110173e00e8ab6fae77` 확인.
2. FP32 Optimum encoder/decoder/decoder-with-past 내보내기. 토크나이저 전체 vocabulary/ID 동일성 확인.
3. ONNX Runtime 동적 QInt8, per-channel, constant MatMul만 양자화. 3개 그래프 모두 ONNX checker 통과.
4. 그래프별 실제 MatMulInteger 노드: encoder 72, decoder 121, cached decoder 97.
5. 원본 가중치 변경 없이 별도 파일 생성. CPUExecutionProvider로 로컬 모델만 로드.

## 변환의 수치 확인

Optimum 기본 atol=1e-5 검증은 경고를 냈습니다. 이를 통과했다고 숨기지 않습니다.
실제 입력을 이용한 별도 FP32 비교는 encoder atol=0.003/rtol=0.005,
decoder atol=0.001/rtol=0.005를 사용했습니다. 이 허용오차는 INT8 정확도 보장이 아닙니다.

| 점검 | 결과 |
|---|---|
| FP32 encoder 최대 절대 차이 | 0.00195503 |
| decoder prefix 4 최대 절대 차이 | 0.00002289 |
| decoder prefix 8 최대 절대 차이 | 0.00003767 |
| 두 prefix의 마지막 argmax | 모두 일치 |
| 캐시 decoder와 전체 prefix decoder | 명시한 허용오차 내 일치 |

## 실제 입력 확인

최종 관련 테스트: `python -m pytest tests -q` **38 passed, 5 warnings**.
경고 5개는 보존한 이전 FP32 exporter 테스트의 TorchScript/trace 경고입니다.
ONNX adapter 후보·sampling 점수·호출 횟수·무음 처리 전용 테스트는 11개입니다.

고정 규칙: 기존 2,398개 평가 manifest의 첫 2행과 첫 AI Hub 행. 출력 확인 후 샘플을 고르지 않았습니다.
정답은 기존 manifest 출처이며 이번에 청취해서 새로 확정하지 않았습니다. 원본 오디오는 업로드하지 않습니다.

| 입력 | 정답 | ONNX INT8 결과 |
|---|---|---|
| new_eval_0007 | 넘어졌어요 | 나마졌어요 |
| new_eval_0008 | 넘어졌어요 | 넘어졌어요 |
| 10.낙상_1086758_label | 헛딛어서 넘어졌어요 | 헛딛어서 넘어졌어요 |
| 합성 5초 디지털 무음 | 발화 없음 | MBC 뉴스 이덕영입니다 |

고유 실제 녹음 3개, 합성 무음 1개, 첫 실제 녹음 재실행 1회로 총 runtime 호출 5회입니다.
같은 seed로 첫 파일을 재실행한 text/token IDs가 동일했습니다. 입력 파형은 변경하지 않았습니다.
단위 테스트를 통과한 것과 실제 인식 성능이 개선된 것은 다릅니다.

**무음 환각은 미해결**입니다. 무음 점수는 약 0.8342였지만 평균 로그확률 약 -0.6831이어서
기존 두 점수 조건의 skip 기준에 해당하지 않았습니다. 반복 없는 환각이므로 반복 guard가 제거하지 않습니다.
점수를 정답 확률처럼 사용하거나 이 결과를 임의로 빈 출력으로 바꾸지 않았습니다.

새 ONNX adapter의 반복 구제/재생성은 합성 후보 단위 테스트로 확인했습니다.
이번 실제 3개 음성에서 재생성이 필요하지 않았으므로 **실제 음성의 재생성 효과·실제 반복 발화 보존은 미검증**입니다.
동일한 가중치라도 CT2와 timestamp, beam/sampling 구현, 점수 정규화, 후보 노출 방식이 다릅니다.

## 실행 명령

검증 작업 폴더: `C:\safewave-m4-handoff-20260916`.

```powershell
.\.venv-onnx\Scripts\python.exe -m m4_whisper.export_onnx_int8 --source C:\rp5\diagnostics\whisper_fresh_four_model_guard_20260915\models\fresh_finetuned_hf_merged --output m4_artifacts\onnx_standard
.\.venv-onnx\Scripts\python.exe -m m4_whisper.verify_onnx_int8 --model-dir m4_artifacts\onnx_standard\int8 --fp32-dir m4_artifacts\onnx_standard\fp32 --hf-source C:\rp5\diagnostics\whisper_fresh_four_model_guard_20260915\models\fresh_finetuned_hf_merged --manifest C:\rp5\diagnostics\whisper_fresh_four_model_guard_20260915\data\evaluation_manifest.csv --output m4_whisper\onnx_verification.json
.\.venv-onnx\Scripts\python.exe -m pytest tests -q
.\.venv-onnx\Scripts\python.exe -m m4_whisper.build_onnx_release --model-dir m4_artifacts\onnx_standard\int8 --output-dir m4_artifacts\releases
```

내보내기와 압축은 기존 산출물을 덮어쓰지 않습니다. 재현 시 새 출력 폴더를 지정하세요.
상세 버전·해시·원본 출력·후보 로그는 [onnx_verification.json](onnx_verification.json)에 있습니다.
원래 CT2 검증과 이전 데모 기록은 `verification.json`이며 이름을 구분하세요.

## 남겨둔 확인 사항

- 전체 ONNX CER/WER/키워드 평가: 미실행.
- 실제 반복 발화, 소음, 신음, 무음 부작용 확대 검증: 미실행.
- Raspberry Pi ARM64 설치·메모리·실시간 속도: 미실행.
- 기존 Docker/응급 지수 인터페이스 통합: 미실행, 기본 서비스 유지.
- CPU-only 환경 전체 신규 설치: 미실행. 실제 실행 환경은 기존 torch의 CPU 연산을 사용하는 별도 버전 가상환경.

인계본은 검증용 브랜치와 prerelease로 제공하며, 기본 서비스에 바로 적용하는 배포가 아닙니다.

## 배포 파일 전달 확인

- 브랜치 코드 커밋: `fc45e2d10a9307863e5299dfb0222208a496191b`.
- 릴리즈: `m4-onnx-int8-20260916`, prerelease=true, latest 지정 안 함.
- `m4-onnx-finetuned-int8.zip`: 510,379,887 bytes.
- SHA-256: `fd75e7da47c290abc12fe50b602b14025c9cbb1ead4e3b732922ee49a291a694`.
- GitHub가 반환한 asset digest와 로컬 압축 해시 일치, 비인증 공개 HEAD HTTP 200 및 파일 크기 일치.
- 압축 CRC 확인, 검증된 로컬 압축의 안전한 설치 및 ONNX 실행 확인.
- 설치한 압축 모델에서 `new_eval_0008`을 실행한 text/token IDs가 기존 검증 실행과 일치.
- 다운로드 전체 바이트의 재수신은 하지 않았습니다. 원격 digest/HEAD와 로컬 압축 설치를 각각 확인했습니다.
- 원본 `C:\rp5`의 모델과 팀 기본 서비스는 교체하지 않았습니다.
