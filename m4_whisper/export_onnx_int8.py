"""Export the exact fresh HF checkpoint as standard Optimum ONNX, then INT8."""

import argparse
from collections import Counter
import gc
import importlib.metadata
import json
from pathlib import Path
import shutil

from .runtime import sha256_file

SOURCE_SHA256 = 'cebed9f4c930309e28a79537f6dcb0454fb22564b21fa110173e00e8ab6fae77'
GRAPH_NAMES = ('encoder_model.onnx', 'decoder_model.onnx', 'decoder_with_past_model.onnx')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(f'Refusing to overwrite {args.output}')
    weight = args.source / 'model.safetensors'
    if sha256_file(weight) != SOURCE_SHA256:
        raise ValueError('Wrong source checkpoint: expected fresh 20260915 fine-tune.')
    import torch
    import onnx
    from onnxruntime.quantization import quantize_dynamic, QuantType
    from transformers import (WhisperForConditionalGeneration, WhisperTokenizerFast,
                              WhisperFeatureExtractor, WhisperProcessor)
    from optimum.exporters.onnx.convert import onnx_export_from_model

    torch.set_num_threads(6)
    fp32 = args.output / 'fp32'
    int8 = args.output / 'int8'
    int8.mkdir(parents=True)
    # v5 stores these fields differently. Adapt metadata only, preserving token IDs.
    tokenizer_config = json.loads((args.source / 'tokenizer_config.json').read_text(encoding='utf-8'))
    tokenizer = WhisperTokenizerFast.from_pretrained(
        args.source, local_files_only=True, extra_special_tokens={},
        additional_special_tokens=tokenizer_config.get('extra_special_tokens', []))
    from tokenizers import Tokenizer
    original_tokenizer = Tokenizer.from_file(str(args.source / 'tokenizer.json'))
    assert tokenizer.get_vocab() == original_tokenizer.get_vocab()
    feature_dict = json.loads((args.source / 'processor_config.json').read_text(encoding='utf-8'))['feature_extractor']
    processor = WhisperProcessor(WhisperFeatureExtractor.from_dict(feature_dict), tokenizer)
    model = WhisperForConditionalGeneration.from_pretrained(
        args.source, local_files_only=True, torch_dtype=torch.float32,
        attn_implementation='eager').eval()
    model.config.use_cache = True
    model.generation_config.use_cache = True
    print('Exporting standard encoder/decoder/cache ONNX graphs on CPU.', flush=True)
    onnx_export_from_model(
        model, output=fp32, task='automatic-speech-recognition-with-past',
        opset=17, device='cpu', no_post_process=True, do_validation=True,
        preprocessors=[processor], batch_size=1, sequence_length=4)
    processor.save_pretrained(fp32)
    model.config.save_pretrained(fp32)
    model.generation_config.save_pretrained(fp32)
    del model
    gc.collect()
    graphs = {}
    for name in GRAPH_NAMES:
        print(f'Dynamic INT8 constant MatMul weights: {name}', flush=True)
        quantize_dynamic(str(fp32 / name), str(int8 / name), weight_type=QuantType.QInt8,
                         per_channel=True, reduce_range=False,
                         op_types_to_quantize=['MatMul'],
                         extra_options={'MatMulConstBOnly': True})
        onnx.checker.check_model(str(int8 / name))
        graph = onnx.load(str(int8 / name))
        counts = Counter(node.op_type for node in graph.graph.node)
        int8_count = sum(t.data_type == onnx.TensorProto.INT8 for t in graph.graph.initializer)
        if counts['MatMulInteger'] == 0 or int8_count == 0:
            raise ValueError(f'Quantization produced no INT8 computation: {name}')
        graphs[name] = {'sha256': sha256_file(int8 / name), 'bytes': (int8 / name).stat().st_size,
                        'fp32_bytes': (fp32 / name).stat().st_size,
                        'int8_initializer_count': int8_count, 'operators': dict(counts)}
        del graph
        gc.collect()
    for path in fp32.iterdir():
        if path.suffix in {'.json', '.txt'}:
            shutil.copy2(path, int8 / path.name)
    for name in ['MODEL_NOTICE.md', 'UPSTREAM_LICENSE.txt', 'OPENAI_LICENSE.txt']:
        shutil.copy2(Path(__file__).parent / name, int8 / name)
    manifest = {
        'schema_version': 1, 'model_id': 'fresh_zeroth_finetuned_20260915_onnx_int8',
        'source_weight_sha256': SOURCE_SHA256, 'format': 'onnx',
        'quantization': 'dynamic QInt8 per-channel constant MatMul weights; other ops float32',
        'engine': 'ONNX Runtime CPU / Optimum', 'opset': 17, 'use_cache': True,
        'ct2_benchmark_applies': False, 'guard_in_graph': False,
        'metadata_adaptation': 'v5 feature_extractor to preprocessor_config; extra_special_tokens to additional_special_tokens; identical token vocabulary',
        'graphs': graphs,
        'versions': {name: importlib.metadata.version(name) for name in
                     ['torch', 'transformers', 'optimum', 'optimum-onnx', 'onnx', 'onnxruntime']},
        'files': {p.name: {'sha256': sha256_file(p), 'bytes': p.stat().st_size}
                  for p in int8.iterdir() if p.is_file()},
    }
    (int8 / 'onnx_manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(json.dumps({'output': str(int8), 'graphs': graphs}), flush=True)


if __name__ == '__main__':
    main()
