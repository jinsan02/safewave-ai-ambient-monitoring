import json
from types import SimpleNamespace
import threading

import numpy as np
import pytest

from m4_whisper.runtime import SpeechRecognizer, validate_audio, render_result
from m4_whisper.settings import transcribe_options, guard_settings
from m4_whisper.guard import detect_suspicious_repetition
from m4_whisper.guard import make_repetition_guard_model_class


def test_audio_is_an_owned_float32_snapshot():
    original = np.zeros(80000, dtype=np.float32)
    snapshot = validate_audio(original, 16000)
    snapshot[0] = 0.5
    assert original[0] == 0
    assert snapshot.shape == (80000,)


@pytest.mark.parametrize('audio,rate', [
    (np.zeros(4, dtype=np.int16), 16000),
    (np.zeros((4, 2), dtype=np.float32), 16000),
    (np.zeros(4, dtype=np.float32), 48000),
    (np.zeros(0, dtype=np.float32), 16000),
    (np.zeros(480001, dtype=np.float32), 16000),
    (np.array([np.nan], dtype=np.float32), 16000),
    (np.array([np.inf], dtype=np.float32), 16000),
    (np.array([1.1], dtype=np.float32), 16000),
])
def test_invalid_audio_is_rejected_not_silently_reprocessed(audio, rate):
    with pytest.raises((ValueError, TypeError)):
        validate_audio(audio, rate)


def segment(text='hello', start=0.1, end=1.0):
    return SimpleNamespace(text=text, start=start, end=end, tokens=[10, 11],
                           avg_logprob=-0.4, no_speech_prob=0.1,
                           compression_ratio=0.7, temperature=0.0)


def test_result_preserves_text_and_offsets_and_is_json_serializable():
    result = render_result([segment(' hello hello')], [], 'w1', 12.0, 5.0,
                           'finetuned_int8', 'int8_float32', 0.3)
    assert result['text'] == 'hello hello'
    assert result['status'] == 'ok'
    assert result['segments'][0]['start_seconds'] == pytest.approx(12.1)
    assert result['window_end_seconds'] == 17
    assert result['segments'][0]['end_seconds'] == 13
    assert 'confidence' not in result
    json.dumps(result, allow_nan=False)


def test_no_text_is_not_declared_silence_or_recognition_success():
    result = render_result([], [], 'w', 0, 5, 'baseline_fp32', 'float32', 1)
    assert result['status'] == 'no_text'
    assert result['text'] == ''


def test_guard_diagnostics_are_separate_from_final_output():
    events = [{'regeneration_attempted': True,
               'regeneration_suppressed_repetition': False,
               'unresolved_repetition': True, 'rescued_low_logprob': False,
               'additional_generation_calls': 1, 'selection_reason': 'failed'}]
    result = render_result([segment('abcabcabc')], events, 'w', 0, 5,
                           'baseline_int8', 'int8_float32', 1)
    assert result['text'] == 'abcabcabc'
    assert result['guard']['regenerations'] == 1
    assert result['guard']['unresolved_candidates'] == 1
    assert result['guard']['final_repetition_suspected']


def test_final_repetition_flag_includes_token_only_detection():
    item = segment(' please help me right now' * 5)
    item.tokens = [50364] + [10, 11, 12, 13, 14] * 5 + [50564]
    assert not detect_suspicious_repetition(item.text)['suspected']
    result = render_result([item], [], 'w', 0, 5,
                           'baseline_fp32', 'float32', 0.1)
    assert result['guard']['final_repetition_suspected']


def test_settings_match_historical_experiment():
    opts = transcribe_options()
    assert opts['beam_size'] == 5
    assert opts['temperature'] == [0, .2, .4, .6, .8, 1]
    assert opts['no_repeat_ngram_size'] == 0
    assert opts['repetition_penalty'] == 1
    assert opts['max_new_tokens'] is None
    assert not opts['without_timestamps']
    guard = guard_settings()
    assert guard.mode == 'selection_regenerate'
    assert guard.regeneration_no_repeat_ngram_size == 3
    assert guard_settings('off').mode == 'off'


def test_short_actual_repetition_is_not_unconditionally_removed():
    text = 'help help'
    assert not detect_suspicious_repetition(text)['suspected']
    assert detect_suspicious_repetition('blueblueblueblue')['suspected']


def test_inference_failure_is_propagated_and_trace_memory_is_cleared():
    def failing_stream(*args, **kwargs):
        def segments():
            recognizer.model.repetition_guard_events.append({'failed': True})
            raise RuntimeError('CT2 failed')
            yield
        return segments(), None

    recognizer = SpeechRecognizer.__new__(SpeechRecognizer)
    recognizer._lock = threading.Lock()
    recognizer.model = SimpleNamespace(transcribe=failing_stream,
                                       repetition_guard_events=[])
    with pytest.raises(RuntimeError, match='CT2 failed'):
        recognizer.transcribe(np.zeros(16000, dtype=np.float32))
    assert recognizer.model.repetition_guard_events == []


def test_timestamp_estimate_outside_audio_is_exposed_not_hidden():
    result = render_result([segment(end=5.1)], [], 'w', 10, 5,
                           'baseline_fp32', 'float32', 0.1)
    assert result['segments'][0]['end_seconds'] == 15.1
    assert result['segments'][0]['timestamp_outside_window']


def test_guard_regenerates_once_with_n3_and_does_not_erase_failure():
    calls = []
    results = [SimpleNamespace(sequences_ids=[[1, 1, 1, 1]], scores=[-0.2], no_speech_prob=0.),
               SimpleNamespace(sequences_ids=[[2, 2, 2, 2]], scores=[-0.2], no_speech_prob=0.)]
    original = results[0]

    class Backend:
        def generate(self, *args, **kwargs):
            calls.append(kwargs)
            return [results.pop(0)]

    class Base:
        max_length = 448
        time_precision = .02

        def __init__(self):
            self.model = Backend()

    guarded = make_repetition_guard_model_class(Base)(repetition_guard=guard_settings())
    tokenizer = SimpleNamespace(eot=50257, decode=lambda ids: 'loop' * 4)
    options = SimpleNamespace(temperatures=[0., .2], beam_size=5, best_of=5,
                              max_initial_timestamp=1., max_new_tokens=None,
                              patience=1, length_penalty=1, repetition_penalty=1,
                              no_repeat_ngram_size=0, suppress_blank=True,
                              suppress_tokens=[-1], compression_ratio_threshold=2.4,
                              log_prob_threshold=-1., no_speech_threshold=.6)
    chosen, *_ = guarded.generate_with_fallback('encoded', [50258], tokenizer, options)
    assert chosen is original
    assert len(calls) == 2
    assert calls[0]['no_repeat_ngram_size'] == 0
    assert calls[1]['no_repeat_ngram_size'] == 3
    assert calls[1]['beam_size'] == 5
    assert 'sampling_temperature' not in calls[1]
    assert guarded.repetition_guard_events[-1]['unresolved_repetition']
