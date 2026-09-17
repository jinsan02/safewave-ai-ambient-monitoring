import importlib.util
import json
import sys
import types
import unittest
from collections import deque
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _RedisError(Exception):
    pass


redis_stub = types.ModuleType("redis")
redis_stub.Redis = object
redis_stub.exceptions = types.SimpleNamespace(
    RedisError=_RedisError,
    ConnectionError=_RedisError,
)
sys.modules.setdefault("redis", redis_stub)

utils_stub = types.ModuleType("utils")
utils_stub.stream_id_ts_ms = lambda value: int(
    (value.decode() if isinstance(value, bytes) else str(value)).split("-")[0]
)
utils_stub.json_loads = lambda raw: json.loads(
    raw.decode() if isinstance(raw, bytes) else raw
) if raw else {}
utils_stub.build_context_window = lambda *_args, **_kwargs: {}
utils_stub.safe_float = lambda value, default=0.0: float(value) if value is not None else default
sys.modules.setdefault("utils", utils_stub)

logic_pkg = types.ModuleType("logic")
logic_pkg.__path__ = [str(ROOT / "ai" / "logic")]
sys.modules.setdefault("logic", logic_pkg)
gguf_stub = types.ModuleType("logic.qwen_gguf")
gguf_stub.QwenLogic = object
sys.modules.setdefault("logic.qwen_gguf", gguf_stub)

qwen_service = _load_module("qwen_service_under_test", ROOT / "ai" / "qwen_service.py")

onnx_stub = types.ModuleType("onnxruntime")
sys.modules.setdefault("onnxruntime", onnx_stub)
qwen_15b = _load_module("qwen_15b_under_test", ROOT / "ai" / "logic" / "qwen_15b.py")
risk_policy = _load_module("risk_policy_under_test", ROOT / "ai" / "logic" / "risk_policy.py")
emergency_score = _load_module(
    "emergency_score_under_test", ROOT / "ai" / "logic" / "emergency_score.py"
)
runtime_inputs = _load_module("runtime_inputs_under_test", ROOT / "ai" / "runtime_inputs.py")
bench_rpi5 = _load_module("bench_rpi5_under_test", ROOT / "scripts" / "bench_rpi5.py")


class _FakeRedis:
    def __init__(self, active=False, state=None):
        self.state = state if state is not None else (b"active" if active else None)
        self.written = None

    def exists(self, _key):
        return int(self.state is not None)

    def get(self, _key):
        return self.state

    def xadd(self, stream, fields, **kwargs):
        self.written = (stream, fields, kwargs)


class M5SchedulerTests(unittest.TestCase):
    def test_selects_highest_risk_then_newest_and_drops_stale(self):
        now_ms = 100_000

        def message(stream_ms, score, needed=True, experts=True):
            payload = {
                "risk_score": score,
                "experts": {"fall": {}} if experts else {},
            }
            return (
                f"{stream_ms}-0".encode(),
                {
                    b"slm_needed": b"True" if needed else b"False",
                    b"data": json.dumps(payload).encode(),
                },
            )

        selected = qwen_service._select_candidate(
            [
                message(60_000, 0.99),  # 30초 제한 밖
                message(90_000, 0.70),
                message(91_000, 0.80),
                message(92_000, 0.80),  # 동점이면 최신
                message(93_000, 0.95, needed=False),
            ],
            now_ms,
        )

        self.assertIsNotNone(selected)
        self.assertEqual(selected[0], b"92000-0")

    def test_emergency_uses_fused_score(self):
        redis_client = _FakeRedis()
        qwen_service._write_emergency(
            redis_client,
            {"ts_ms": 1, "node_id": 2, "risk_score": 0.2, "risk_level": "normal"},
            {"risk_score": 0.9, "risk_level": "critical", "emergency": True},
        )

        payload = json.loads(redis_client.written[1]["data"])
        self.assertEqual(payload["risk_score"], 0.9)
        self.assertEqual(payload["risk_level"], "critical")
        self.assertTrue(payload["emergency"])

    def test_phase2_uses_existing_lock(self):
        self.assertTrue(qwen_service._phase2_locked(_FakeRedis(active=True), 1))
        self.assertTrue(qwen_service._phase2_locked(_FakeRedis(state=b"cooldown"), 1))
        self.assertFalse(qwen_service._phase2_locked(_FakeRedis(active=False), 1))

    def test_locked_highest_candidate_falls_back_to_next_node(self):
        class NodeLockRedis:
            def get(self, key):
                return b"active" if str(key).endswith(":1") else None

        def message(stream_ms, node_id, score):
            payload = {"node_id": node_id, "risk_score": score, "experts": {"fall": {}}}
            return (
                f"{stream_ms}-0".encode(),
                {b"slm_needed": b"True", b"data": json.dumps(payload).encode()},
            )

        selected = qwen_service._select_unlocked_candidate(
            NodeLockRedis(),
            [message(99_000, 1, 0.9), message(98_000, 2, 0.8)],
            100_000,
        )
        self.assertEqual(selected["node_id"], 2)


