"""Shared admission checks for local source documents."""

from __future__ import annotations

import stat
import zipfile
from pathlib import Path

ALLOWED_SUFFIXES = frozenset({".txt", ".pdf", ".docx", ".pptx", ".xlsx", ".xls"})
DEFAULT_MAX_BYTES = 20 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 2_000
MAX_ARCHIVE_UNCOMPRESSED = 50 * 1024 * 1024
MAX_ARCHIVE_RATIO = 200


class FileValidationError(ValueError):
    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


def validate_document(
    path: Path, suffix: str, *, max_bytes: int | None = DEFAULT_MAX_BYTES
) -> None:
    """Reject unsupported, mislabelled, or structurally dangerous inputs."""

    suffix = suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise FileValidationError("unsupported_format")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise FileValidationError("invalid_document") from exc
    if size == 0:
        raise FileValidationError("empty_upload")
    if max_bytes is not None and size > max_bytes:
        raise FileValidationError("limit_exceeded")
    with path.open("rb") as handle:
        header = handle.read(4096)
    if suffix == ".txt":
        if b"\x00" in header:
            raise FileValidationError("invalid_document")
        return
    if suffix == ".pdf":
        if not header.startswith(b"%PDF-"):
            raise FileValidationError("invalid_document")
        return
    if suffix == ".xls":
        if not header.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            raise FileValidationError("invalid_document")
        return
    if not header.startswith(b"PK\x03\x04"):
        raise FileValidationError("invalid_document")

    required = {
        ".docx": "word/document.xml",
        ".pptx": "ppt/presentation.xml",
        ".xlsx": "xl/workbook.xml",
    }[suffix]
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if max_bytes is not None and len(entries) > MAX_ARCHIVE_ENTRIES:
                raise FileValidationError("unsafe_archive")
            names: set[str] = set()
            total_size = 0
            for info in entries:
                names.add(info.filename)
                normalized = info.filename.replace("\\", "/")
                parts = normalized.split("/")
                if normalized.startswith("/") or ".." in parts or ":" in parts[0]:
                    raise FileValidationError("unsafe_archive")
                if stat.S_IFMT(info.external_attr >> 16) == stat.S_IFLNK:
                    raise FileValidationError("unsafe_archive")
                total_size += info.file_size
                if max_bytes is not None and total_size > MAX_ARCHIVE_UNCOMPRESSED:
                    raise FileValidationError("unsafe_archive")
                if (
                    info.file_size > 1024
                    and info.file_size > info.compress_size * MAX_ARCHIVE_RATIO
                ):
                    raise FileValidationError("unsafe_archive")
            if required not in names:
                raise FileValidationError("invalid_document")
    except (zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise FileValidationError("invalid_document") from exc
