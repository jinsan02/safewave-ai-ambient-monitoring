# M5(SLM) 정리 — 노트북 실측 (2026-09-23, 노진산 본인 작업용)

모델 `qwen_15b_gguf_q5`(Qwen2.5-1.5B Q5_K_M, llama.cpp), 프롬프트·가드레일 `logic/qwen_15b.py`. 코드 develop `5d85860`.

## 1. 정확도 — `eval_qwen_accuracy.py` 100케이스 (정답 = compute_emergency_score 등급)
| | CPU(8스레드) | GPU |
|---|---|---|
| exact / adjacent | 51% / 82% | 53% / 82% |
| safe fail(critical→normal) | 0 | 0 |
| critical recall | 100% | 100% |
| normal FP | 53.7% | 53.7% |
| 원문 JSON 유효 | 100/100 | 100/100 |
| 틀린 방향 | **전부 과대**(normal→warning 18, normal→critical 18, warning→critical 13) | 동일 경향 |

- 운영에서 M5가 호출되는 구간(게이트 ≥ 0.6) 33건만: exact 20/33(61%), 틀린 13건은 모두 warning→critical.
- 그 13건(D-04 HR160, D-11 HR28+RR5, E-04, F-01~F-05, F-08, I-08, J-01, J-06, J-08)은 **임상적으로 critical이 맞아 보이는 것이 많음** → 정답 재판정 필요.
- 카테고리별 약점: three_domain 0/8, two_domain 1/12, alarm_sound 1/8.

## 2. 실제 데이터 직접 추론 (`slm_probe.py`, 5분마다, 빈 방 실제 CSI ± "도와주세요")
20회(22:18~23:53), 모두 `slm_mode=qwen`, 추론 p50 약 1.8 s(CPU 8스레드). 아래 표는 첫 19회 기준.

| 관찰 | 예 |
|---|---|
| M1 확정 시 critical "낙상감지" | 8/8 (빈 방 = M1 오경보를 그대로 따름) |
| **점수가 M1 확률을 거의 그대로 복사** | 낙상위험 64% → 0.64, 82% → 0.82 |
| **긴급 키워드 반영이 일관되지 않음** | 키워드만: critical 0.9(2회) / normal 0.5(1회). **낙상위험 51~59% + "도와주세요" → normal**, 64~82% + 키워드 → warning 유지(가중 없음) |
| reason 자리 오염 | 소견이 없으면 reason에 "미측정"을 그대로 씀(상태 문장의 표기를 복사) |

→ 가장 위험한 것은 **"도와주세요"가 있는데 normal**인 경우. 게이트 쪽 키워드 규칙(0.85)과 M5 판단이 어긋남. 프롬프트 few-shot에 "낙상위험 + 긴급키워드 → critical" 예가 있는데도 낮은 확률에서는 따르지 않음.

## 3. 속도
| 환경 | p50 | p90/p95 | 최대 |
|---|---|---|---|
| RPi5(9/17) | 9 s 단독 | 동시 23 s | — |
| 노트북 CPU 8스레드(스택 동시) | 1.6 s | p90 11.5 s | 50.7 s(첫 호출, 캐시 없음) |
| 노트북 GPU | 0.75 s | p95 1.05 s | 9.0 s(첫 호출) |

- prefill(프롬프트 읽기)은 `n_threads_batch` 미지정 → llama-cpp-python 기본값 = **전 코어**. 노트북에서 ai-qwen 최대 1,517%, RPi5에서도 M5 실행 중 4코어 점유 추정.

## 4. 오늘 고친 것 (develop 반영)
| 커밋 | 내용 |
|---|---|
| `ac75c46` | M2 off → 상태 문장 `심박:0` → 모델이 심정지로 판단(critical "심박이상(hr=0)") → "미측정"으로 표기 |
| `5d85860` | JSON 해석 실패 시 예비 추출이 `hr=118`·`119`의 "1"을 점수 1.0으로 읽던 문제 |
| `5d85860` | 모델·토크나이저 로드 실패 시 조용히 fallback → 시작 시 ERROR `qwen_unavailable_fallback_only`, `qwen_invoked`에 `slm_mode` |
| (설정) | GPU M5: transformers가 torch(cu128)를 불러와 llama.cpp NCCL과 충돌 → `USE_TORCH=0` |

## 5. 파이프라인 상호작용
- 규칙 경보 뒤 90초 동안 전 노드 M5 요청 억제 + 빈 방 M1 오경보 약 100초 간격(113분 65건) → **파이프라인에서 M5 호출 0건**(22:02~23:54). M1 문제가 풀리기 전에는 M5가 운영에서 거의 돌지 않음.
- M2 스텁 on → 전 노드 critical 연속 → Phase 2 락으로 M3 생략·TTS 적체.

## 6. 할 일 (우선순위)
1. **키워드 일관성**: 긴급 키워드가 있으면 최소 warning(또는 critical) 보장하는 후처리 가드 — vital_override와 같은 방식. 안전 쪽이라 우선. (프롬프트 변경은 평가 재실행 필요)
2. reason에 "미측정" 쓰지 않게: 상태 문장 표기 변경 또는 후처리.
3. 평가 13건 정답 재판정 → 정답 세트 v2.
4. `n_threads_batch = QWEN_GGUF_THREADS` 선택 설정 → RPi5에서 지연·M1 영향 비교 후 결정.
5. 규칙 경보 뒤 M5 "설명만" 허용 여부(API 알림 흐름 변경, 설계 결정).
6. GPU 이미지(`rp5-ai-qwen-gpu`) 오늘 수정분 리빌드.

원자료: `reports/laptop/m5eval_{cpu,gpu}_results.json`, `reports/laptop/slm_probe.jsonl`(Git 제외).