class M5ResultConsistencyTests(unittest.TestCase):
    def _logic(self):
        logic = qwen_15b.QwenLogic.__new__(qwen_15b.QwenLogic)
        logic.session = True
        logic.tokenizer = True
        logic.redis_client = None
        logic._last_prompt_tokens = 10
        logic._last_output_tokens = 5
        logic._ensure_model_loaded = lambda: None
        logic._fetch_hourly_context = lambda: {}
        logic._fetch_time_series = lambda: None
        logic._build_messages = lambda *_args, **_kwargs: []
        logic._apply_context_window = lambda score, _ctx: score
        logic._apply_feedback_adjustment = lambda score: score
        return logic

    def test_score_is_authoritative_for_final_level(self):
        logic = self._logic()
        logic._evaluate_with_qwen = lambda _messages: (
            '{"risk_score":0.7,"risk_level":"critical","reason":"test"}'
        )
        result = logic.evaluate({"vital": {"heart_rate": 72, "breathing_rate": 16}})
        self.assertEqual(result["risk_score"], 0.7)
        self.assertEqual(result["risk_level"], "warning")
        self.assertTrue(result["emergency"])

    def test_vital_crisis_sets_warning_floor(self):
        logic = self._logic()
        logic._evaluate_with_qwen = lambda _messages: (
            '{"risk_score":"0.1","risk_level":"critical","reason":"normal"}'
        )
        result = logic.evaluate({"vital": {"heart_rate": 130, "breathing_rate": 16}})
        self.assertEqual(result["risk_score"], 0.65)
        self.assertEqual(result["risk_level"], "warning")
        self.assertTrue(result["emergency"])

    def test_hourly_counts_events_not_result_snapshots(self):
        now_ms = 2_000_000

        class HistoryRedis:
            def xrevrange(self, stream, count):
                if stream == "ai:result":
                    payload = {"risk_level": "critical", "experts": {}}
                    return [
                        (f"{now_ms - offset}-0".encode(), {b"data": json.dumps(payload).encode()})
                        for offset in (1_000, 2_000, 3_000)
                    ]
                events = [
                    (1_000, 1, "critical", "fall"),
                    (31_000, 1, "critical", "fall"),  # 같은 노드 90초 이내 반복
                    (2_000, 2, "warning", "vital"),
                ]
                return [
                    (
                        f"{now_ms - offset}-0".encode(),
                        {b"data": json.dumps({
                            "node_id": node,
                            "risk_level": level,
                            "summary": summary,
                        }).encode()},
                    )
                    for offset, node, level, summary in events
                ]

        logic = qwen_15b.QwenLogic.__new__(qwen_15b.QwenLogic)
        logic.redis_client = HistoryRedis()
        logic.hourly_window_ms = 3_600_000
        logic.hourly_emergency_scan_limit = 600
        logic.hourly_event_sample_limit = 8
        logic.hourly_event_dedup_ms = 90_000
        logic.hourly_cache_ms = 0
        logic._hourly_cache_at_ms = 0
        logic._hourly_cache_data = None

        context = logic._fetch_hourly_context(now_ts_ms=now_ms)
        self.assertEqual(context["critical_count"], 1)
        self.assertEqual(context["warning_count"], 1)
        self.assertEqual(context["sampled_result_points"], 0)


