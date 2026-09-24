#!/usr/bin/env python3
"""저장된 M5 평가 결과(qwen_raw)를 현재 후처리(vital_override·판정표 하한·정규화)에 다시 통과시켜 채점.

모델을 다시 돌리지 않고 후처리 변경만의 효과를 잰다(같은 모델 출력 → 결정적 비교).
qwen_raw는 평가 결과에 200자까지 저장된다 — JSON이 잘린 건은 건수만 보고한다.
  docker run --rm -v C:/rp5:/repo -w /repo -e PYTHONIOENCODING=utf-8 --entrypoint python3 rp5-ai-qwen \\
    scripts/dev/m5_replay_postprocess.py reports/laptop/m5v2final_gpu_gguf_laptop_ho.json
"""
import importlib.util
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "ai"))
from logic.qwen_15b import QwenLogic  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "ev", os.path.join(os.path.dirname(__file__), "..", "eval_qwen_accuracy.py"))
ev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ev)
ORDER = {"normal": 0, "warning": 1, "critical": 2}


def replay(path):
    data = json.load(open(path, encoding="utf-8"))
    meta = data.get("meta") or {}
    defs = ev._random_case_defs(meta["random"], meta["seed"]) if meta.get("random") else None
    cases = {c["id"]: c for c in ev.generate_dataset(defs)}
    q = QwenLogic.__new__(QwenLogic)
    q.session = q.tokenizer = True
    q.redis_client = None
    q.feedback_topic_key = "unused"
    q._last_prompt_tokens = q._last_output_tokens = None
    q._ensure_model_loaded = lambda: None
    q._fetch_hourly_context = lambda: {}
    q._fetch_time_series = lambda: None
    q._build_messages = lambda *_a, **_k: []
    op = raised = truncated = 0
    exact_before = exact_after = over = under = 0
    for r in data["results"]:
        if not r["m5_called"]:
            continue
        op += 1
        raw = r["qwen_raw"]
        truncated += int(not raw.rstrip().endswith("}"))
        q._evaluate_with_qwen = lambda _m, raw=raw: raw
        out = q.evaluate(cases[r["id"]]["expert_results"])
        gt = r["gt_level"]
        exact_before += int(r["pred_level"] == gt)
        exact_after += int(out["risk_level"] == gt)
        over += int(ORDER[out["risk_level"]] > ORDER[gt])
        under += int(ORDER[out["risk_level"]] < ORDER[gt])
        raised += int(bool(out.get("rubric_floor")))
    print(f"{os.path.basename(path)}: 운영 {op}건 exact {exact_before} → {exact_after} "
          f"(하한 적용 {raised}건, 과대 {over}, 과소 {under}, raw 잘림 {truncated})")


if __name__ == "__main__":
    for p in sys.argv[1:]:
        replay(p)
