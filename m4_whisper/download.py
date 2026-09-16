"""Download one release artifact and verify it before installing; never overwrite."""

import argparse
import hashlib
import json
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath


def digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def artifact_url(catalog, item):
    return item.get('download_base', catalog['download_base']) + '/' + item['filename']


def install_archive(archive_path, target, expected_sha256):
    target = Path(target).resolve()
    if target.exists():
        raise FileExistsError(f'Refusing to replace {target}')
    if digest_file(archive_path) != expected_sha256:
        raise ValueError('Archive SHA-256 mismatch; nothing installed.')
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.m4-unpack-', dir=target.parent) as tmp:
        staging = Path(tmp) / 'model'
        staging.mkdir()
        with zipfile.ZipFile(archive_path) as archive:
            for entry in archive.infolist():
                path = PurePosixPath(entry.filename)
                raw_path = entry.orig_filename
                if (path.is_absolute() or '..' in path.parts or ':' in entry.filename
                        or '\\' in raw_path or '\x00' in raw_path
                        or ((entry.external_attr >> 16) & 0o170000) == 0o120000):
                    raise ValueError(f'Unsafe archive path: {entry.filename}')
            archive.extractall(staging)
        staging.rename(target)


def main():
    catalog = json.loads(Path(__file__).with_name('artifacts.json').read_text(encoding='utf-8'))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifact', choices=catalog['artifacts'], required=True)
    parser.add_argument('--root', type=Path, default=Path('m4_artifacts'))
    args = parser.parse_args()
    item = catalog['artifacts'][args.artifact]
    target = args.root / item['relative_directory']
    if target.exists():
        raise FileExistsError(f'Refusing to replace {target}')
    url = artifact_url(catalog, item)
    with tempfile.TemporaryDirectory(prefix='m4-download-') as tmp:
        archive = Path(tmp) / item['filename']
        print(f'Downloading {item["bytes"] / 1e6:.1f} MB: {args.artifact}', flush=True)
        with urllib.request.urlopen(url, timeout=120) as response, archive.open('wb') as output:
            shutil.copyfileobj(response, output)
        install_archive(archive, target, item['sha256'])
    print(f'Verified and installed: {target.resolve()}')


if __name__ == '__main__':
    main()
