"""M3 환경음 전문가(AST 6-class end-to-end ONNX) 테스트.

모델 파일이 없는 환경에서도 돌도록, ONNX 실추론 테스트는 모델이 있을 때만 수행한다
(`volumes/models/ast_onnx/*.onnx` 또는 M3_ENV_SOUND_MODEL 경로).

    python -m unittest tests.test_m3_onnx -v
"""

import os
import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
AI_DIR = ROOT / "ai"
if str(AI_DIR) not in sys.path:
    sys.path.insert(0, str(AI_DIR))

from experts.m3_ast_base import ActivityClassificationModel, EnvSoundAnalysisModel  # noqa: E402

SAMPLE_RATE = 16000
WINDOW_SAMPLES = SAMPLE_RATE * 3


def _model_dir() -> Path:
    configured = os.getenv("M3_ENV_SOUND_MODEL", "ast_onnx")
    path = Path(configured)
    return path if path.is_absolute() else ROOT / "volumes" / "models" / configured


def _has_model() -> bool:
    directory = _model_dir()
    return directory.is_dir() and any(directory.glob("*.onnx"))


class TestPreprocessing(unittest.TestCase):
    """모델 없이도 성립해야 하는 입력 정규화 규칙."""

    def setUp(self):
        self.model = EnvSoundAnalysisModel(str(ROOT / "volumes" / "models" / "__missing__"))

    def test_labels_are_the_six_class_contract(self):
        self.assertEqual(
            EnvSoundAnalysisModel.ENV_LABELS,
            ["silence", "speech", "impact", "noise", "alarm", "unknown"],
        )
        self.assertEqual(
            EnvSoundAnalysisModel.ENV_LABELS[EnvSoundAnalysisModel.IMPACT_INDEX], "impact"
        )

    def test_short_audio_is_front_padded_to_window(self):
        data, _ = self.model._preprocess(np.ones(1000, dtype=np.float32))
        self.assertEqual(data.shape, (1, WINDOW_SAMPLES))
        # 앞쪽이 0으로 채워지고 최근 소리가 끝에 남는다
        self.assertEqual(float(data[0, 0]), 0.0)
        self.assertEqual(float(data[0, -1]), 1.0)

    def test_peak_mode_keeps_the_impact_inside_the_window(self):
        """긴 입력에서 충격음이 창 밖으로 밀리면 안 된다.

        audio-sensing VAD 이벤트는 최대 6초다. 낙상은 충격 뒤 신음·뒤척임이 이어져
        마지막 3초만 쓰면 정작 '쿵'이 잘려 나간다 (실측 46/50 → 13/50).
        """
        def lag_of(impact_at_s: float) -> float:
            """충격을 넣은 6초 버퍼 → 선택된 창에서 '창 끝까지 남은 시간'(초)."""
            six_seconds = np.zeros(SAMPLE_RATE * 6, dtype=np.float32)
            six_seconds[int(SAMPLE_RATE * impact_at_s)] = 1.0
            data = self.model._preprocess(six_seconds)[0][0]
            self.assertEqual(data.size, WINDOW_SAMPLES)
            self.assertEqual(float(np.max(np.abs(data))), 1.0, "충격음이 창 밖으로 잘렸다")
            return (WINDOW_SAMPLES - int(np.argmax(np.abs(data)))) / SAMPLE_RATE

        # 충격 앞에 여유가 있으면 창 끝에서 0.3~2.2초(실측 최적 구간)에 놓는다
        self.assertGreater(lag_of(3.0), 0.3)
        self.assertLess(lag_of(3.0), 2.2)

        # 충격이 버퍼 맨 앞이라 앞쪽 여유가 없어도, 최소한 창 안에는 들어와야 한다
        self.assertLess(lag_of(0.5), 3.0)

    def test_latest_mode_keeps_the_most_recent_window(self):
        self.model.window_mode = "latest"
        try:
            signal = np.arange(WINDOW_SAMPLES * 2, dtype=np.float32) / (WINDOW_SAMPLES * 2)
            data, _ = self.model._preprocess(signal)
            np.testing.assert_allclose(data[0], signal[-WINDOW_SAMPLES:], atol=1e-6)
        finally:
            self.model.window_mode = "peak"

    def test_dict_input_with_sample_rate_is_resampled(self):
        # 8kHz 1.5초 = 12000 샘플 → 16kHz 3초 윈도우로 정규화
        payload = {"waveform": np.zeros(12000, dtype=np.float32), "sample_rate": 8000}
        data, _ = self.model._preprocess(payload)
        self.assertEqual(data.shape, (1, WINDOW_SAMPLES))

    def test_no_audio_returns_no_audio_source(self):
        for empty in (None, {"sample_rate": SAMPLE_RATE}, np.zeros(0, dtype=np.float32)):
            result = self.model.infer(empty)
            self.assertEqual(result["env_sound_source"], "no-audio")
            self.assertEqual(result["env_sound_label"], "silence")
            self.assertFalse(result["impact_alert"])

    def test_heuristic_fallback_when_model_missing(self):
        result = self.model.infer(np.zeros(WINDOW_SAMPLES, dtype=np.float32))
        self.assertIsNone(self.model.session)
        self.assertEqual(self.model.backend, "heuristic")
        self.assertEqual(result["env_sound_source"], "heuristic")
        self.assertIn(result["env_sound_label"], EnvSoundAnalysisModel.ENV_LABELS)
        self.assertFalse(result["impact_alert"])

    def test_gate_uses_pre_gain_peak_from_metadata(self):
        """sensing 이 조용한 이벤트를 0.85까지 증폭해 보내므로, 게이트는 받은 파형이
        아니라 메타의 raw_peak(보정 전)로 판정해야 한다."""
        loud_after_gain = (np.ones(WINDOW_SAMPLES, dtype=np.float32) * 0.85)
        _, gate_peak = self.model._preprocess(
            {"waveform": loud_after_gain, "sample_rate": SAMPLE_RATE, "raw_peak": 0.0031}
        )
        self.assertAlmostEqual(gate_peak, 0.0031, places=6)
        self.assertLess(gate_peak, self.model.silence_gate)

    def test_gate_peak_is_none_without_metadata(self):
        _, gate_peak = self.model._preprocess(np.ones(WINDOW_SAMPLES, dtype=np.float32) * 0.5)
        self.assertIsNone(gate_peak)

    def test_alias_class_still_available(self):
        self.assertTrue(issubclass(ActivityClassificationModel, EnvSoundAnalysisModel))


