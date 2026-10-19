"""Build a deterministic, internal-only source bundle from a clean Git commit.

This is a handoff for rights and security review, not a publication approval.
The bundle never includes the rest of the Greenpine repository.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from scripts.release_audit import (
    _MAX_MEMBER_BYTES,
    _SOURCE_BASENAMES,
    _SOURCE_SUFFIXES,
    _secret_findings,
    audit_tracked_source,
    product_scope,
)

_ARCHIVE_ROOT = "project-watch/"
_MAX_BUNDLE_BYTES = 50 * 1024 * 1024


class ReviewBundleError(ValueError):
    """A source bundle cannot be safely prepared."""


def _git(repo_root: Path, *arguments: str) -> bytes:
    result = subprocess.run(["git", *arguments], cwd=repo_root, capture_output=True, check=False)
    if result.returncode:
        raise ReviewBundleError(f"Git command failed: {arguments[0]}")
    return result.stdout


def build_review_bundle(repo_root: Path, output: Path) -> dict[str, object]:
    """Export reviewed product source from HEAD, rejecting dirty or unsafe input."""
    repo_root = repo_root.resolve(strict=True)
    output = output.resolve()
    if output.suffix.lower() != ".zip":
        raise ReviewBundleError("output must be a .zip file")
    if output.exists():
        raise ReviewBundleError("output already exists")
    if not output.parent.is_dir():
        raise ReviewBundleError("output parent directory does not exist")
    if output.is_relative_to(repo_root):
        raise ReviewBundleError("output must be outside the source repository")
    top_level = Path(_git(repo_root, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    scope = product_scope(repo_root)
    if top_level != repo_root or not (repo_root / scope).is_dir():
        raise ReviewBundleError("repo-root must be the Git root containing Project Watch")
    if _git(repo_root, "status", "--porcelain", "-z", "--", scope):
        raise ReviewBundleError("product worktree must be clean, including untracked files")

    tracked_count, findings = audit_tracked_source(repo_root)
    if findings:
        raise ReviewBundleError("source audit failed: " + "; ".join(findings))
    if tracked_count == 0:
        raise ReviewBundleError("no tracked product source")
    commit = _git(repo_root, "rev-parse", "HEAD").decode("ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise ReviewBundleError("invalid source commit")

    archived = _git(
        repo_root,
        "archive",
        "--format=zip",
        f"--prefix={_ARCHIVE_ROOT}",
        commit if scope == "." else f"{commit}:{scope}",
    )
    if len(archived) > _MAX_BUNDLE_BYTES:
        raise ReviewBundleError("source archive exceeds size limit")

    expected_paths = {
        item.decode("utf-8").removeprefix(f"{scope}/")
        for item in _git(repo_root, "ls-files", "-z", "--", scope).split(b"\0")
        if item
    }
    if "REVIEW_ONLY.json" in expected_paths:
        raise ReviewBundleError("reserved review manifest path is tracked")
    records: list[dict[str, str]] = []
    source_files: list[tuple[str, bytes]] = []
    seen: set[str] = set()
    with ZipFile(io.BytesIO(archived), "r") as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            name = member.filename
            if not name.startswith(_ARCHIVE_ROOT):
                raise ReviewBundleError("source archive contains a path outside Project Watch")
            relative = name.removeprefix(_ARCHIVE_ROOT)
            parts = PurePosixPath(relative).parts
            if (
                not relative
                or relative.startswith("/")
                or ".." in parts
                or "\\" in relative
                or relative in seen
            ):
                raise ReviewBundleError("source archive contains an unsafe or duplicate path")
            seen.add(relative)
            if relative not in expected_paths:
                raise ReviewBundleError("source archive contains an untracked file")
            path = PurePosixPath(relative)
            if path.suffix.lower() not in _SOURCE_SUFFIXES and path.name not in _SOURCE_BASENAMES:
                raise ReviewBundleError(f"{relative}: non-text tracked artifact")
            if member.file_size > _MAX_MEMBER_BYTES:
                raise ReviewBundleError(f"{relative}: tracked file too large")
            data = archive.read(member)
            secret_findings = _secret_findings(data, relative)
            if secret_findings:
                raise ReviewBundleError(
                    "source archive audit failed: " + "; ".join(secret_findings)
                )
            records.append({"path": relative, "sha256": hashlib.sha256(data).hexdigest()})
            source_files.append((relative, data))
    if seen != expected_paths:
        raise ReviewBundleError("source archive does not match tracked product files")

    manifest = {
        "kind": "internal_source_review_bundle",
        "publication_authorized": False,
        "rights_review_required": True,
        "source_commit": commit,
        "files": sorted(records, key=lambda item: item["path"]),
    }
    archive_buffer = io.BytesIO()
    with ZipFile(archive_buffer, "w") as archive:
        for relative, data in sorted(source_files):
            info = ZipInfo(f"{_ARCHIVE_ROOT}{relative}", (1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
        manifest_info = ZipInfo(f"{_ARCHIVE_ROOT}REVIEW_ONLY.json", (1980, 1, 1, 0, 0, 0))
        manifest_info.compress_type = ZIP_DEFLATED
        manifest_info.external_attr = 0o100644 << 16
        archive.writestr(
            manifest_info, json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        )
    bundle = archive_buffer.getvalue()
    if len(bundle) > _MAX_BUNDLE_BYTES:
        raise ReviewBundleError("review bundle exceeds size limit")
    try:
        with output.open("xb") as destination:
            destination.write(bundle)
    except FileExistsError as error:
        raise ReviewBundleError("output already exists") from error
    return {
        "source_commit": commit,
        "file_count": len(records),
        "archive_sha256": hashlib.sha256(bundle).hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    try:
        result = build_review_bundle(arguments.repo_root, arguments.output)
    except (ReviewBundleError, OSError) as error:
        print(f"review bundle rejected: {error}", file=sys.stderr)
        return 2
    print(json.dumps({**result, "publication_authorized": False}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
