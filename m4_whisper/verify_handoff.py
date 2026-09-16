"""Maintainer integration checks; not a new corpus accuracy evaluation."""

import argparse
import gc
import json
from pathlib import Path

import ctranslate2
import numpy as np
import soundfile as sf
from faster_whisper import WhisperModel

from .guard import make_repetition_guard_model_class
from .runtime import SpeechRecognizer, sha256_file
from .settings import MODELS, guard_settings, transcribe_options


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-root', type=Path, default=Path('m4_artifacts/ct2'))
    parser.add_argument('--audio', type=Path, action='append', required=True)
    parser.add_argument('--output', type=Path, default=Path('m4_whisper/verification.json'))
    args = parser.parse_args()
    report = {'purpose': 'integration smoke, not corpus evaluation', 'seed': 42,
              'calls': [], 'onnx_exports': {}}
    fixed = transcribe_options()
    for variant, (precision, _) in MODELS.items():
        directory = args.model_root / variant
        recognizer = SpeechRecognizer(directory, variant)
        guarded = make_repetition_guard_model_class(WhisperModel)
        control = guarded(str(directory), device='cpu', compute_type=precision,
                          cpu_threads=6, num_workers=1, local_files_only=True,
                          repetition_guard=guard_settings())
        for audio_path in args.audio:
            waveform, rate = sf.read(audio_path, dtype='float32')
            ctranslate2.set_random_seed(42)
            result = recognizer.transcribe(waveform, rate, window_id='smoke')
            ctranslate2.set_random_seed(42)
            segments, _ = control.transcribe(waveform, **fixed)
            segments = list(segments)
            expected_text = ''.join(s.text for s in segments).strip()
            expected_tokens = [list(s.tokens) for s in segments]
            actual_tokens = [s['token_ids'] for s in result['segments']]
            assert result['text'] == expected_text
            assert actual_tokens == expected_tokens
            assert not recognizer.model.repetition_guard_events
            control.repetition_guard_events.clear()
            report['calls'].append({
                'model': variant, 'audio_sha256': sha256_file(audio_path),
                'duration_seconds': waveform.size / rate,
                'wrapper_matches_direct_text_and_tokens': True, 'result': result,
            })
        del recognizer, control
        gc.collect()
    for variant in ['baseline_fp32', 'finetuned_fp32']:
        path = args.model_root.parent / 'onnx' / variant / 'export_manifest.json'
        data = json.loads(path.read_text(encoding='utf-8'))
        report['onnx_exports'][variant] = data['validation']
    report['unique_audio_files'] = len(args.audio)
    report['ct2_wrapper_calls'] = len(report['calls'])
    report['ct2_direct_control_calls'] = len(report['calls'])
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'wrapper_direct_matches': len(report['calls']),
                      'unique_audio_files': len(args.audio)}), flush=True)


if __name__ == '__main__':
    main()
