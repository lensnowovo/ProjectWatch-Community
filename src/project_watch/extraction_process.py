"""Bounded subprocess entrypoint for document text extraction.

The child gets no database credentials or network client. This separates parser
crashes and hangs from the API process, but it is not an OS security sandbox.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

from project_watch.domain import FragmentInput
from project_watch.extractors import (
    MAX_FRAGMENT_CHARS,
    ExtractionError,
    ExtractionResult,
    extract_document,
)
from project_watch.file_validation import ALLOWED_SUFFIXES, DEFAULT_MAX_BYTES

PARSER_TIMEOUT_SECONDS = 45
PDF_PARSER_TIMEOUT_SECONDS = 360
MAX_PARSER_OUTPUT_BYTES = 16 * 1024 * 1024
_CHILD_ENV_KEYS = (
    "SystemRoot", "WINDIR", "PATH", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL",
    "TESSDATA_PREFIX",
)


def _child_environment() -> dict[str, str]:
    environment = {key: os.environ[key] for key in _CHILD_ENV_KEYS if key in os.environ}
    # Office/PDF dependencies may load BLAS transitively. Keep each short-lived
    # parser within its process budget, regardless of the host's core count.
    environment["OPENBLAS_NUM_THREADS"] = "1"
    environment["OMP_NUM_THREADS"] = "1"
    return environment


def _decode_result(
    payload: bytes, *, max_output_bytes: int | None = None
) -> ExtractionResult:
    if max_output_bytes is not None and len(payload) > max_output_bytes:
        raise ExtractionError("parser_output_invalid")
    try:
        raw = json.loads(payload)
        if not isinstance(raw, dict):
            raise ValueError
        if "error_code" in raw:
            code = raw["error_code"]
            if not isinstance(code, str) or code not in {
                "empty_upload",
                "encrypted_document",
                "invalid_document",
                "limit_exceeded",
                "no_text",
                "unsafe_archive",
                "unsupported_format",
            }:
                raise ValueError
            raise ExtractionError(code)
        fragments = raw["fragments"]
        needs_ocr = raw["needs_ocr"]
        if not isinstance(fragments, list) or not isinstance(needs_ocr, bool):
            raise ValueError
        validated: list[FragmentInput] = []
        for index, fragment in enumerate(fragments, start=1):
            if not isinstance(fragment, dict):
                raise ValueError
            ordinal = fragment["ordinal"]
            locator = fragment["locator"]
            content = fragment["text"]
            if (
                type(ordinal) is not int
                or ordinal != index
                or not isinstance(locator, str)
                or not 0 < len(locator) <= 512
                or not isinstance(content, str)
                or not 0 < len(content) <= MAX_FRAGMENT_CHARS
            ):
                raise ValueError
            validated.append(FragmentInput(ordinal=ordinal, locator=locator, text=content))
        if not validated and not needs_ocr:
            raise ValueError
        return ExtractionResult(fragments=validated, needs_ocr=needs_ocr)
    except ExtractionError:
        raise
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExtractionError("parser_output_invalid") from exc


def extract_document_subprocess(
    path: Path | str, *, suffix: str, timeout_seconds: float | None = None,
    max_output_bytes: int | None = None,
    cancel=None, on_progress=None, idle_seconds: float = 900,
) -> ExtractionResult:
    """Run the local parser without volume ceilings, never passing API secrets.

    Network/team callers may request a watchdog and result-byte quota explicitly.
    This child-process boundary is not an OS security sandbox.
    """
    if timeout_seconds is not None and timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive or None")
    if timeout_seconds is None:
        from project_watch.parser_supervision import run_parser
        try:
            path = Path(path).resolve(strict=True)
            with tempfile.TemporaryDirectory(prefix='project-watch-parser-') as directory:
                output, progress = Path(directory) / 'result.json', Path(directory) / 'progress'
                run_parser([sys.executable, '-m', 'project_watch.extraction_process', str(path), suffix,
                            str(output), str(progress)], env=_child_environment(), progress_path=progress,
                           cancel=cancel, on_progress=on_progress, idle_seconds=idle_seconds)
                return _decode_result(output.read_bytes(), max_output_bytes=max_output_bytes)
        except OSError as exc:
            raise ExtractionError('parser_crashed') from exc
    try:
        path = Path(path).resolve(strict=True)
        completed = subprocess.run(
            [sys.executable, "-m", "project_watch.extraction_process", str(path), suffix],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout_seconds,
            check=False,
            cwd=path.parent,
            env=_child_environment(),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired as exc:
        raise ExtractionError("parser_timeout") from exc
    except OSError as exc:
        raise ExtractionError("parser_crashed") from exc
    if completed.returncode != 0:
        raise ExtractionError("parser_crashed")
    return _decode_result(completed.stdout, max_output_bytes=max_output_bytes)


def main() -> None:
    if len(sys.argv) not in (3, 5):
        raise SystemExit(2)
    last_progress = 0.0
    def progress():
        nonlocal last_progress
        if len(sys.argv) == 5 and time.monotonic() - last_progress >= .5:
            Path(sys.argv[4]).touch()
            last_progress = time.monotonic()
    try:
        if sys.argv[1] == "--stdin":
            suffix = sys.argv[2].lower()
            if suffix not in ALLOWED_SUFFIXES:
                raise ExtractionError("unsupported_format")
            with tempfile.TemporaryDirectory(prefix="project-watch-parser-") as directory:
                source = Path(directory) / ("source" + suffix)
                size = 0
                with source.open("wb") as target:
                    while chunk := sys.stdin.buffer.read(1024 * 1024):
                        size += len(chunk)
                        if size > DEFAULT_MAX_BYTES:
                            raise ExtractionError("limit_exceeded")
                        target.write(chunk)
                result = extract_document(source, suffix=suffix, max_bytes=DEFAULT_MAX_BYTES)
        else:
            result = extract_document(sys.argv[1], suffix=sys.argv[2], progress=progress)
        payload = {
            "fragments": [asdict(fragment) for fragment in result.fragments],
            "needs_ocr": result.needs_ocr,
        }
    except ExtractionError as exc:
        payload = {"error_code": exc.error_code}
    except Exception:
        payload = {"error_code": "invalid_document"}
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(sys.argv) == 5:
        Path(sys.argv[3]).write_bytes(encoded)
    else:
        sys.stdout.buffer.write(encoded)


if __name__ == "__main__":
    main()
