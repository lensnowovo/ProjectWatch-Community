"""Offline runtime attribution inventory. Consistency is NOT release clearance."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

OUTPUTS = (
    "frontend/public/assets/THIRD-PARTY-LICENSES.txt",
    "src/project_watch/web/assets/THIRD-PARTY-LICENSES.txt",
)
OGL_README_SHA256 = "431694c5ceee21023d54c8d11663e028d2f845f00fa49e59cce25f43944c7479"


class NoticeError(ValueError):
    pass


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").strip()


def ogl_license(module: Path) -> str:
    """The verified 1.0.11 npm archive puts its full license in README, not LICENSE."""
    path = module / "README.md"
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != OGL_README_SHA256:
        raise NoticeError("OGL license README differs from verified 1.0.11 archive")
    return read(path).split("## Unlicense", 1)[1].strip()


def render(root: Path) -> str:
    frontend = root / "frontend"
    lock = json.loads(read(frontend / "package-lock.json"))
    sections = [
        "ProjectWatch Studio third-party notices\n\n"
        "Generated from local license files and the locked runtime dependency set.\n"
        "This inventory is not permission to publish or a legal clearance.\n"
        "It does not cover Python wheels, development tools or repository screenshots."
    ]
    bits = frontend / "src/bits"
    attribution = read(bits / "LICENSE-react-bits.md")
    components = sorted(path.stem for path in bits.glob("*.tsx"))
    for name in components:
        if not re.search(rf"\b{re.escape(name)}\b", attribution):
            raise NoticeError(f"Unlisted React Bits component: {name}")
    sections += ["React Bits adapted components: " + ", ".join(components), attribution]
    fonts = sorted((frontend / "src/fonts").glob("OFL-*.txt"))
    if not fonts:
        raise NoticeError("Missing font license files")
    font_notice = frontend / "src/fonts/NOTICE.md"
    if font_notice.is_file():
        sections.append(read(font_notice))
    sections += [f"Font license: {path.name}\n\n{read(path)}" for path in fonts]
    runtime = [(path, entry) for path, entry in lock["packages"].items()
               if path and not entry.get("dev")]
    for relative, entry in sorted(runtime):
        module = (frontend / relative).resolve()
        if not module.is_relative_to((frontend / "node_modules").resolve()):
            raise NoticeError(f"Unexpected runtime package path: {relative}")
        metadata = json.loads(read(module / "package.json"))
        version = entry["version"]
        if metadata.get("version") != version:
            raise NoticeError(f"Installed version differs from lock: {relative}")
        name = metadata["name"]
        declared = entry.get("license", "not declared")
        notices = sorted(path for path in module.iterdir() if path.is_file() and
                         path.name.upper().startswith(("LICENSE", "LICENCE", "COPYING", "NOTICE", "COPYRIGHT")))
        licenses = [path for path in notices if path.name.upper().startswith(("LICENSE", "LICENCE", "COPYING"))]
        heading = f"Runtime dependency: {name}@{version} (declared: {declared})"
        if not licenses:
            # Verified against the lock-pinned npm tarball; do not substitute a floating license.
            if (name, version, declared) != ("ogl", "1.0.11", "Unlicense"):
                raise NoticeError(f"Missing runtime license text: {name}@{version}")
            sections.append(heading + "\n\nREADME.md — Unlicense\n" + ogl_license(module))
            continue
        sections.append(heading + "\n\n" + "\n\n".join(
            f"{path.name}\n{read(path)}" for path in notices))
    return "\n\n".join(sections) + "\n"


def check_outputs(root: Path, expected: str) -> list[str]:
    return [relative for relative in OUTPUTS
            if not (root / relative).is_file() or read(root / relative) != expected.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    expected = render(root)
    if args.write:
        for relative in OUTPUTS:
            (root / relative).write_text(expected, encoding="utf-8", newline="\n")
    drift = check_outputs(root, expected)
    if drift:
        print("Stale third-party notices: " + ", ".join(drift))
        return 1
    print("Third-party notices match local sources; rights review still required.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