class M5RiskPolicyTests(unittest.TestCase):
    def test_context_and_feedback_are_clamped(self):
        self.assertEqual(
            risk_policy.apply_context_window(0.95, {"recent_warning_count": 3}),
            1.0,
        )
        redis_client = _FakeRedis(state=None)
        redis_client.get = lambda _key: json.dumps({"delta": -2.0})
        self.assertEqual(
            risk_policy.apply_feedback_adjustment(0.4, redis_client, "feedback"),
            0.0,
        )

    def test_normalize_result_uses_score_as_authority(self):
        result = {"risk_score": "0.9", "risk_level": "normal", "emergency": False}
        self.assertIs(risk_policy.normalize_result(result), result)
        self.assertEqual(result["risk_level"], "critical")
        self.assertTrue(result["emergency"])

    def test_confirmed_m1_consensus_reaches_m5_gate(self):
        score, breakdown = emergency_score.compute_emergency_score({
            "fall": {
                "fall_score": 0.81,
                "fall_detected": True,
                "infer_confidence": 0.75,
            }
        })
        self.assertEqual(score, 0.65)
        self.assertTrue(breakdown["fall_consensus_bypass"])

        raw_score, raw_breakdown = emergency_score.compute_emergency_score({
            "fall": {
                "fall_score": 0.81,
                "fall_detected": False,
                "infer_confidence": 0.75,
            }
        })
        self.assertLess(raw_score, 0.6)
        self.assertNotIn("fall_consensus_bypass", raw_breakdown)


class ResourceBoundaryTests(unittest.TestCase):
    def test_docker_memory_units(self):
        self.assertEqual(bench_rpi5.parse_size_mb("2.5GiB"), 2560.0)
        self.assertEqual(bench_rpi5.parse_size_mb("430MiB"), 430.0)
        self.assertIsNone(bench_rpi5.parse_size_mb("0B"))

    def test_rpi5_stream_and_redis_limits_are_bounded(self):
        sensing = (ROOT / "sensing" / "main.py").read_text(encoding="utf-8")
        audio = (ROOT / "sensing" / "audio_main.py").read_text(encoding="utf-8")
        redis_conf = (ROOT / "db" / "redis.conf").read_text(encoding="utf-8")
        self.assertIn('os.getenv("CSI_STREAM_MAXLEN", "36_000")', sensing)
        self.assertIn('os.getenv("AUDIO_STREAM_MAXLEN", "120")', audio)
        self.assertIn("maxmemory 512mb", redis_conf)
        self.assertIn("maxmemory-policy noeviction", redis_conf)


