"""Assemble a private, unsigned macOS .app with a pinned relocatable interpreter.

This can assemble target files on Windows, but cannot certify native Mac behavior.
Only the explicit wheel and pinned runtime are inputs; user data is never copied.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import posixpath
import re
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

from scripts.build_windows_bundle import digest, validate_output, verify_product

RUNTIMES = {
    "arm64": {"target": "aarch64-apple-darwin", "sha256": "ba32036ad8f4c434d97a1ee7feef8b7edc0fac16777639b97e39c74a108e4440"},
    "x86_64": {"target": "x86_64-apple-darwin", "sha256": "9686ddd8639b855905447ea4a16eb54a804a9ef12afbe7c9ecb1412d9c1fbc98"},
}
PYTHON_VERSION = "3.12.15"
RUNTIME_RELEASE = "20261013"
EXTRAS = "desktop,ocr,mcp"


def runtime_url(arch):
    target = RUNTIMES[arch]["target"]
    return ("https://github.com/astral-sh/python-build-standalone/releases/download/"
            f"{RUNTIME_RELEASE}/cpython-{PYTHON_VERSION}%2B{RUNTIME_RELEASE}-{target}-install_only.tar.gz")


def safe_name(name):
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or ".." in path.parts or "\\" in name
            or ":" in name or "\x00" in name or path.parts[0] != "python"):
        raise ValueError("Unsafe runtime member")
    return path


def extract_runtime(archive, dest, expected):
    """Materialize internal links as copies, retaining executable modes for ZIP.

    Avoid creating platform-dependent Windows symlinks. Never follow disk links.
    Resolve link contents from the verified archive only, rejecting cycles/escapes.
    """
    if digest(archive) != expected or dest.exists():
        raise ValueError("Runtime hash mismatch or destination already exists")
    modes = {}
    with tarfile.open(archive, "r:gz") as stream:
        members = {}
        seen = set()
        total = 0
        for item in stream.getmembers():
            name = safe_name(item.name).as_posix()
            if name.casefold() in seen or not (item.isdir() or item.isfile() or item.issym() or item.islnk()):
                raise ValueError("Duplicate/special runtime member")
            seen.add(name.casefold())
            members[name] = item
            total += item.size
        if total > 2 * 1024**3:
            raise ValueError("Unexpected runtime archive size")

        def source(name, chain=()):
            if name in chain or len(chain) > 32 or name not in members:
                raise ValueError("Invalid runtime link")
            item = members[name]
            if item.isfile():
                return item
            if item.issym() or item.islnk():
                link = item.linkname
                if PurePosixPath(link).is_absolute() or "\\" in link or ":" in link:
                    raise ValueError("Unsafe runtime link")
                target = posixpath.normpath(posixpath.join(posixpath.dirname(name), link) if item.issym() else link)
                safe_name(target)
                return source(target, (*chain, name))
            raise ValueError("Runtime link must reference a regular file")

        # Validate every link before writing anything.
        for name, item in members.items():
            if not item.isdir():
                source(name)
        dest.mkdir(parents=True)
        for name, item in members.items():
            relative = PurePosixPath(name).relative_to("python")
            path = dest.joinpath(*relative.parts)
            if item.isdir():
                path.mkdir(parents=True, exist_ok=True)
                continue
            origin = source(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            with stream.extractfile(origin) as src, path.open("xb") as out:
                shutil.copyfileobj(src, out)
            mode = 0o755 if origin.mode & 0o111 else 0o644
            path.chmod(mode)
            modes[relative.as_posix()] = mode
    return modes


def validate_macho(path, arch):
    with path.open('rb') as stream:
        validate_macho_header(stream.read(4096), arch)


def validate_macho_header(data, arch):
    if arch not in RUNTIMES:
        raise ValueError('Unsupported Mach-O architecture')
    expected = 0x0100000C if arch == "arm64" else 0x01000007
    if len(data) < 8:
        raise ValueError("Missing Mach-O executable")
    if data[:4] == b"\xcf\xfa\xed\xfe":
        cpus = [struct.unpack_from("<I", data, 4)[0]]
    elif data[:4] == b"\xca\xfe\xba\xbe":
        count = struct.unpack_from(">I", data, 4)[0]
        if count > 16 or len(data) < 8 + count * 20:
            raise ValueError("Invalid universal Mach-O")
        cpus = [struct.unpack_from(">I", data, 8 + index * 20)[0] for index in range(count)]
    else:
        raise ValueError("Not a supported Mach-O executable")
    if expected not in cpus:
        raise ValueError("Wrong runtime architecture")


def validate_native_tree(runtime, arch):
    for path in runtime.rglob("*"):
        if path.is_file() and path.suffix in {".so", ".dylib"}:
            abi = re.search(r"(?:cpython-|\.cp)(\d{2,3})", path.name)
            if abi and abi[1] != "312":
                raise ValueError("Wrong Python extension ABI: " + path.name)
            validate_macho(path, arch)


def write_plist(app, version, arch):
    match = re.fullmatch(r"(\d+\.\d+\.\d+)(?:(a|b|rc)(\d+))?", version)
    if not match:
        raise ValueError("Version needs an explicit macOS build-number mapping")
    short = match[1]
    build_version = short + (("fc" if match[2] == "rc" else match[2]) + match[3] if match[2] else "")
    info = {"CFBundleName": "ProjectWatch", "CFBundleDisplayName": "ProjectWatch",
            "CFBundleIdentifier": "app.projectwatch.desktop", "CFBundlePackageType": "APPL",
            "CFBundleExecutable": "ProjectWatch", "CFBundleVersion": build_version,
            "CFBundleIconFile": "ProjectWatch.icns",
            "CFBundleShortVersionString": short, "ProjectWatchVersion": version,
            "LSMinimumSystemVersion": "13.0", "LSUIElement": True,
            "LSArchitecturePriority": [arch], "NSHighResolutionCapable": True}
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps(info))


def archive_app(app, output, modes):
    if output.exists():
        raise ValueError("Archive destination exists")
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(app.rglob("*")):
            if path.is_symlink():
                raise ValueError("Unexpected installed symlink")
            if not path.is_file():
                continue
            relative = path.relative_to(app).as_posix()
            info = zipfile.ZipInfo(app.name + "/" + relative)
            info.create_system = 3
            mode = modes.get(relative, 0o644)
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            with path.open("rb") as src, archive.open(info, "w") as out:
                shutil.copyfileobj(src, out)


def verify_archive(output):
    """Re-open the actual ZIP; a valid staging directory is not a valid download."""
    prefix = 'ProjectWatch.app/'
    manifest_name = 'Contents/Resources/bundle-manifest.json'
    with zipfile.ZipFile(output) as archive:
        entries = {}
        folded = set()
        for item in archive.infolist():
            name = item.filename
            path = PurePosixPath(name)
            if (not name.startswith(prefix) or path.as_posix() != name or '..' in path.parts
                    or '\\' in name or ':' in name or name.casefold() in folded
                    or stat.S_ISLNK(item.external_attr >> 16)):
                raise ValueError('Unsafe/duplicate Mac archive entry')
            folded.add(name.casefold())
            if not item.is_dir():
                entries[name[len(prefix):]] = item
        manifest = json.loads(archive.read(prefix + manifest_name))
        if manifest.get('format') != 'projectwatch-macos-bundle-v1' or manifest.get('arch') not in RUNTIMES:
            raise ValueError('Invalid Mac bundle manifest')
        expected = manifest['files']
        if set(entries) != set(expected) | {manifest_name}:
            raise ValueError('Mac archive inventory mismatch')
        for rel, checksum in expected.items():
            with archive.open(entries[rel]) as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != checksum:
                    raise ValueError('Mac archive file hash mismatch: ' + rel)
        for rel in ('Contents/MacOS/ProjectWatch', 'Contents/Resources/runtime/bin/python3.12'):
            if rel not in entries or (entries[rel].external_attr >> 16) & 0o111 != 0o111:
                raise ValueError('Mac archive missing executable permission: ' + rel)
        for rel in ('Contents/Resources/runtime/bin/python3.12',):
            with archive.open(entries[rel]) as stream:
                validate_macho_header(stream.read(4096), manifest['arch'])
    return {'files': len(entries), 'arch': manifest['arch'], 'native_execution': False}


def build(args):
    repo = Path(__file__).resolve().parents[1]
    dest, wheel = args.out.resolve(), args.wheel.resolve()
    validate_output(dest, repo)
    spec = RUNTIMES[args.arch]
    if digest(args.runtime) != spec["sha256"]:
        raise ValueError("Pinned upstream runtime hash mismatch")
    with zipfile.ZipFile(wheel) as z:
        name, = [n for n in z.namelist() if n.endswith(".dist-info/METADATA")]
        metadata = BytesParser().parsebytes(z.read(name))
        if metadata["Name"] != "projectwatch-local":
            raise ValueError("Expected the full ProjectWatch wheel")
        for name in ("project_watch/studio/desktop.py", "project_watch/studio/insight_generation.py",
                     "project_watch/api.py", "project_watch/web/index.html"):
            if name not in z.namelist():
                raise ValueError("Complete distribution required")
    uv = shutil.which(args.uv)
    if not uv:
        raise ValueError("Maintainer needs uv; end users do not")
    dest.mkdir()
    app = dest / "ProjectWatch.app"
    resources = app / "Contents/Resources"
    runtime = resources / "runtime"
    runtime_modes = extract_runtime(args.runtime, runtime, spec["sha256"])
    validate_macho(runtime / "bin/python3.12", args.arch)
    site = runtime / "lib/python3.12/site-packages"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PROJECT_WATCH_", "PYTHON", "UV_", "PIP_"))}
    subprocess.run([uv, "pip", "install", "--python", sys.executable,
                    "--python-version", "3.12", "--python-platform", spec["target"],
                    "--only-binary", ":all:", "--link-mode", "copy", "--target", str(site), str(wheel) + f"[{EXTRAS}]"],
                   env=env, check=True)
    verify_product(wheel, site)
    from PIL import Image
    with Image.open(site/'project_watch/static/brand-desktop.png') as icon:
        icon.save(resources/'ProjectWatch.icns', format='ICNS')
    validate_native_tree(runtime, args.arch)
    for required in ("certifi/cacert.pem", "pystray/_darwin.py", "objc/__init__.py", "AppKit/__init__.py"):
        if not (site / required).is_file():
            raise ValueError("Missing macOS runtime dependency: " + required)
    executable = app / "Contents/MacOS/ProjectWatch"
    executable.parent.mkdir(parents=True)
    executable.write_bytes((repo / "scripts/macos/ProjectWatch.sh").read_bytes())
    executable.chmod(0o755)
    write_plist(app, metadata["Version"], args.arch)
    for name in ("LICENSE", "NOTICE"):
        shutil.copyfile(repo / name, resources / name)
    shutil.copytree(repo / "LICENSES", resources / "LICENSES")
    modes = {"Contents/Resources/runtime/" + name: mode for name, mode in runtime_modes.items()}
    modes["Contents/MacOS/ProjectWatch"] = 0o755
    # Shared libraries loaded by dyld only need read permission; no installed CLI is the app entrypoint.
    inventory = {p.relative_to(app).as_posix(): digest(p) for p in sorted(app.rglob("*")) if p.is_file()}
    manifest = {"format": "projectwatch-macos-bundle-v1", "version": metadata["Version"], "arch": args.arch,
                "python": PYTHON_VERSION, "runtime_url": runtime_url(args.arch), "runtime_sha256": spec["sha256"],
                "product_wheel_sha256": digest(wheel), "builder_sha256": digest(Path(__file__)),
                "launcher_sha256": digest(executable), "files": inventory,
                "native_verified": False, "signed": False, "notarized": False, "publication_authorized": False}
    (resources / "bundle-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    output = dest / f"ProjectWatch-macOS-{args.arch}-candidate.zip"
    archive_app(app, output, modes)
    verify_archive(output)
    print(json.dumps({"archive": str(output), "sha256": digest(output), "files": len(inventory),
                      "native_verified": False, "publication_authorized": False}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arch", choices=RUNTIMES, required=True)
    for name in ("runtime", "wheel", "out"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--uv", default="uv")
    build(parser.parse_args())


if __name__ == "__main__":
    # Maintainer invocation is `python -m scripts.build_macos_bundle` from the checkout.
    main()
