from .guard import RepetitionGuardSettings


MODELS = {
    'baseline_fp32': ('float32', 'b36f7608d8809e0c0dcde4c3cd6a5c08feeaf6eaa05ba0b38872e1e63c2c27ca'),
    'baseline_int8': ('int8_float32', '35c79c3e2a57bf7d26d35cdacf7affbba3341ed4b5a692dc7875031509b119cd'),
    'finetuned_fp32': ('float32', '30c50464afb1ca9fb0222e2e9d774d2056bd61f1a004dfb3fbf9d9a969898cbb'),
    'finetuned_int8': ('int8_float32', 'b7eee8e7be0a97274745b36aa47b489a9e55872ecf356c220ecb5372b305f1cb'),
}


def guard_settings(mode='selection_regenerate'):
    return RepetitionGuardSettings(
        mode=mode, regeneration_no_repeat_ngram_size=3,
        generation_cap_tokens=224,
    )


def transcribe_options():
    return {
        'language': 'ko', 'task': 'transcribe', 'beam_size': 5, 'best_of': 5,
        'patience': 1, 'length_penalty': 1, 'repetition_penalty': 1,
        'no_repeat_ngram_size': 0, 'temperature': [0., .2, .4, .6, .8, 1.],
        'compression_ratio_threshold': 2.4, 'log_prob_threshold': -1.,
        'no_speech_threshold': .6, 'condition_on_previous_text': True,
        'prompt_reset_on_temperature': .5, 'initial_prompt': None, 'prefix': None,
        'suppress_blank': True, 'suppress_tokens': [-1],
        'without_timestamps': False, 'max_initial_timestamp': 1.,
        'word_timestamps': False, 'vad_filter': False, 'max_new_tokens': None,
        'chunk_length': None, 'clip_timestamps': '0',
        'hallucination_silence_threshold': None, 'hotwords': None,
    }
