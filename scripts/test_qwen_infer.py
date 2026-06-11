"""
Qwen 추론 테스트:
1. chat template 적용 여부 확인
2. _evaluate_with_qwen 직접 호출해서 JSON 생성 여부 확인
"""
import os, sys, traceback, logging
os.environ["ORT_USE_GPU"] = "0"
os.environ["REDIS_HOST"] = "localhost"  # Redis 없어도 동작하도록
logging.basicConfig(level=logging.WARNING)
sys.path.insert(0, "/app")

from logic.qwen_05b import QwenLogic

q = QwenLogic("/app/models/qwen_05b")
q._ensure_model_loaded()
print(f"session: {q.session is not None}")
print(f"with_past: {q.session_with_past is not None}")
print(f"tokenizer: {q.tokenizer is not None}")
print(f"max_new_tokens: {q.max_new_tokens}")
print(f"apply_chat_template: {hasattr(q.tokenizer, 'apply_chat_template') if q.tokenizer else 'N/A'}")

if q.session and q.tokenizer:
    expert = {
        "fall":      {"fall_score": 0.05, "fall_detected": False, "infer_confidence": 0.75},
        "vital":     {"heart_rate": 118.0, "breathing_rate": 24.0, "infer_confidence": 0.7},
        "env_sound": {"env_sound_label": "silence", "env_sound_confidence": 0.0, "infer_confidence": 0.0},
        "speech_ko": {"transcript_ko": "", "stt_confidence": 0.5, "speech_detected": True, "infer_confidence": 0.45},
    }
    prompt = q._build_analysis_prompt(expert, None, None)
    print(f"\nprompt len: {len(prompt)} chars")

    # chat template 적용 확인
    if hasattr(q.tokenizer, "apply_chat_template"):
        messages = [
            {"role": "system", "content": "당신은 안전 모니터링 AI입니다. 반드시 JSON만 출력하세요."},
            {"role": "user", "content": prompt},
        ]
        formatted = q.tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        print(f"formatted len: {len(formatted)} chars")
        toks = q.tokenizer(formatted, return_tensors="np", truncation=True, max_length=768)
        print(f"token count (truncated to 768): {toks['input_ids'].shape[1]}")
        print(f"last 80 chars of formatted: ...{formatted[-80:]}")

    print("\n=== _evaluate_with_qwen 호출 (실제 생성) ===")
    print("※ CPU full-seq 추론, 64토큰 생성 → 수분 소요 가능")
    try:
        result = q._evaluate_with_qwen(prompt)
        print(f"raw output: {repr(result)}")
        if result:
            parsed = q._parse_qwen_json_response(result)
            print(f"parsed: {parsed}")
        else:
            print("output is None — 추론 실패 또는 빈 응답")
    except Exception:
        traceback.print_exc()
