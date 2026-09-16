"""Package one verified ONNX INT8 model, without audio or training checkpoints."""

import argparse
import json
from pathlib import Path
import zipfile

from .runtime import sha256_file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-dir', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    manifest_path = args.model_dir / 'onnx_manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    names = sorted(manifest['files']) + ['onnx_manifest.json']
    for name in names:
        if Path(name).name != name or Path(name).suffix not in {'.onnx', '.json', '.txt', '.md'}:
            raise ValueError(f'Unexpected artifact member: {name}')
        if name != 'onnx_manifest.json' and sha256_file(args.model_dir / name) != manifest['files'][name]['sha256']:
            raise ValueError(f'Changed asset: {name}')
    args.output_dir.mkdir(parents=True, exist_ok=True)
    destination = args.output_dir / 'm4-onnx-finetuned-int8.zip'
    with zipfile.ZipFile(destination, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name in names:
            archive.write(args.model_dir / name, arcname=name)
        archive.writestr('ONNX_INT8_NOTICE.txt',
                         'One fresh Zeroth-derived fine-tuned model, dynamic INT8 ONNX.\n'
                         'Three ONNX graphs are components, not three separate trained models.\n'
                         'Run with m4_whisper/onnx_runtime.py from the team handoff branch.\n'
                         'CT2 benchmark metrics do not apply. Silence hallucination remains.\n'
                         'Not approved for automatic emergency decisions or deployment.\n')
    with zipfile.ZipFile(destination) as archive:
        bad = archive.testzip()
        if bad:
            raise ValueError(f'Archive CRC failure: {bad}')
    catalog_path = Path(__file__).with_name('artifacts.json')
    catalog = json.loads(catalog_path.read_text(encoding='utf-8'))
    item = {
        'filename': destination.name, 'relative_directory': 'onnx/finetuned_int8',
        'sha256': sha256_file(destination), 'bytes': destination.stat().st_size,
        'download_base': 'https://github.com/jinsan02/safewave-ai-ambient-monitoring/releases/download/m4-onnx-int8-20260916',
        'historical_ct2_metrics_apply': False, 'source_weight_sha256': manifest['source_weight_sha256'],
        'files': {name: {'sha256': sha256_file(args.model_dir / name),
                         'bytes': (args.model_dir / name).stat().st_size} for name in names},
    }
    catalog['recommended_artifact'] = 'onnx_finetuned_int8'
    catalog['artifacts']['onnx_finetuned_int8'] = item
    catalog_path.write_text(json.dumps(catalog, indent=2), encoding='utf-8')
    print(json.dumps({'path': str(destination), 'bytes': item['bytes'], 'sha256': item['sha256']}), flush=True)


if __name__ == '__main__':
    main()
