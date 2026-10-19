"""Synthetic attribution fixtures; no providers, product home or network."""
import hashlib
import json
from pathlib import Path

import pytest

from scripts.frontend_notices import NoticeError, check_outputs, render


@pytest.fixture
def source(tmp_path):
    frontend = tmp_path / "frontend"
    bits = frontend / "src/bits"
    bits.mkdir(parents=True)
    (bits / "Wave.tsx").write_text("// synthetic component\n")
    (bits / "LICENSE-react-bits.md").write_text("Synthetic permission for Wave\n")
    fonts = frontend / "src/fonts"
    fonts.mkdir()
    (fonts / "OFL-test.txt").write_text("Synthetic font permission\n")
    module = frontend / "node_modules/example"
    module.mkdir(parents=True)
    (module / "package.json").write_text(json.dumps({"name": "example", "version": "1.0"}))
    (module / "LICENSE").write_text("Synthetic package permission\n")
    (module / "CopyrightNotice.txt").write_text("Synthetic copyright\n")
    (frontend / "package-lock.json").write_text(json.dumps({"packages": {
        "": {}, "node_modules/example": {"version": "1.0", "license": "MIT"},
        "node_modules/build-only": {"version": "9.0", "dev": True}}}))
    return tmp_path


def test_runtime_closure_and_copyright_are_included(source):
    text = render(source)
    assert "example@1.0" in text
    assert "Synthetic copyright" in text
    assert "Synthetic package permission" in text
    assert "Synthetic font permission" in text
    assert "build-only" not in text


def test_missing_component_attribution_fails(source):
    (source / "frontend/src/bits/New.tsx").write_text("// synthetic\n")
    with pytest.raises(NoticeError, match="New"):
        render(source)


def test_installed_version_drift_fails(source):
    (source / "frontend/node_modules/example/package.json").write_text(
        '{"name":"example","version":"2.0"}')
    with pytest.raises(NoticeError, match="version"):
        render(source)


def test_missing_license_is_not_silently_accepted(source):
    (source / "frontend/node_modules/example/LICENSE").unlink()
    with pytest.raises(NoticeError, match="license"):
        render(source)


def test_outputs_must_match_expected(source):
    text = render(source)
    assert len(check_outputs(source, text)) == 2


def test_ogl_requires_verified_readme(source, monkeypatch):
    module = source / "frontend/node_modules/example"
    (module / "package.json").write_text('{"name":"ogl","version":"1.0.11"}')
    (module / "LICENSE").unlink()
    lock = source / "frontend/package-lock.json"
    data = json.loads(lock.read_text())
    data["packages"]["node_modules/example"] = {"version": "1.0.11", "license": "Unlicense"}
    lock.write_text(json.dumps(data))
    with pytest.raises(NoticeError, match="license README"):
        render(source)
    raw = b"Synthetic README\n## Unlicense\nSynthetic complete license\n"
    (module / "README.md").write_bytes(raw)
    with pytest.raises(NoticeError, match="license README"):
        render(source)
    monkeypatch.setattr("scripts.frontend_notices.OGL_README_SHA256", hashlib.sha256(raw).hexdigest())
    assert "Synthetic complete license" in render(source)
    data["packages"]["node_modules/example"]["license"] = "MIT"
    lock.write_text(json.dumps(data))
    with pytest.raises(NoticeError, match="license"):
        render(source)


def test_license_british_spelling(source):
    module = source / "frontend/node_modules/example"
    (module / "LICENSE").rename(module / "LICENCE")
    assert "Synthetic package permission" in render(source)


def test_repository_notices_are_current():
    root = Path(__file__).resolve().parents[1]
    if not (root / "frontend/node_modules").is_dir():
        pytest.skip("npm ci required for local attribution check")
    assert check_outputs(root, render(root)) == []