class RuntimeInputTests(unittest.TestCase):
    def test_m1_slots_are_stable_and_incomplete_nodes_are_zero(self):
        import numpy as np

        buffers = {
            2: [np.full(64, value, dtype=np.float32) for value in (1.0, 2.0)],
            3: [np.ones(64, dtype=np.float32)],
        }
        self.assertFalse(
            runtime_inputs.m1_window_ready(
                [2, 3], buffers, max_nodes=3, window_frames=2
            )
        )
        model_input = runtime_inputs.build_m1_input(
            [2, 3], buffers, max_nodes=3, window_frames=2
        )
        self.assertEqual(model_input.shape, (1, 3, 64, 2))
        self.assertTrue(np.all(model_input[0, 0] == 0.0))
        self.assertTrue(np.all(model_input[0, 1, :, 0] == 1.0))
        self.assertTrue(np.all(model_input[0, 1, :, 1] == 2.0))
        self.assertTrue(np.all(model_input[0, 2] == 0.0))

        buffers[3].append(np.full(64, 3.0, dtype=np.float32))
        self.assertTrue(
            runtime_inputs.m1_window_ready(
                [2, 3], buffers, max_nodes=3, window_frames=2
            )
        )

    def test_m1_grid_uses_receive_time_slots(self):
        import numpy as np

        slot = runtime_inputs.grid_slot
        self.assertEqual(slot(1_000_004), slot(1_000_000))   # ±5ms 스냅
        self.assertEqual(slot(1_000_006), slot(1_000_010))

        buffer = deque(maxlen=4)
        place = runtime_inputs.place_m1_grid_frame
        self.assertEqual(place(buffer, np.ones(64), 100, None), (0, False))
        self.assertEqual(place(buffer, np.full(64, 2.0), 103, 100), (2, False))  # 2슬롯 결측
        self.assertEqual([float(f[0]) for f in buffer], [1.0, 0.0, 0.0, 2.0])
        # 같은 슬롯에 두 번째 프레임 → 최신으로 교체, 길이 유지
        self.assertEqual(place(buffer, np.full(64, 3.0), 103, 103), (0, True))
        self.assertEqual([float(f[0]) for f in buffer], [1.0, 0.0, 0.0, 3.0])
        # 긴 공백은 창 길이까지만 0으로 채운다
        self.assertEqual(place(buffer, np.full(64, 4.0), 1_000, 103), (4, False))
        self.assertEqual([float(f[0]) for f in buffer], [0.0, 0.0, 0.0, 4.0])

    def test_skipped_ticks_count_as_zero_votes(self):
        expired = runtime_inputs.expired_m1_ticks
        self.assertEqual(expired(10_199, 10_000, 200), 0)   # 현재 tick 진행 중
        self.assertEqual(expired(10_399, 10_000, 200), 0)   # 다음 tick 진행 중
        self.assertEqual(expired(10_400, 10_000, 200), 1)   # 한 tick이 추론 없이 끝남
        self.assertEqual(expired(11_000, 0, 200), 0)        # 시작 전

        votes = deque(maxlen=5)
        result = {}
        for detected in (True, True, True, True):
            result = runtime_inputs.aggregate_m1_result(
                {"fall_score": 0.9, "fall_detected": detected}, votes
            )
        result = runtime_inputs.record_skipped_m1_ticks(votes, 1, result)
        self.assertTrue(result["fall_detected"])          # 4/5
        self.assertEqual(result["fall_votes"], 4)
        result = runtime_inputs.record_skipped_m1_ticks(votes, 2, result)
        self.assertFalse(result["fall_detected"])         # 2/5: 오래된 발화가 밀려남
        self.assertEqual(result["skipped_ticks"], 3)
        result = runtime_inputs.record_skipped_m1_ticks(votes, 9, result, insufficient=True)
        self.assertEqual(result["fall_votes"], 0)
        self.assertEqual(result["input_status"], "insufficient_input")

    def test_m1_tail_requires_every_trained_node_in_current_slot(self):
        arrivals = {1: 1_000, 2: 995, 3: 990}
        self.assertTrue(
            runtime_inputs.m1_tail_ready([1, 2, 3], arrivals, 1_000, max_age_ms=10)
        )
        arrivals[3] = 989
        self.assertFalse(
            runtime_inputs.m1_tail_ready([1, 2, 3], arrivals, 1_000, max_age_ms=10)
        )

    def test_m1_three_of_five_aggregation_and_insufficient_state(self):
        votes = deque(maxlen=5)
        results = []
        for detected in (True, True, True, False, False):
            results.append(runtime_inputs.aggregate_m1_result(
                {"fall_score": 0.81, "fall_detected": detected}, votes
            ))

        self.assertFalse(results[2]["fall_detected"])  # 3/3 is not yet a complete N=5 window
        self.assertTrue(results[4]["fall_detected"])   # 3/5
        self.assertFalse(results[4]["window_fall_detected"])
        self.assertEqual(results[4]["fall_votes"], 3)
        self.assertEqual(results[4]["fall_vote_samples"], 5)
        self.assertEqual(results[4]["fall_score"], 0.81)

        insufficient = runtime_inputs.insufficient_m1_result()
        self.assertEqual(insufficient["input_status"], "insufficient_input")
        self.assertTrue(insufficient["insufficient_input"])
        self.assertFalse(insufficient["fall_detected"])


def _extract_functions(path: Path, names: set, namespace: dict) -> dict:
    """무거운 서비스 모듈을 import하지 않고 순수 함수만 뽑아 실행한다."""
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in nodes} == names, names
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


