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

    def test_m1_gap_preserves_grid_with_zero_frames(self):
        import numpy as np

        buffer = deque(maxlen=4)
        runtime_inputs.append_m1_grid_frame(
            buffer, np.ones(64), 0, frame_interval_ms=10
        )
        missing, clock_reset = runtime_inputs.append_m1_grid_frame(
            buffer, np.full(64, 2.0), 30, frame_interval_ms=10
        )
        self.assertEqual(missing, 2)
        self.assertFalse(clock_reset)
        self.assertTrue(np.all(buffer[0] == 1.0))
        self.assertTrue(np.all(buffer[1] == 0.0))
        self.assertTrue(np.all(buffer[2] == 0.0))
        self.assertTrue(np.all(buffer[3] == 2.0))

    def test_m1_device_clock_delta_handles_uint32_wrap(self):
        self.assertEqual(
            runtime_inputs.device_time_delta_ms(0x00000005, 0xFFFFFFFB), 10
        )
        self.assertEqual(runtime_inputs.device_time_delta_ms(990, 1000), -10)

    def test_m1_clock_reset_restarts_only_the_grid(self):
        import numpy as np

        buffer = deque(maxlen=4)
        buffer.append(np.ones(64))
        missing, clock_reset = runtime_inputs.append_m1_grid_frame(
            buffer, np.full(64, 2.0), -100, frame_interval_ms=10
        )
        self.assertEqual(missing, 0)
        self.assertTrue(clock_reset)
        self.assertEqual(len(buffer), 1)
        self.assertTrue(np.all(buffer[0] == 2.0))

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


if __name__ == "__main__":
    unittest.main()
