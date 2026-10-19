"""Write a synthetic-only receipt for one already-built Project Watch wheel."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from scripts.release_audit import audit_wheel


def build_manifest(wheel: Path, commit: str) -> dict[str, str | int | bool]:
    if wheel.suffix != ".whl" or not wheel.is_file():
        raise ValueError("wheel must be an existing .whl file")
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise ValueError("commit must be a full lowercase Git SHA")
    count, findings = audit_wheel(wheel)
    if count == 0 or findings:
        raise ValueError("wheel inventory rejected")
    with wheel.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    return {
        "schema_version": 1,
        "commit": commit,
        "wheel": wheel.name,
        "sha256": digest,
        "size_bytes": wheel.stat().st_size,
        "scope": "synthetic_only",
        "publication_authorized": False,
    }


def write_manifest(wheel: Path, commit: str, output: Path) -> None:
    receipt = build_manifest(wheel, commit)
    output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    write_manifest(args.wheel, args.commit, args.output)


if __name__ == "__main__":
    main()
