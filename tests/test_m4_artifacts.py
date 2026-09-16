import hashlib
import zipfile

import pytest

from m4_whisper.download import install_archive


def test_onnx_asset_can_use_the_team_release_without_changing_legacy_urls():
    from m4_whisper import download
    assert hasattr(download, 'artifact_url')
    catalog = {'download_base': 'https://github.com/Ldg48/rp5/releases/download/old'}
    legacy = {'filename': 'old.zip'}
    current = {'filename': 'new.zip', 'download_base': 'https://github.com/jinsan02/safewave-ai-ambient-monitoring/releases/download/new'}
    assert download.artifact_url(catalog, legacy).endswith('/old/old.zip')
    assert download.artifact_url(catalog, current) == current['download_base'] + '/new.zip'


def build_zip(path, names):
    with zipfile.ZipFile(path, 'w') as archive:
        for name in names:
            entry = zipfile.ZipInfo('placeholder')
            entry.filename = name
            archive.writestr(entry, b'model-test')
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_archive_is_verified_then_installed_without_overwriting(tmp_path):
    source = tmp_path / 'model.zip'
    digest = build_zip(source, ['model.bin', 'config.json'])
    target = tmp_path / 'installed'
    install_archive(source, target, digest)
    assert (target / 'model.bin').read_bytes() == b'model-test'
    with pytest.raises(FileExistsError):
        install_archive(source, target, digest)


def test_bad_checksum_never_installs_model(tmp_path):
    source = tmp_path / 'model.zip'
    build_zip(source, ['model.bin'])
    with pytest.raises(ValueError, match='SHA-256'):
        install_archive(source, tmp_path / 'target', '0' * 64)
    assert not (tmp_path / 'target').exists()


@pytest.mark.parametrize('name', ['../outside', '/absolute', 'C:/drive', 'a\\escape'])
def test_archive_path_escape_is_rejected(tmp_path, name):
    source = tmp_path / 'model.zip'
    digest = build_zip(source, [name])
    with pytest.raises(ValueError, match='path'):
        install_archive(source, tmp_path / 'target', digest)
    assert not (tmp_path / 'target').exists()
