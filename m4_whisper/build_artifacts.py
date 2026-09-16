"""Maintainer utility: package only allowlisted weights/configs, never audio."""

import argparse
import json
from pathlib import Path
import shutil
import zipfile

from .runtime import sha256_file
from .settings import MODELS

CT2_SOURCES = {
    'baseline_fp32': 'zeroth_base_ct2_float32',
    'baseline_int8': 'zeroth_base_ct2_int8',
    'finetuned_fp32': 'fresh_finetuned_ct2_float32',
    'finetuned_int8': 'fresh_finetuned_ct2_int8',
}
ALLOWLIST = {'model.bin', 'config.json', 'processor_config.json',
             'preprocessor_config.json', 'tokenizer.json', 'tokenizer_config.json',
             'vocabulary.json', 'vocabulary.txt', 'encoder.onnx', 'decoder.onnx',
             'export_manifest.json', 'generation_config.json', 'special_tokens_map.json',
             'added_tokens.json', 'vocab.json', 'merges.txt', 'normalizer.json',
             'MODEL_NOTICE.md', 'UPSTREAM_LICENSE.txt', 'OPENAI_LICENSE.txt'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment-models', type=Path, required=True)
    parser.add_argument('--tokenizer', type=Path, required=True)
    parser.add_argument('--preprocessor', type=Path, required=True)
    parser.add_argument('--root', type=Path, default=Path('m4_artifacts'))
    args = parser.parse_args()
    package = Path(__file__).resolve().parent
    catalog = {'release_tag': 'm4-whisper-handoff-20260916',
               'download_base': 'https://github.com/Ldg48/rp5/releases/download/m4-whisper-handoff-20260916',
               'guard_sha256': sha256_file(package / 'guard.py'), 'artifacts': {}}
    for variant, original in CT2_SOURCES.items():
        target = args.root / 'ct2' / variant
        if target.exists():
            raise FileExistsError(f'Refusing to replace {target}')
        target.mkdir(parents=True)
        source = args.experiment_models / original
        for path in source.iterdir():
            if path.is_file() and path.name in ALLOWLIST:
                shutil.copy2(path, target / path.name)
        if sha256_file(target / 'model.bin') != MODELS[variant][1]:
            raise ValueError(f'Unexpected source model: {variant}')
        shutil.copy2(args.tokenizer, target / 'tokenizer.json')
        shutil.copy2(args.preprocessor, target / 'preprocessor_config.json')
    release = args.root / 'release'
    release.mkdir(parents=True, exist_ok=True)
    for engine, variants in [('ct2', list(CT2_SOURCES)),
                             ('onnx', ['baseline_fp32', 'finetuned_fp32'])]:
        for variant in variants:
            directory = args.root / engine / variant
            if not directory.exists():
                raise FileNotFoundError(directory)
            for name in ['MODEL_NOTICE.md', 'UPSTREAM_LICENSE.txt', 'OPENAI_LICENSE.txt']:
                shutil.copy2(package / name, directory / name)
            name = f'm4-{engine}-{variant}.zip'
            path = release / name
            if path.exists():
                raise FileExistsError(f'Refusing to replace {path}')
            files = sorted(p for p in directory.iterdir() if p.is_file() and p.name in ALLOWLIST)
            with zipfile.ZipFile(path, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
                for item in files:
                    archive.write(item, item.name)
            catalog['artifacts'][f'{engine}_{variant}'] = {
                'filename': name, 'relative_directory': f'{engine}/{variant}',
                'sha256': sha256_file(path), 'bytes': path.stat().st_size,
                'files': {p.name: {'sha256': sha256_file(p), 'bytes': p.stat().st_size} for p in files},
                'historical_ct2_metrics_apply': engine == 'ct2',
            }
            print(f'{name}: {path.stat().st_size / 1e6:.1f} MB', flush=True)
    (package / 'artifacts.json').write_text(json.dumps(catalog, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
