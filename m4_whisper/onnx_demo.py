"""ONNX graph smoke demo, NOT the benchmarked CT2/guard decoding policy."""

import argparse
import json
from pathlib import Path
import sys
import time

import numpy as np

from .runtime import validate_audio


def infer(model_dir, audio, sample_rate=16000):
    import onnxruntime as ort
    from tokenizers import Tokenizer
    from faster_whisper.feature_extractor import FeatureExtractor
    waveform = validate_audio(audio, sample_rate)
    model_dir = Path(model_dir)
    tokenizer = Tokenizer.from_file(str(model_dir / 'tokenizer.json'))
    ids = [tokenizer.token_to_id(token) for token in
           ['<|startoftranscript|>', '<|ko|>', '<|transcribe|>', '<|notimestamps|>']]
    eos = tokenizer.token_to_id('<|endoftext|>')
    if any(value is None for value in ids) or eos is None:
        raise ValueError('Whisper multilingual tokenizer required.')
    waveform = np.pad(waveform, (0, 480000 - waveform.size))
    features = FeatureExtractor()(waveform, padding=0)[None].astype(np.float32)
    if features.shape != (1, 80, 3000):
        raise ValueError(f'Unexpected feature shape: {features.shape}')
    options = ort.SessionOptions()
    options.intra_op_num_threads = 6
    options.inter_op_num_threads = 1
    encoder = ort.InferenceSession(str(model_dir / 'encoder.onnx'),
                                   sess_options=options, providers=['CPUExecutionProvider'])
    decoder = ort.InferenceSession(str(model_dir / 'decoder.onnx'),
                                   sess_options=options, providers=['CPUExecutionProvider'])
    start = time.perf_counter()
    hidden = encoder.run(None, {'input_features': features})[0]
    initial_length = len(ids)
    ended = False
    # Full model context limit, not a short output cap to hide repetition.
    for _ in range(448 - initial_length):
        logits = decoder.run(None, {'input_ids': np.asarray([ids], dtype=np.int64),
                                   'encoder_hidden_states': hidden})[0][0]
        logits[eos + 1:] = -np.inf
        next_token = int(np.argmax(logits))
        if next_token == eos:
            ended = True
            break
        ids.append(next_token)
    generated = ids[initial_length:]
    return {'schema_version': 1, 'engine': 'ONNX Runtime CPU',
            'purpose': 'graph_smoke_only', 'precision': 'float32',
            'benchmark_comparable': False, 'guard_applied': False,
            'decoding': 'greedy, no timestamps, no cache, no fallback',
            'text': tokenizer.decode(generated), 'token_ids': generated,
            'termination': 'eos' if ended else 'context_limit',
            'elapsed_seconds': time.perf_counter() - start}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-dir', type=Path, required=True)
    parser.add_argument('--audio', type=Path, required=True)
    args = parser.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    try:
        import soundfile as sf
        audio, rate = sf.read(args.audio, dtype='float32')
        print(json.dumps(infer(args.model_dir, audio, rate), ensure_ascii=False, allow_nan=False))
        return 0
    except Exception as exc:
        print(json.dumps({'status': 'error', 'error_type': type(exc).__name__,
                          'message': str(exc)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
