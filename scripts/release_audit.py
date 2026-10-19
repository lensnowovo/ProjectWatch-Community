"""Offline, conservative release inventory; never authorizes publication.

Run from the repository with a built wheel. Findings are paths and categories,
never matching secret values. License metadata needs human rights review.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import re
import subprocess
import sys
import tomllib
import zipfile
from collections import deque
from pathlib import Path, PurePosixPath

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

_TEXT_SUFFIXES = {
    ".py", ".html", ".css", ".js", ".mjs", ".ts", ".tsx", ".cmd", ".sql", ".md", ".txt", ".toml", ".svg",
    ".yml", ".yaml", ".ps1",
}
_SOURCE_SUFFIXES = _TEXT_SUFFIXES | {".json"}
_SOURCE_BASENAMES = {
    ".dockerignore", ".gitignore", ".gitattributes", "Dockerfile.parser", "requirements-parser.lock",
    "LICENSE", "NOTICE",
}
_SECRET_PATTERNS = {
    "private_key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "aws_key": re.compile(rb"AKIA[0-9A-Z]{16}"),
    "github_token": re.compile(rb"ghp_[A-Za-z0-9]{36}"),
    "api_key": re.compile(rb"sk-[A-Za-z0-9_-]{20,}"),
}
_MAX_MEMBER_BYTES = 20 * 1024 * 1024
_STUDIO_FONT_ASSETS = {
    "project_watch/web/assets/newsreader-latin-wght-normal.woff2",
    "project_watch/web/assets/noto-serif-sc-chinese-simplified-600-normal.woff2",
}
_STUDIO_LICENSE_ASSET = "project_watch/web/assets/THIRD-PARTY-LICENSES.txt"


def _secret_findings(data: bytes, label: str) -> list[str]:
    return [
        f"{label}: {kind}" for kind, pattern in _SECRET_PATTERNS.items() if pattern.search(data)
    ]


def audit_wheel(path: Path) -> tuple[int, list[str]]:
    """Reject private artifacts, unexpected files and obvious credential shapes."""
    findings: list[str] = []
    count = 0
    with zipfile.ZipFile(path) as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            count += 1
            name = member.filename
            parts = PurePosixPath(name).parts
            if name.startswith("/") or ".." in parts or "\\" in name:
                findings.append(f"{name}: unsafe wheel path")
                continue
            suffix = PurePosixPath(name).suffix.lower()
            package_file = name.startswith("project_watch/") and suffix in {
                ".py",
                ".html",
                ".css",
                ".js",
                ".mjs",
                ".sql",
                ".svg",
            }
            metadata_file = ".dist-info/" in name and (
                PurePosixPath(name).name in {"METADATA", "WHEEL", "RECORD", "top_level.txt"}
                or "/licenses/" in name
            )
            studio_asset = name in _STUDIO_FONT_ASSETS or name == _STUDIO_LICENSE_ASSET
            if not package_file and not metadata_file and not studio_asset:
                findings.append(f"{name}: unexpected wheel member")
                continue
            if member.file_size > _MAX_MEMBER_BYTES:
                findings.append(f"{name}: member too large")
                continue
            if name in _STUDIO_FONT_ASSETS:
                with archive.open(member) as font:
                    if font.read(4) != b"wOF2":
                        findings.append(f"{name}: invalid WOFF2 header")
            if suffix in _TEXT_SUFFIXES or PurePosixPath(name).name in _SOURCE_BASENAMES | {"METADATA"}:
                findings.extend(_secret_findings(archive.read(member), name))
    return count, findings


def product_scope(repo_root: Path) -> str:
    """Recognize the independent product without treating arbitrary repos as it."""
    manifest = repo_root / "pyproject.toml"
    if manifest.is_file():
        try:
            metadata = tomllib.loads(manifest.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError):
            metadata = {}
        if metadata.get("project", {}).get("name") in {"projectwatch-local", "greenpine-project-watch"}:
            return "."
    return "products/project-watch"


def _public_studio_fixture(relative: str, data: bytes) -> bool:
    """Narrow exception for opt-in, trimmed public Studio API replay metadata.

    These snapshots are real, so labelling them synthetic would be misleading.
    Other fixtures retain the synthetic-only rule; all files still undergo the
    credential scan. Reapplying the probe's allowlist rejects extra response
    fields, headers, body text, and oversized result sets.
    """
    if "/tests/fixtures/studio_live/" not in "/" + relative.replace("\\", "/"):
        return False
    try:
        from project_watch.studio.http import Response
        from scripts.studio_live_probe import PUBLIC_ENDPOINTS, trim_response
    except ImportError as error:
        # An unimportable probe says nothing about the fixture. Swallowing this
        # once reported every live fixture as unmarked, so fail loudly instead.
        raise RuntimeError(
            "release audit cannot check Studio live fixtures: the repository root and the "
            "installed project_watch package must both be importable"
        ) from error
    try:
        fixture = json.loads(data.decode("utf-8"))
        if set(fixture) != {"provenance", "response"}:
            return False
        meta = fixture["provenance"]
        if set(meta) != {"kind", "captured_at", "endpoint", "company", "operation", "records"}:
            return False
        if meta["kind"] != "public_live" or meta["endpoint"] != PUBLIC_ENDPOINTS.get(meta["operation"]):
            return False
        from datetime import datetime

        if datetime.fromisoformat(meta["captured_at"]).tzinfo is None:
            return False
        if not isinstance(meta["company"], str) or not 0 < len(meta["company"]) <= 200:
            return False
        response = fixture["response"]
        if set(response) != {"format", "body"}:
            return False
        body = response["body"]
        raw = body.encode("utf-8") if response["format"] == "xml" else json.dumps(body).encode("utf-8")
        fmt, trimmed, count = trim_response(meta["operation"], Response(200, {}, raw))
        return (fmt == response["format"] and trimmed == body
                and count == meta["records"] and 3 <= count <= 5 and len(data) <= 100_000)
    except Exception:
        return False


def audit_tracked_source(repo_root: Path) -> tuple[int, list[str]]:
    """Inspect only tracked Project Watch files; untracked files need separate review."""
    scope = product_scope(repo_root)
    command = subprocess.run(
        ["git", "ls-files", "-z", "--", scope],
        cwd=repo_root,
        capture_output=True,
        check=True,
    )
    paths = [item.decode("utf-8") for item in command.stdout.split(b"\0") if item]
    findings: list[str] = []
    if not paths:
        findings.append(f"{scope}: no tracked files found")
    for relative in paths:
        path = repo_root / relative
        if path.is_symlink() or not path.is_file():
            findings.append(f"{relative}: non-regular tracked file")
            continue
        if path.suffix.lower() not in _SOURCE_SUFFIXES and path.name not in _SOURCE_BASENAMES:
            findings.append(f"{relative}: non-text tracked artifact")
            continue
        if path.stat().st_size > _MAX_MEMBER_BYTES:
            findings.append(f"{relative}: tracked file too large")
            continue
        data = path.read_bytes()
        findings.extend(_secret_findings(data, relative))
        if path.suffix.lower() == ".json":
            try:
                json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                findings.append(f"{relative}: invalid text JSON")
        if "/tests/fixtures/" in "/" + relative.replace("\\", "/") and not any(
            marker in data for marker in (b"synthetic", b"Synthetic", "合成".encode())
        ) and not _public_studio_fixture(relative, data):
            findings.append(f"{relative}: fixture lacks synthetic marker")
    return len(paths), findings


def dependency_licenses(extras: set[str], product_name: str = "projectwatch-local") -> tuple[list[dict[str, str]], list[str]]:
    """Inventory installed runtime dependency closure for selected extras."""
    pending = deque([(product_name, frozenset(extras))])
    seen: dict[str, set[str]] = {}
    records: dict[str, dict[str, str]] = {}
    findings: list[str] = []
    while pending:
        name, selected_extras = pending.popleft()
        normalized = canonicalize_name(name)
        if normalized in seen and selected_extras.issubset(seen[normalized]):
            continue
        seen.setdefault(normalized, set()).update(selected_extras)
        try:
            distribution = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            findings.append(f"{normalized}: dependency not installed")
            continue
        metadata = distribution.metadata
        license_name = metadata.get("License-Expression") or metadata.get("License")
        if not license_name:
            classifiers = metadata.get_all("Classifier", [])
            license_name = "; ".join(
                classifier for classifier in classifiers if classifier.startswith("License ::")
            )
        license_name = (license_name or "").strip()
        if not license_name or len(license_name) > 200 or "\n" in license_name:
            license_name = "REVIEW_REQUIRED"
            findings.append(f"{normalized}: license metadata needs manual review")
        records[normalized] = {
            "name": metadata.get("Name", name),
            "version": distribution.version,
            "license": license_name,
        }
        for requirement_text in distribution.requires or []:
            requirement = Requirement(requirement_text)
            active = requirement.marker is None or any(
                requirement.marker.evaluate({"extra": extra}) for extra in selected_extras | {""}
            )
            if not active:
                continue
            try:
                child = importlib.metadata.distribution(requirement.name)
            except importlib.metadata.PackageNotFoundError:
                findings.append(f"{requirement.name}: dependency not installed")
                continue
            if requirement.specifier and child.version not in requirement.specifier:
                findings.append(f"{requirement.name}: installed version violates requirement")
            pending.append((requirement.name, frozenset(requirement.extras)))
    return sorted(records.values(), key=lambda item: canonicalize_name(item["name"])), sorted(
        set(findings)
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--extras", nargs="*", default=["team", "postgres", "mcp"])
    args = parser.parse_args()
    wheel_count, wheel_findings = audit_wheel(args.wheel)
    if wheel_count == 0:
        wheel_findings.append("wheel: no files found")
    tracked_count, source_findings = audit_tracked_source(args.repo_root)
    licenses, license_findings = dependency_licenses(set(args.extras))
    findings = wheel_findings + source_findings + license_findings
    print(
        json.dumps(
            {
                "wheel_members": wheel_count,
                "tracked_source_files": tracked_count,
                "dependency_licenses": licenses,
                "findings": findings,
                "publication_authorized": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if not findings else 2


if __name__ == "__main__":
    # Run directly, sys.path[0] is scripts/, so the scripts package itself is not
    # importable and the Studio live fixture check cannot load the probe.
    _REPO_ROOT = str(Path(__file__).resolve().parent.parent)
    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)
    sys.exit(main())
