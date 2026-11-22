"""SEC-006: the uv bootstrap is hash-pinned and every installer copy agrees."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REQ = ROOT / "requirements" / "uv-bootstrap.txt"
SETUP = ROOT / "deployment" / "windows" / "setup.ps1"
WORKFLOWS = [ROOT / ".github" / "workflows" / "ci.yml",
             ROOT / ".github" / "workflows" / "windows-deployment.yml"]
WIN_AMD64_SHA256 = "edd469425cd62bcd8c8cc0226c5f9043a94e37ed869da8268c80fdbfd3e5015e"


def _hashes(text: str) -> list[str]:
    return re.findall(r"--hash=sha256:([0-9a-f]{64})", text)


def test_requirements_pin_uv_0_10_0_with_known_artifact_hashes() -> None:
    text = REQ.read_text(encoding="ascii")
    assert re.search(r"^uv==0\.10\.0 \\$", text, re.MULTILINE)
    hashes = _hashes(text)
    assert len(hashes) >= 18
    assert len(set(hashes)) == len(hashes)
    assert WIN_AMD64_SHA256 in hashes


def test_setup_ps1_embeds_identical_hash_set_and_requires_hashes() -> None:
    req_hashes = set(_hashes(REQ.read_text(encoding="ascii")))
    setup = SETUP.read_text(encoding="utf-8")
    assert "--require-hashes" in setup and "--only-binary=:all:" in setup
    assert "'uv==0.10.0'" not in setup
    assert set(_hashes(setup)) == req_hashes


def test_ci_workflows_install_uv_only_through_hashed_requirements() -> None:
    for wf in WORKFLOWS:
        text = wf.read_text(encoding="utf-8")
        assert "uv==0.10.0" not in text, wf.name
        assert ("--require-hashes --only-binary=:all: -r requirements/uv-bootstrap.txt"
                in text), wf.name
