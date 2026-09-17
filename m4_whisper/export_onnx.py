"""Export original HF weights, never renamed CT2 weights, to FP32 ONNX graphs."""

import argparse
import importlib.metadata
import json
from pathlib import Path

import numpy as np
import torch

from .runtime import sha256_file


class EncoderGraph(torch.nn.Module):
    def __init__(self, model):
        super().__init__()
        self.encoder = model.model.encoder

    def forward(self, input_features):
        return self.encoder(input_features).last_hidden_state


class DecoderGraph(torch.nn.Module):
    def __init__(self, model):
        super().__init__()
        self.decoder = model.model.decoder
        self.projection = model.proj_out

    def forward(self, input_ids, encoder_hidden_states):
        length = input_ids.shape[1]
        # Explicit additive causal mask avoids tracing a Python mask-size branch.
        mask = torch.triu(torch.full((length, length), float('-inf'),
                                    dtype=encoder_hidden_states.dtype,
                                    device=input_ids.device), diagonal=1)[None, None]
        decoded = self.decoder(input_ids=input_ids, attention_mask=mask,
                               encoder_hidden_states=encoder_hidden_states,
                               use_cache=False).last_hidden_state
        return self.projection(decoded[:, -1, :])


def export_graphs(model, destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    model.eval()
    config = model.config
    features = torch.zeros(1, config.num_mel_bins, config.max_source_positions * 2)
    ids = torch.tensor([[config.decoder_start_token_id] * 4], dtype=torch.int64)
    encoder, decoder = EncoderGraph(model).eval(), DecoderGraph(model).eval()
    with torch.no_grad():
        hidden = encoder(features)
        common = dict(opset_version=17, dynamo=False,
                      export_params=True, do_constant_folding=True)
        torch.onnx.export(encoder, (features,), str(destination / 'encoder.onnx'),
                          input_names=['input_features'],
                          output_names=['encoder_hidden_states'], **common)
        torch.onnx.export(decoder, (ids, hidden), str(destination / 'decoder.onnx'),
                          input_names=['input_ids', 'encoder_hidden_states'],
                          output_names=['next_token_logits'],
                          dynamic_axes={'input_ids': {1: 'prefix_length'}}, **common)


def validate_graphs(model, destination, features):
    import onnx
    import onnxruntime as ort
    destination = Path(destination)
    options = ort.SessionOptions()
    options.intra_op_num_threads = 6
    options.inter_op_num_threads = 1
    sessions = {}
    for name in ['encoder', 'decoder']:
        path = destination / f'{name}.onnx'
        onnx.checker.check_model(str(path), full_check=True)
        sessions[name] = ort.InferenceSession(str(path), sess_options=options,
                                              providers=['CPUExecutionProvider'])
    with torch.no_grad():
        expected_hidden = EncoderGraph(model)(torch.from_numpy(features)).numpy()
    hidden = sessions['encoder'].run(None, {'input_features': features})[0]
    np.testing.assert_allclose(hidden, expected_hidden, atol=5e-4, rtol=5e-3)
    report = {'encoder_max_absolute_error': float(np.max(np.abs(hidden - expected_hidden))),
              'decoder_checks': [], 'providers': ['CPUExecutionProvider']}
    # Natural token prefix, not a shortened generation. No generation/ASR score claim.
    prefix = [50258, 50264, 50359, 50363]
    for length in [4, 8, 16]:
        ids = np.asarray([prefix + [220] * (length - 4)], dtype=np.int64)
        with torch.no_grad():
            expected = DecoderGraph(model)(torch.from_numpy(ids),
                                           torch.from_numpy(hidden)).numpy()
        actual = sessions['decoder'].run(None, {'input_ids': ids,
                                               'encoder_hidden_states': hidden})[0]
        np.testing.assert_allclose(actual, expected, atol=5e-4, rtol=5e-3)
        report['decoder_checks'].append({
            'prefix_length': length, 'max_absolute_error': float(np.max(np.abs(actual - expected))),
            'argmax_equal': bool(np.array_equal(actual.argmax(-1), expected.argmax(-1))),
        })
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--audio', type=Path, required=True,
                        help='Local validation WAV only; audio is not copied into artifacts.')
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(f'Refusing to overwrite {args.output}')
    from transformers import WhisperForConditionalGeneration, WhisperProcessor
    import soundfile as sf
    from .runtime import validate_audio
    torch.set_num_threads(6)
    print('Loading source checkpoint on CPU for export.', flush=True)
    model = WhisperForConditionalGeneration.from_pretrained(
        str(args.source), local_files_only=True, dtype=torch.float32,
        attn_implementation='eager').eval()
    processor = WhisperProcessor.from_pretrained(str(args.source), local_files_only=True)
    waveform, sample_rate = sf.read(args.audio, dtype='float32')
    waveform = validate_audio(waveform, sample_rate)
    features = processor.feature_extractor(waveform, sampling_rate=16000,
                                           return_tensors='np').input_features
    print('Exporting encoder and dynamic-prefix decoder.', flush=True)
    export_graphs(model, args.output)
    print('Checking ONNX graphs and CPU numerical parity.', flush=True)
    validation = validate_graphs(model, args.output, features)
    processor.save_pretrained(args.output)
    model.config.save_pretrained(args.output)
    model.generation_config.save_pretrained(args.output)
    processor.tokenizer.backend_tokenizer.save(str(args.output / 'tokenizer.json'))
    weight = next(p for p in [args.source / 'model.safetensors', args.source / 'pytorch_model.bin'] if p.exists())
    manifest = {
        'format': 'onnx', 'precision': 'float32', 'opset': 17,
        'source_weight_sha256': sha256_file(weight),
        'source_weight_name': weight.name,
        'source_kind': 'HF checkpoint (not CT2 INT8 conversion)',
        'guard_in_graph': False, 'ct2_benchmark_applies': False,
        'cache': False, 'batch_size': 1,
        'encoder_input': {'name': 'input_features', 'dtype': 'float32', 'shape': [1, 80, 3000]},
        'encoder_output': {'shape': [1, 1500, 768]},
        'decoder_inputs': {'input_ids': 'int64 [1, prefix_length], 1..448',
                           'encoder_hidden_states': 'float32 [1, 1500, 768]'},
        'decoder_output': {'name': 'next_token_logits', 'shape': [1, 51865]},
        'validation_audio_sha256': sha256_file(args.audio),
        'validation': validation,
        'versions': {name: importlib.metadata.version(name)
                     for name in ['torch', 'transformers', 'onnx', 'onnxruntime']},
        'files': {p.name: {'sha256': sha256_file(p), 'bytes': p.stat().st_size}
                  for p in args.output.iterdir() if p.is_file()},
    }
    (args.output / 'export_manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(json.dumps(validation), flush=True)


if __name__ == '__main__':
    main()
