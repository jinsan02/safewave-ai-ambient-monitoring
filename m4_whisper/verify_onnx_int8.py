"""Small local ONNX smoke and export checks. Not a corpus accuracy benchmark."""

import argparse
import csv
import gc
import importlib.metadata
import json
from pathlib import Path

import numpy as np
import soundfile as sf
import torch

from .onnx_runtime import OnnxSpeechRecognizer
from .runtime import sha256_file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-dir', type=Path, required=True)
    parser.add_argument('--fp32-dir', type=Path, required=True)
    parser.add_argument('--hf-source', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    torch.set_num_threads(6)
    torch.manual_seed(42)
    with args.manifest.open(encoding='utf-8-sig', newline='') as handle:
        rows = list(csv.DictReader(handle))
    # Fixed by manifest order, not selected by model output.
    chosen = rows[:2] + [next(row for row in rows if row['evaluation_source'] != 'direct_recording')]
    recognizer = OnnxSpeechRecognizer(args.model_dir)
    report = {'purpose': 'integration smoke only; not corpus CER/WER', 'seed': 42,
              'selection': 'first two manifest rows and first non-direct-recording row',
              'manifest_sha256': sha256_file(args.manifest), 'calls': [],
              'model_graphs': json.loads((args.model_dir / 'onnx_manifest.json').read_text())['graphs'],
              'versions': {n: importlib.metadata.version(n) for n in
                           ['torch', 'transformers', 'optimum', 'optimum-onnx', 'onnxruntime', 'numpy']}}
    for row in chosen:
        waveform, rate = sf.read(row['wav_path'], dtype='float32')
        before = waveform.copy()
        result = recognizer.transcribe(waveform, rate, window_id=row['audio_id'])
        assert np.array_equal(waveform, before)
        report['calls'].append({'sample_id': row['audio_id'], 'reference': row['transcript'],
                                'reference_status': 'existing manifest; not newly listened to',
                                'audio_sha256': sha256_file(row['wav_path']), 'result': result})
        print(json.dumps({'sample': row['audio_id'], 'text': result['text'],
                          'seconds': result['elapsed_seconds']}), flush=True)
    silence = recognizer.transcribe(np.zeros(16000 * 5, dtype=np.float32), window_id='synthetic_digital_silence')
    report['synthetic_silence'] = silence
    # Reset only at the start of an independent reproducibility run.
    torch.manual_seed(42)
    waveform, rate = sf.read(chosen[0]['wav_path'], dtype='float32')
    repeated = recognizer.transcribe(waveform, rate, window_id=chosen[0]['audio_id'])
    first = report['calls'][0]['result']
    assert repeated['text'] == first['text']
    assert repeated['raw_selected_candidate']['token_ids'] == first['raw_selected_candidate']['token_ids']
    report['same_seed_repeat_first_sample_matches'] = True
    report['unique_real_audio_files'] = len(chosen)
    report['runtime_calls'] = len(chosen) + 2
    report['source_weight_unchanged'] = sha256_file(args.hf_source / 'model.safetensors')

    # Export fidelity uses FP32 ONNX against the original HF weights, not INT8 equality.
    from transformers import WhisperForConditionalGeneration
    from optimum.onnxruntime import ORTModelForSpeechSeq2Seq
    hf = WhisperForConditionalGeneration.from_pretrained(args.hf_source, local_files_only=True,
                                                         dtype=torch.float32, attn_implementation='eager').eval()
    ort = ORTModelForSpeechSeq2Seq.from_pretrained(args.fp32_dir, local_files_only=True,
                                                 provider='CPUExecutionProvider', use_cache=True,
                                                 use_merged=False, use_io_binding=False)
    features = recognizer.processor.feature_extractor(waveform, sampling_rate=rate,
                                                       return_tensors='pt').input_features
    fidelity = {'encoder_tolerance': {'atol': 0.003, 'rtol': 0.005},
                'decoder_tolerance': {'atol': 0.001, 'rtol': 0.005}, 'decoder': []}
    with torch.inference_mode():
        expected = hf.model.encoder(features).last_hidden_state
        actual = ort.encoder(features, attention_mask=None).last_hidden_state
        np.testing.assert_allclose(actual.numpy(), expected.numpy(), atol=0.003, rtol=0.005)
        fidelity['encoder_max_absolute_error'] = float((actual - expected).abs().max())
        for length in (4, 8):
            ids = torch.tensor([recognizer.prefix + [220] * (length - 4)])
            hidden = hf.model.decoder(input_ids=ids, encoder_hidden_states=expected, use_cache=False).last_hidden_state
            hf_logits = hf.proj_out(hidden)
            ort_output = ort.decoder(input_ids=ids, encoder_hidden_states=expected, use_cache=True)
            np.testing.assert_allclose(ort_output.logits.numpy(), hf_logits.numpy(), atol=0.001, rtol=0.005)
            fidelity['decoder'].append({'prefix_length': length,
                                        'max_absolute_error': float((ort_output.logits - hf_logits).abs().max()),
                                        'last_argmax_equal': bool(torch.equal(ort_output.logits[:, -1].argmax(-1), hf_logits[:, -1].argmax(-1)))})
        token = ort_output.logits[:, -1].argmax(-1).reshape(1, 1)
        cached = ort.decoder_with_past(input_ids=token, encoder_hidden_states=expected,
                                      past_key_values=ort_output.past_key_values, use_cache=True)
        whole = ort.decoder(input_ids=torch.cat((ids, token), dim=1), encoder_hidden_states=expected, use_cache=True)
        np.testing.assert_allclose(cached.logits[:, -1].numpy(), whole.logits[:, -1].numpy(), atol=0.001, rtol=0.005)
        fidelity['cached_decoder_matches_full_prefix'] = True
    report['fp32_export_fidelity'] = fidelity
    report['default_export_validation_note'] = 'Optimum default atol=1e-5 warned; independent real-input comparison uses stated tolerances, not hidden export success.'
    del ort, hf
    gc.collect()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    print('Smoke, reproducibility and FP32 export fidelity complete.', flush=True)


if __name__ == '__main__':
    main()