@unittest.skipUnless(_has_model(), f"ONNX 모델 없음: {_model_dir()}")
class TestOnnxInference(unittest.TestCase):
    """실제 ONNX 로드 후 계약(출력 키·형상·판정 규칙) 검증."""

    @classmethod
    def setUpClass(cls):
        cls.model = EnvSoundAnalysisModel(str(_model_dir()))

    def test_session_loaded_with_expected_graph_io(self):
        self.assertIsNotNone(self.model.session)
        self.assertEqual(self.model.backend, "onnx")
        self.assertEqual(self.model.input_name, "waveform")
        self.assertEqual(self.model.window_samples, WINDOW_SAMPLES)
        self.assertIn("probs", self.model.output_names)
        self.assertIn("raw_peak", self.model.output_names)

    def test_silence_gate_forces_silence_label(self):
        # 게이트(0.005) 아래의 아주 작은 신호
        quiet = (np.random.default_rng(0).normal(size=WINDOW_SAMPLES) * 1e-4).astype(np.float32)
        result = self.model.infer(quiet)
        self.assertEqual(result["env_sound_label"], "silence")
        self.assertTrue(result["silence_gated"])
        self.assertFalse(result["impact_alert"])
        self.assertEqual(result["env_sound_confidence"], 1.0)

    def test_audible_input_returns_model_label_and_probs(self):
        rng = np.random.default_rng(1)
        signal = (rng.normal(size=WINDOW_SAMPLES) * 0.2).astype(np.float32)
        result = self.model.infer(signal)

        self.assertEqual(result["env_sound_source"], "onnx")
        self.assertFalse(result["silence_gated"])
        self.assertIn(result["env_sound_label"], EnvSoundAnalysisModel.ENV_LABELS)

        probs = result["env_sound_probs"]
        self.assertEqual(set(probs), set(EnvSoundAnalysisModel.ENV_LABELS))
        self.assertAlmostEqual(sum(probs.values()), 1.0, places=2)
        # 라벨은 최대 확률 클래스와 일치해야 한다
        self.assertEqual(result["env_sound_label"], max(probs, key=probs.get))

    def test_impact_alert_follows_threshold(self):
        rng = np.random.default_rng(2)
        signal = (rng.normal(size=WINDOW_SAMPLES) * 0.2).astype(np.float32)
        result = self.model.infer(signal)
        expected = result["impact_prob"] >= self.model.impact_threshold
        self.assertEqual(result["impact_alert"], expected)

    def test_output_keys_match_the_runtime_contract(self):
        result = self.model.infer(np.zeros(WINDOW_SAMPLES, dtype=np.float32))
        for key in ("env_sound_label", "env_sound_confidence", "env_sound_source",
                    "activity", "activity_confidence",
                    "raw_peak", "impact_prob", "impact_alert", "silence_gated"):
            self.assertIn(key, result)
        self.assertEqual(result["activity"], result["env_sound_label"])

    def test_metadata_raw_peak_triggers_gate_even_for_loud_waveform(self):
        rng = np.random.default_rng(5)
        loud = (rng.normal(size=WINDOW_SAMPLES) * 0.4).astype(np.float32)
        result = self.model.infer(
            {"waveform": loud, "sample_rate": SAMPLE_RATE, "raw_peak": 0.002}
        )
        self.assertTrue(result["silence_gated"])
        self.assertEqual(result["env_sound_label"], "silence")
        self.assertFalse(result["impact_alert"])
        self.assertAlmostEqual(result["raw_peak"], 0.002, places=6)

    def test_dict_and_array_inputs_agree(self):
        rng = np.random.default_rng(3)
        signal = (rng.normal(size=WINDOW_SAMPLES) * 0.15).astype(np.float32)
        from_array = self.model.infer(signal)
        from_dict = self.model.infer({"waveform": signal, "sample_rate": SAMPLE_RATE})
        self.assertEqual(from_array["env_sound_label"], from_dict["env_sound_label"])
        self.assertAlmostEqual(from_array["impact_prob"], from_dict["impact_prob"], places=6)


if __name__ == "__main__":
    unittest.main()
