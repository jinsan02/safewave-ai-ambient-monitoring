import importlib.util

import pytest


def runtime():
    assert importlib.util.find_spec('m4_whisper.onnx_runtime') is not None, 'ONNX runtime missing'
    from m4_whisper import onnx_runtime
    return onnx_runtime


def candidate(module, text, score=-0.3, cap=False, silence=False):
    return module.make_candidate(text, [100, 101], score, 0.9 if silence else 0.0,
                                 0.0, cap, candidate_id='test')


def test_onnx_selection_rescues_low_score_candidate():
    m = runtime()
    early = candidate(m, 'help', -1.2)
    repeat = candidate(m, 'blue blue blue blue blue', -0.4)
    chosen, event = m.finish_candidates([early, repeat], repeat, 'selection_regenerate',
                                      lambda: pytest.fail('unnecessary regeneration'))
    assert chosen is early
    assert event['rescued_low_logprob']


def test_onnx_regeneration_once_and_unresolved_preserves_original():
    m = runtime()
    repeat = candidate(m, 'blue blue blue blue blue')
    calls = []
    def regenerate():
        calls.append(1)
        return candidate(m, 'again again again again again')
    chosen, event = m.finish_candidates([repeat], repeat, 'selection_regenerate', regenerate)
    assert chosen is repeat
    assert len(calls) == 1
    assert event['unresolved_repetition']


@pytest.mark.parametrize('cap,silence,text', [(True, False, 'short'), (False, True, 'short'),
                                             (False, False, '')])
def test_onnx_invalid_regeneration_not_counted_as_success(cap, silence, text):
    m = runtime()
    repeat = candidate(m, 'blue blue blue blue blue')
    chosen, event = m.finish_candidates([repeat], repeat, 'selection_regenerate',
                                      lambda: candidate(m, text, -1.2, cap, silence))
    assert chosen is repeat
    assert not event['regeneration_success']


def test_onnx_off_and_normal_repeat_unchanged():
    m = runtime()
    for text, mode in [('blue blue blue blue blue', 'off'), ('help help', 'selection_regenerate')]:
        original = candidate(m, text)
        chosen, event = m.finish_candidates([original], original, mode,
                                          lambda: pytest.fail('unnecessary regeneration'))
        assert chosen is original
        assert event['additional_calls'] == 0


def test_onnx_regeneration_success_keeps_nonrepetitive_mistake():
    m = runtime()
    repeat = candidate(m, 'blue blue blue blue blue')
    mistake = candidate(m, 'wrong word')
    chosen, event = m.finish_candidates([repeat], repeat, 'selection_regenerate', lambda: mistake)
    assert chosen is mistake
    assert event['regeneration_success']


def test_silence_decision_not_overridden_by_guard():
    m = runtime()
    silent = candidate(m, 'blue blue blue blue blue', -1.2, silence=True)
    chosen, event = m.finish_candidates([silent], silent, 'selection_regenerate',
                                      lambda: pytest.fail('silence must not regenerate'))
    assert chosen is silent
    assert event['selection_reason'] == 'preserve_no_speech_skip'


def test_sampling_score_is_restored_before_cross_temperature_comparison():
    import torch
    m = runtime()
    assert hasattr(m, 'restore_sampling_scores')
    raw = torch.tensor([[1.0, 2.0, 3.0]])
    restored = m.restore_sampling_scores((raw / 0.2,), 0.2)
    torch.testing.assert_close(restored[0], raw)


def test_already_generated_alternative_is_rescued_without_regeneration():
    m = runtime()
    assert hasattr(m, 'run_fallback')
    seen = []
    def generate(temp, ngram, run_id):
        seen.append((temp, ngram))
        repeat = candidate(m, 'blue blue blue blue blue', -1.3 if temp == 0 else -0.3)
        alternative = candidate(m, 'wrong but not repeated', -1.4)
        return repeat, [repeat] if temp == 0 else [repeat, alternative]
    chosen, event, attempts = m.run_fallback(generate, 'selection_regenerate')
    assert chosen['text'] == 'wrong but not repeated'
    assert all(ngram == 0 for _, ngram in seen)
    assert event['generation_calls'] == len(seen)


def test_repetition_forced_fallback_calls_are_counted():
    m = runtime()
    assert hasattr(m, 'run_fallback')
    def generate(temp, ngram, run_id):
        c = candidate(m, 'blue blue blue blue blue')
        return c, [c]
    _, event, _ = m.run_fallback(generate, 'selection_regenerate')
    assert event['generation_calls'] == 7
    assert event['calls_after_first_stock_stop'] == 6
    assert event['additional_calls'] == 1
