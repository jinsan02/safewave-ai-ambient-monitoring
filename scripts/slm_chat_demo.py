import os
import sys

sys.path.insert(0, os.path.join(os.getcwd(), "ai"))
from logic.qwen_05b import QwenLogic


def fallback_chat(user_text: str) -> str:
    text = (user_text or "").strip()
    if not text:
        return "입력이 비어 있어요. 문장을 입력해 주세요."
    if any(k in text for k in ["살려", "도와", "응급", "119", "불", "화재"]):
        return "긴급 키워드를 감지했어요. 즉시 주변 도움 요청 또는 119 신고를 권장합니다."
    if any(k in text for k in ["어지러", "아파", "가슴", "숨"]):
        return "건강 이상 신호 가능성이 있어요. 증상이 지속되면 즉시 의료 도움을 받으세요."
    return "현재 입력만으로는 긴급 징후가 낮아 보여요. 증상을 더 구체적으로 말해 주세요."


def main() -> None:
    model_path = os.path.join(os.getcwd(), "volumes", "models", "qwen_05b")
    qwen = QwenLogic(model_path)

    turns = [
        "안녕, 지금 상태 어때?",
        "가슴이 답답하고 숨이 차",
        "살려줘, 위험해",
    ]

    print("[SLM] demo chat start")
    for text in turns:
        print(f"YOU> {text}")
        resp = qwen._evaluate_with_qwen(text)
        if resp and str(resp).strip():
            print(f"BOT> {str(resp).strip()}")
        else:
            print(f"BOT> {fallback_chat(text)}")
    print("BOT> 종료합니다.")


if __name__ == "__main__":
    main()
