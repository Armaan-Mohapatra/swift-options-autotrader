"""Cross-platform release ZIP verification without touching a Windows host."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[2] / 'deployment/windows/extract_approved_package.py'
spec = importlib.util.spec_from_file_location('release_extract', MODULE_PATH)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(module)
COMMIT = 'a' * 40


def prepared(tmp_path, *, extra=None):
    files = {
        'pyproject.toml': b'synthetic only',
        'uv.lock': b'synthetic only',
        'deployment/windows/setup.ps1': b'# synthetic only',
    }
    files.update(extra or {})
    manifest = {'project': 'swift-options-autotrader', 'source_commit': COMMIT,
                'mode': 'PAPER_ONLY', 'live_enabled': False,
                'broker_writes_enabled': False,
                'files': {k: hashlib.sha256(v).hexdigest() for k,v in files.items()}}
    z = tmp_path / 'release.zip'
    with zipfile.ZipFile(z,'w') as archive:
        for name,raw in files.items():
            archive.writestr(name,raw)
        archive.writestr('PACKAGE_MANIFEST.json',json.dumps(manifest))
    return z, hashlib.sha256(z.read_bytes()).hexdigest()


def test_archive_verified_before_files_are_written(tmp_path):
    z,digest=prepared(tmp_path)
    target=tmp_path / 'isolated'
    assert module.extract_approved_package(
        z,target,expected_sha256=digest,expected_commit=COMMIT) == 3
    assert (target/'deployment/windows/setup.ps1').read_bytes()==b'# synthetic only'
    assert not (target/'.env').exists()


def test_different_zip_checksum_fails_without_writing(tmp_path):
    z,digest=prepared(tmp_path)
    target=tmp_path/'isolated'
    with pytest.raises(RuntimeError, match='digest'):
        module.extract_approved_package(
            z,target,expected_sha256='0'*64,expected_commit=COMMIT)
    assert not target.exists()


def test_different_release_commit_fails_without_writing(tmp_path):
    z,digest=prepared(tmp_path)
    target=tmp_path/'isolated'
    with pytest.raises(RuntimeError, match='commit'):
        module.extract_approved_package(
            z,target,expected_sha256=digest,expected_commit='b'*40)
    assert not target.exists()


@pytest.mark.parametrize('unsafe',[
    {'../outside':b'bad'}, {'dir\\outside':b'bad'},
    {'C:/other':b'bad'},
])
def test_path_traversal_or_platform_redirect_fails_before_write(tmp_path,unsafe):
    z,digest=prepared(tmp_path,extra=unsafe)
    target=tmp_path/'isolated'
    with pytest.raises(RuntimeError):
        module.extract_approved_package(z,target,expected_sha256=digest,expected_commit=COMMIT)
    assert not target.exists()


def test_package_rejects_duplicate_names_even_when_signed(tmp_path):
    z,digest=prepared(tmp_path)
    with zipfile.ZipFile(z,'a') as archive:
        archive.writestr('pyproject.toml',b'duplicate')
    digest=hashlib.sha256(z.read_bytes()).hexdigest()
    target=tmp_path/'isolated'
    with pytest.raises(RuntimeError,match='duplicate'):
        module.extract_approved_package(z,target,expected_sha256=digest,expected_commit=COMMIT)
    assert not target.exists()


def test_package_rejects_change_to_manifested_file_even_after_outer_digest_recomputed(tmp_path):
    z,digest=prepared(tmp_path)
    new=tmp_path/'tampered.zip'
    with zipfile.ZipFile(z) as archive, zipfile.ZipFile(new,'w') as out:
        for name in archive.namelist():
            raw=archive.read(name)
            if name=='pyproject.toml':raw=b'changed'
            out.writestr(name,raw)
    digest=hashlib.sha256(new.read_bytes()).hexdigest()
    with pytest.raises(RuntimeError,match='integrity'):
        module.extract_approved_package(new,tmp_path/'isolated',expected_sha256=digest,
                                        expected_commit=COMMIT)