class RuleAlertTests(unittest.TestCase):
    def test_reason_only_for_confirmed_rules(self):
        reason = risk_policy.rule_alert_reason
        experts = {
            "fall": {"fall_votes": 3, "fall_vote_samples": 5},
            "vital": {"heart_rate": 135, "breathing_rate": 20},
            "env_sound": {"label": "impact"},
        }
        self.assertIn("낙상 확정(M1 3/5)", reason({"fall_consensus_bypass": True}, experts))
        self.assertIn("impact", reason({"fall_hazard_bypass": True}, experts))
        self.assertIn("HR=135", reason({"vital_bypass": True}, experts))
        self.assertIsNone(reason({"fall": 1.0, "keyword_fall_bonus": True}, experts))
        self.assertIsNone(reason({"temporal_escalation": ["sustained_warn"]}, experts))
        self.assertIsNone(reason(None, None))

    def test_single_window_fall_does_not_trigger_rule_alert(self):
        score, breakdown = emergency_score.compute_emergency_score({
            "fall": {"fall_score": 0.95, "fall_detected": False, "window_fall_detected": True},
        })
        self.assertIsNone(risk_policy.rule_alert_reason(breakdown, {}))

    def _writer(self):
        logs = []
        ns = {
            "json": json,
            "_redis": redis_stub,
            "CRITICAL_THRESHOLD": risk_policy.CRITICAL_THRESHOLD,
            "EMERGENCY_STREAM": "ai:emergency",
            "EMERGENCY_STREAM_MAXLEN": 3600,
            "PHASE2_LOCK_PREFIX": "phase2:active:",
            "logging": __import__("logging"),
            "_log": lambda level, event, **fields: logs.append(event),
        }
        _extract_functions(ROOT / "ai" / "main.py", {"_write_rule_alert"}, ns)
        return ns["_write_rule_alert"], logs

    def test_rule_alert_entry_matches_api_contract(self):
        write, _ = self._writer()
        redis = _FakeRedis()
        outcome = write(redis, 1000, 2, 0.65, {"fall_consensus_bypass": True}, "낙상 확정")
        self.assertEqual(outcome, "written")
        stream, fields, kwargs = redis.written
        entry = json.loads(fields["data"])
        self.assertEqual(stream, "ai:emergency")
        self.assertEqual(kwargs["maxlen"], 3600)
        self.assertEqual(entry["risk_level"], "critical")
        self.assertTrue(entry["emergency"])
        self.assertEqual(entry["risk_score"], 0.85)
        self.assertEqual(entry["gate_score"], 0.65)
        self.assertEqual(entry["slm_mode"], "rule")
        self.assertEqual((entry["node_id"], entry["ts_ms"], entry["summary"]), (2, 1000, "낙상 확정"))

    def test_rule_alert_respects_phase2_lock_and_redis_errors(self):
        write, logs = self._writer()
        locked = _FakeRedis(active=True)
        self.assertEqual(write(locked, 1, 1, 0.7, {}, "x"), "locked")
        self.assertIsNone(locked.written)

        class _FullRedis(_FakeRedis):
            def xadd(self, *_args, **_kwargs):
                raise _RedisError("OOM command not allowed")

        self.assertEqual(write(_FullRedis(), 1, 1, 0.7, {}, "x"), "failed")
        self.assertIn("rule_alert_write_failed", logs)


class Phase2TranscriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = _extract_functions(
            ROOT / "api" / "main.py", {"_fresh_transcript", "_classify_phase2"}, {}
        )

    def _snapshot(self, audio_ts, text="괜찮아요", detected=True):
        return {
            "audio": {"ts_ms": audio_ts},
            "experts": {"speech_ko": {"speech_detected": detected, "transcript_ko": text}},
        }

    def test_only_audio_recorded_after_tts_counts(self):
        fresh = self.ns["_fresh_transcript"]
        self.assertIsNone(fresh(self._snapshot(999), since_ms=1000))   # 경보 전 발화·에코
        self.assertEqual(fresh(self._snapshot(1000), since_ms=1000), "괜찮아요")
        self.assertIsNone(fresh(self._snapshot(2000, detected=False), since_ms=1000))
        self.assertIsNone(fresh(self._snapshot(2000, text="  "), since_ms=1000))
        self.assertIsNone(fresh({"experts": {}}, since_ms=0))

    def test_intent_classification_regression(self):
        classify = self.ns["_classify_phase2"]
        self.assertEqual(classify(None), "call_emergency")
        self.assertEqual(classify("괜찮아요"), "cancel_alarm")
        self.assertEqual(classify("안 괜찮아"), "call_emergency")
        self.assertEqual(classify("살려주세요"), "call_emergency")

    def test_cannot_move_is_not_classified_as_fine(self):
        classify = self.ns["_classify_phase2"]
        for text in ("일어날 수가 없어", "힘이 없어", "아니 못 일어나겠어", "움직일 수 없어요",
                     "숨이 차", "어지러워"):
            self.assertEqual(classify(text), "call_emergency", text)
        for text in ("괜찮아요", "안 아파요", "멀쩡해", "안 다쳤어"):
            self.assertEqual(classify(text), "cancel_alarm", text)

    def test_alert_message_names_node_and_reason(self):
        ns = _extract_functions(ROOT / "api" / "notifier.py", {"build_risk_message"}, {"Any": object})
        title, body = ns["build_risk_message"](0.85, "critical", True, "낙상 확정(M1 3/5)", 2)
        self.assertEqual(title, "응급 상황 감지")
        self.assertIn("[노드 2]", body)
        self.assertIn("낙상 확정", body)
        title, body = ns["build_risk_message"](0.7, "warning", False)
        self.assertEqual(title, "이상 징후 감지")
        self.assertNotIn("노드", body)


if __name__ == "__main__":
    unittest.main()
