"""Bounded content-addressed original storage for the local preview."""

from __future__ import annotations

import hashlib
import ntpath
import os
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from project_watch.file_validation import (
    ALLOWED_SUFFIXES,
    DEFAULT_MAX_BYTES,
    FileValidationError,
    validate_document,
)


class UploadError(ValueError):
    """An uploaded original did not meet the preview intake boundary."""


@dataclass(frozen=True, slots=True)
class StoredBlob:
    sha256: str
    display_name: str
    suffix: str
    size: int
    path: Path


@dataclass(frozen=True, slots=True)
class BlobInventory:
    """Read-only reconciliation; never deletes objects or changes source records."""

    present: tuple[str, ...]
    missing: tuple[str, ...]
    corrupt: tuple[str, ...]
    orphan: tuple[str, ...]
    unexpected: tuple[str, ...]


class BlobStore:
    def __init__(self, root: Path | str, *, max_bytes: int | None = DEFAULT_MAX_BYTES) -> None:
        self.root = Path(root).resolve()
        if self.root == Path(self.root.anchor) or self.root == Path.home().resolve():
            raise ValueError("blob store must be a dedicated subdirectory")
        if not self.root.parent.is_dir():
            raise ValueError("blob store parent directory does not exist")
        if max_bytes is not None and max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self.max_bytes = max_bytes
        self.root.mkdir(exist_ok=True)

    def path_for_hash(self, sha256: str) -> Path:
        if len(sha256) != 64 or any(character not in "0123456789abcdef" for character in sha256):
            raise ValueError("invalid SHA-256 digest")
        return self.root / sha256[:2] / sha256

    def put_stream(self, filename: str, stream: BinaryIO) -> StoredBlob:
        display_name = ntpath.basename(filename.replace("/", "\\")).strip()
        if not display_name or len(display_name) > 255:
            raise UploadError("filename must contain 1-255 characters")
        suffix = Path(display_name).suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            raise UploadError("unsupported file suffix")

        digest = hashlib.sha256()
        size = 0
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=".incoming-", dir=self.root, delete=False
            ) as temporary:
                temp_path = Path(temporary.name)
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes):
                        raise UploadError("upload stream must contain bytes")
                    size += len(chunk)
                    if self.max_bytes is not None and size > self.max_bytes:
                        raise UploadError("upload exceeds size limit")
                    digest.update(chunk)
                    temporary.write(chunk)
                temporary.flush()
                os.fsync(temporary.fileno())
            if size == 0:
                raise UploadError("empty upload")
            try:
                validate_document(temp_path, suffix, max_bytes=self.max_bytes)
            except FileValidationError as exc:
                raise UploadError(exc.error_code) from exc

            sha256 = digest.hexdigest()
            destination = self.path_for_hash(sha256)
            if destination.parent.is_symlink() or destination.is_symlink():
                raise UploadError("object store contains a symbolic link")
            destination.parent.mkdir(exist_ok=True)
            if destination.exists():
                with destination.open("rb") as existing:
                    existing_hash = hashlib.file_digest(existing, "sha256").hexdigest()
                if existing_hash != sha256:
                    raise UploadError("stored object hash mismatch")
            else:
                os.replace(temp_path, destination)
                temp_path = None
            return StoredBlob(
                sha256=sha256,
                display_name=display_name,
                suffix=suffix,
                size=size,
                path=destination,
            )
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def audit(self, expected_hashes: Iterable[str]) -> BlobInventory:
        """Compare source references with content-addressed objects, without repair.

        Callers should derive expected hashes from authorized project sources.
        A shared hash is physically stored once but remains project-scoped in the
        repository. Orphans are reported, not removed: retention is a human decision.
        """
        expected = set(expected_hashes)
        for digest in expected:
            self.path_for_hash(digest)
        present: set[str] = set()
        corrupt: set[str] = set()
        unexpected: set[str] = set()
        for path in self.root.rglob("*"):
            relative = path.relative_to(self.root).as_posix()
            if path.is_symlink():
                unexpected.add(relative)
                continue
            if path.is_dir():
                continue
            digest = path.name
            if (
                len(path.relative_to(self.root).parts) != 2
                or path.parent.name != digest[:2]
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                unexpected.add(relative)
                continue
            with path.open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            if actual == digest:
                present.add(digest)
            else:
                corrupt.add(digest)
        return BlobInventory(
            present=tuple(sorted(expected & present)),
            missing=tuple(sorted(expected - present - corrupt)),
            corrupt=tuple(sorted(expected & corrupt)),
            orphan=tuple(sorted(present - expected)),
            unexpected=tuple(sorted(unexpected | {digest for digest in corrupt - expected})),
        )
