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
        'pyproject.toml': b'[project]\nname="swift-options-autotrader"\nversion="0.1.0"\n',
        'uv.lock': b'synthetic only',
        'deployment/windows/setup.ps1': b'# synthetic only',
        'deployment/windows/common.ps1': b'# synthetic',
        'deployment/windows/manage.ps1': b'# synthetic',
        'deployment/windows/service.ps1': b'# synthetic',
        'deployment/windows/secrets.ps1': b'# synthetic',
        'deployment/windows/healthcheck.ps1': b'# synthetic',
        'deployment/windows/activate-live.ps1': b'# synthetic',
        'deployment/windows/extract_approved_package.py': b'# synthetic',
    }
    files.update(extra or {})
    manifest = {'project': 'swift-options-autotrader', 'source_commit': COMMIT,
                'mode': 'PAPER_ONLY', 'live_enabled': False,
                'broker_writes_enabled': False, 'package_version': '0.1.0',
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
        z,target,expected_sha256=digest,expected_commit=COMMIT) == 10
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
            if name == 'pyproject.toml':
                raw = b'changed'
            out.writestr(name,raw)
    digest=hashlib.sha256(new.read_bytes()).hexdigest()
    with pytest.raises(RuntimeError,match='integrity'):
        module.extract_approved_package(new,tmp_path/'isolated',expected_sha256=digest,
                                        expected_commit=COMMIT)

def test_installer_never_copies_managed_scripts_from_unverified_launcher():
    setup = (Path(__file__).resolve().parents[2] / 'deployment/windows/setup.ps1').read_text()
    assert "Copy-Item -LiteralPath (Join-Path $source ('deployment/windows/' + $file))" in setup
    assert "Copy-Item -LiteralPath (Join-Path $PSScriptRoot $file)" not in setup
    assert "Get-FileHash -Algorithm SHA256" in setup
    assert "[System.IO.Compression.ZipFile]::OpenRead" in setup
    assert "Join-Path $PSScriptRoot 'extract_approved_package.py'" not in setup


def test_manifest_version_mismatch_denied_even_if_archive_digest_matches(tmp_path):
    z, _digest = prepared(tmp_path)
    tampered = tmp_path / 'version-wrong.zip'
    with zipfile.ZipFile(z) as old, zipfile.ZipFile(tampered, 'w') as new:
        for name in old.namelist():
            raw = old.read(name)
            if name == 'PACKAGE_MANIFEST.json':
                document = json.loads(raw)
                document['package_version'] = '999.999.999'
                raw = json.dumps(document).encode()
            new.writestr(name, raw)
    digest = hashlib.sha256(tampered.read_bytes()).hexdigest()
    with pytest.raises(RuntimeError, match='version'):
        module.extract_approved_package(tampered, tmp_path/'isolated',
                                        expected_sha256=digest, expected_commit=COMMIT)


def test_install_helper_uses_supported_net_file_open_api():
    setup = (Path(__file__).resolve().parents[2] / 'deployment/windows/setup.ps1').read_text()
    assert '[System.IO.File]::CreateNew(' not in setup
    assert '[System.IO.File]::Open(' in setup
