"""A synthetic-only artifact gets a precise, non-release receipt."""

import hashlib
import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from scripts.build_manifest import build_manifest, write_manifest


def _synthetic_wheel(path: Path) -> None:
    with ZipFile(path, "w") as archive:
        archive.writestr("project_watch/api.py", "# synthetic module\n")
        archive.writestr("project_watch/static/upload_batch.mjs", "// synthetic module\n")
        archive.writestr("greenpine_project_watch-0.1.dist-info/METADATA", "Name: synthetic\n")


def test_manifest_records_exact_wheel_and_rejects_publication_claim(tmp_path: Path) -> None:
    wheel = tmp_path / "greenpine_project_watch-0.1.0a0-py3-none-any.whl"
    _synthetic_wheel(wheel)
    commit = "a" * 40

    result = build_manifest(wheel, commit)
    assert result == {
        "schema_version": 1,
        "commit": commit,
        "wheel": wheel.name,
        "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "size_bytes": wheel.stat().st_size,
        "scope": "synthetic_only",
        "publication_authorized": False,
    }
    target = tmp_path / "manifest.json"
    write_manifest(wheel, commit, target)
    assert json.loads(target.read_text(encoding="utf-8")) == result


@pytest.mark.parametrize("name", ["missing.whl", "source.zip"])
def test_manifest_refuses_missing_or_non_wheel_file(tmp_path: Path, name: str) -> None:
    path = tmp_path / name
    if name.endswith(".zip"):
        path.write_bytes(b"synthetic")
    with pytest.raises(ValueError):
        build_manifest(path, "a" * 40)


def test_manifest_refuses_ambiguous_commit(tmp_path: Path) -> None:
    wheel = tmp_path / "synthetic.whl"
    _synthetic_wheel(wheel)
    with pytest.raises(ValueError):
        build_manifest(wheel, "main")


def test_manifest_refuses_private_material_in_wheel(tmp_path: Path) -> None:
    wheel = tmp_path / "synthetic.whl"
    _synthetic_wheel(wheel)
    with ZipFile(wheel, "a") as archive:
        archive.writestr("project_watch/private-bp.pdf", b"%PDF-synthetic")
    with pytest.raises(ValueError, match="wheel inventory rejected"):
        build_manifest(wheel, "a" * 40)
