"""Internal review bundles contain only audited files from one clean commit."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest

from scripts.review_bundle import ReviewBundleError, build_review_bundle


def test_standalone_bundle_exports_the_whole_audited_product_root(tmp_path: Path) -> None:
    repo = tmp_path / "standalone"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "greenpine-project-watch"\n', encoding="utf-8"
    )
    (repo / "README.md").write_text("# Synthetic ProjectWatch\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Synthetic Test", "-c", "user.email=synthetic@example.test",
         "commit", "-qm", "synthetic standalone source"], cwd=repo, check=True,
    )
    output = tmp_path / "standalone.zip"
    result = build_review_bundle(repo, output)
    assert result["file_count"] == 2
    with ZipFile(output) as archive:
        assert "project-watch/README.md" in archive.namelist()
        assert "project-watch/pyproject.toml" in archive.namelist()
        assert not any("products/project-watch" in path for path in archive.namelist())


def _synthetic_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repository"
    product = repo / "products" / "project-watch"
    product.mkdir(parents=True)
    (product / "README.md").write_text("# Synthetic Project Watch\n", encoding="utf-8")
    (product / "pyproject.toml").write_text(
        '[project]\nname = "synthetic-project-watch"\n', encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "add", "products/project-watch"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Synthetic Test",
            "-c",
            "user.email=synthetic@example.test",
            "commit",
            "-qm",
            "synthetic source",
        ],
        cwd=repo,
        check=True,
    )
    return repo, product


def test_bundle_is_deterministic_and_marked_internal_review(tmp_path: Path) -> None:
    repo, _product = _synthetic_repo(tmp_path)
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"

    first_result = build_review_bundle(repo, first)
    second_result = build_review_bundle(repo, second)

    assert first.read_bytes() == second.read_bytes()
    assert first_result == second_result
    assert first_result["file_count"] == 2
    with ZipFile(first) as archive:
        assert set(archive.namelist()) == {
            "project-watch/README.md",
            "project-watch/pyproject.toml",
            "project-watch/REVIEW_ONLY.json",
        }
        manifest = json.loads(archive.read("project-watch/REVIEW_ONLY.json"))
        assert manifest["publication_authorized"] is False
        assert manifest["source_commit"] == first_result["source_commit"]
        assert {item["path"] for item in manifest["files"]} == {
            "README.md",
            "pyproject.toml",
        }
        for item in manifest["files"]:
            payload = archive.read(f"project-watch/{item['path']}")
            assert hashlib.sha256(payload).hexdigest() == item["sha256"]
    assert hashlib.sha256(first.read_bytes()).hexdigest() == first_result["archive_sha256"]


def test_bundle_rejects_untracked_or_modified_product_files(tmp_path: Path) -> None:
    repo, product = _synthetic_repo(tmp_path)
    output = tmp_path / "review.zip"
    (product / "private-bp.pdf").write_bytes(b"%PDF-synthetic")
    with pytest.raises(ReviewBundleError, match="product worktree must be clean"):
        build_review_bundle(repo, output)
    assert not output.exists()

    (product / "private-bp.pdf").unlink()
    (product / "README.md").write_text("# Edited synthetic source\n", encoding="utf-8")
    with pytest.raises(ReviewBundleError, match="product worktree must be clean"):
        build_review_bundle(repo, output)
    assert not output.exists()


def test_bundle_rejects_tracked_binary_and_redacts_obvious_secret(tmp_path: Path) -> None:
    repo, product = _synthetic_repo(tmp_path)
    token = "ghp_" + "A" * 36
    (product / "private-bp.pdf").write_bytes(b"%PDF-synthetic")
    (product / "README.md").write_text(f"# Synthetic {token}\n", encoding="utf-8")
    subprocess.run(["git", "add", "products/project-watch"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Synthetic Test",
            "-c",
            "user.email=synthetic@example.test",
            "commit",
            "-qm",
            "synthetic unsafe source",
        ],
        cwd=repo,
        check=True,
    )
    output = tmp_path / "review.zip"

    with pytest.raises(ReviewBundleError) as error:
        build_review_bundle(repo, output)
    assert "non-text tracked artifact" in str(error.value)
    assert "github_token" in str(error.value)
    assert token not in str(error.value)
    assert not output.exists()


def test_bundle_never_overwrites_existing_output(tmp_path: Path) -> None:
    repo, _product = _synthetic_repo(tmp_path)
    output = tmp_path / "review.zip"
    output.write_bytes(b"synthetic pre-existing output")
    with pytest.raises(ReviewBundleError, match="output already exists"):
        build_review_bundle(repo, output)
    assert output.read_bytes() == b"synthetic pre-existing output"


def test_bundle_output_must_remain_outside_repository(tmp_path: Path) -> None:
    repo, _product = _synthetic_repo(tmp_path)
    output = repo / "review.zip"
    with pytest.raises(ReviewBundleError, match="outside the source repository"):
        build_review_bundle(repo, output)
    assert not output.exists()


def test_module_cli_builds_synthetic_review_bundle(tmp_path: Path) -> None:
    repo, _product = _synthetic_repo(tmp_path)
    output = tmp_path / "cli-review.zip"
    product_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.review_bundle",
            "--repo-root",
            str(repo),
            "--output",
            str(output),
        ],
        cwd=product_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert output.is_file()
    assert json.loads(result.stdout)["publication_authorized"] is False
