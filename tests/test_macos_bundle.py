"""Archive/platform contracts only; no claim to execute Darwin on Windows."""
import io
import json
import plistlib
import struct
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import build_macos_bundle as bundle


def runtime(tmp_path, entries):
    archive = tmp_path / "runtime.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        for name, payload, mode, link in entries:
            item = tarfile.TarInfo(name)
            item.mode = mode
            if link is not None:
                item.type = tarfile.SYMTYPE
                item.linkname = link
                stream.addfile(item)
            else:
                item.size = len(payload)
                stream.addfile(item, io.BytesIO(payload))
    return archive


def test_build_rejects_same_version_wheel_from_an_older_checkout(tmp_path, monkeypatch):
    repo = tmp_path / "checkout"
    monkeypatch.setattr(bundle, "__file__", str(repo / "scripts/build_macos_bundle.py"))
    wheel = tmp_path / "projectwatch_local-0.3.0a3-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("projectwatch_local-0.3.0a3.dist-info/METADATA",
                         "Name: projectwatch-local\nVersion: 0.3.0a3\n")
        for name in ("studio/desktop.py", "studio/insight_generation.py", "api.py", "web/index.html"):
            source = repo / "src/project_watch" / name
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"original")
            archive.write(source, f"project_watch/{name}")
    (repo / "src/project_watch/api.py").write_bytes(b"updated without a version bump")

    archive = runtime(tmp_path, [])
    monkeypatch.setitem(bundle.RUNTIMES["arm64"], "sha256", bundle.digest(archive))
    args = SimpleNamespace(out=tmp_path / "out", wheel=wheel, runtime=archive, arch="arm64",
                           uv="missing-synthetic-uv")
    with pytest.raises(ValueError, match=r"correspondence failed: extra=0, missing=0, changed=1"):
        bundle.build(args)
    assert not args.out.exists()


def test_extract_internal_links_and_keep_executable_mode(tmp_path):
    archive = runtime(tmp_path, [("python/bin/python3.12", b"synthetic", 0o755, None),
                                 ("python/bin/python3", b"", 0o777, "python3.12")])
    dest = tmp_path / "runtime"
    modes = bundle.extract_runtime(archive, dest, bundle.digest(archive))
    assert (dest / "bin/python3").read_bytes() == b"synthetic"
    assert not (dest / "bin/python3").is_symlink()
    assert modes["bin/python3"] == 0o755


@pytest.mark.parametrize("name,link", [
    ("../escape", None), ("python/../escape", None), ("/absolute", None),
    ("python/bin/link", "../../../escape"), ("python/bin/link", "/etc/passwd"),
    ("python/bin/link", "link"), ("python/bin/link", "missing"),
])
def test_runtime_rejects_unsafe_paths_before_writing(tmp_path, name, link):
    archive = runtime(tmp_path, [(name, b"synthetic", 0o644, link)])
    dest = tmp_path / "runtime"
    with pytest.raises(ValueError):
        bundle.extract_runtime(archive, dest, bundle.digest(archive))
    assert not dest.exists()


def test_bad_hash_and_existing_dest_are_never_overwritten(tmp_path):
    archive = runtime(tmp_path, [("python/file", b"synthetic", 0o644, None)])
    with pytest.raises(ValueError):
        bundle.extract_runtime(archive, tmp_path / "out", "wrong")
    out = tmp_path / "out"
    out.mkdir()
    (out / "original").write_bytes(b"keep")
    with pytest.raises(ValueError):
        bundle.extract_runtime(archive, out, bundle.digest(archive))
    assert (out / "original").read_bytes() == b"keep"


def test_duplicate_casefold_path_rejected(tmp_path):
    archive = runtime(tmp_path, [("python/A", b"a", 0o644, None), ("python/a", b"b", 0o644, None)])
    with pytest.raises(ValueError):
        bundle.extract_runtime(archive, tmp_path / "out", bundle.digest(archive))


def test_native_cpu_check(tmp_path):
    executable = tmp_path / "python"
    executable.write_bytes(b"\xcf\xfa\xed\xfe" + struct.pack("<I", 0x0100000C))
    bundle.validate_macho(executable, "arm64")
    with pytest.raises(ValueError, match="architecture"):
        bundle.validate_macho(executable, "x86_64")
    executable.write_bytes(b"MZWindowsBinary")
    with pytest.raises(ValueError):
        bundle.validate_macho(executable, "arm64")


def test_reject_wrong_python_extension_abi(tmp_path):
    (tmp_path / "extension.cpython-313-darwin.so").write_bytes(b"synthetic")
    with pytest.raises(ValueError, match="ABI"):
        bundle.validate_native_tree(tmp_path, "arm64")


def test_plist_and_archive_permissions(tmp_path):
    app = tmp_path / "ProjectWatch.app"
    executable = app / "Contents/MacOS/ProjectWatch"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"#!/bin/sh\nexit 0\n")
    bundle.write_plist(app, "0.2.0a1", "arm64")
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    assert info["CFBundleExecutable"] == "ProjectWatch" and info["LSUIElement"]
    assert info["CFBundleVersion"] == "0.2.0a1"
    out = tmp_path / "app.zip"
    bundle.archive_app(app, out, {"Contents/MacOS/ProjectWatch": 0o755})
    with zipfile.ZipFile(out) as archive:
        entry = archive.getinfo("ProjectWatch.app/Contents/MacOS/ProjectWatch")
        assert (entry.external_attr >> 16) & 0o777 == 0o755
        assert archive.testzip() is None


def test_launcher_no_bootstrap_or_system_python():
    path = Path(__file__).resolve().parents[1] / "scripts/macos/ProjectWatch.sh"
    source = path.read_bytes()
    assert b"\r" not in source
    assert b'"$RUNTIME/bin/python3.12" -I -B' in source
    assert b"pip install" not in source and b"npm" not in source and b"curl " not in source
    assert b"PROJECT_WATCH_HOME" not in source


def test_macos_home_never_adopts_existing_developer_or_windows_home(tmp_path, monkeypatch):
    from project_watch.studio import desktop
    monkeypatch.setattr(desktop.sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("PROJECT_WATCH_HOME", str(tmp_path / "old-data"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "windows"))
    assert desktop.desktop_home() == tmp_path / "Library/Application Support/ProjectWatch-Desktop"
    assert not list(tmp_path.iterdir())


def test_finder_path_is_single_argument(tmp_path, monkeypatch):
    from project_watch.studio import desktop
    monkeypatch.setattr(desktop.sys, "platform", "darwin")
    calls = []
    monkeypatch.setattr(desktop.subprocess, "run", lambda argv, **kw: calls.append(argv))
    path = tmp_path / '资料 "quotes" with spaces'
    desktop.open_data_folder(path)
    assert calls == [["/usr/bin/open", str(path)]]
















@pytest.mark.parametrize('fault', [None, 'missing', 'changed', 'permission', 'extra'])
def test_actual_zip_is_checked_against_inventory_and_execution_modes(tmp_path, fault):
    app = tmp_path / 'ProjectWatch.app'
    names = ['Contents/MacOS/ProjectWatch', 'Contents/Resources/runtime/bin/python3.12']
    for rel in names:
        path = app / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'\xcf\xfa\xed\xfe' + struct.pack('<I', 0x0100000C))
    payload = app / 'Contents/Resources/payload'
    payload.write_bytes(b'synthetic')
    manifest = {'format': 'projectwatch-macos-bundle-v1', 'arch': 'arm64',
                'files': {p.relative_to(app).as_posix(): bundle.digest(p) for p in sorted(app.rglob('*')) if p.is_file()}}
    (app / 'Contents/Resources/bundle-manifest.json').write_text(json.dumps(manifest))
    modes = {name: 0o755 for name in names}
    if fault == 'missing':
        payload.unlink()
    elif fault == 'changed':
        payload.write_bytes(b'mutated after manifest')
    elif fault == 'permission':
        modes[names[-1]] = 0o644
    elif fault == 'extra':
        (app / 'unlisted').write_bytes(b'synthetic')
    archive = tmp_path / 'app.zip'
    bundle.archive_app(app, archive, modes)
    if fault is None:
        assert bundle.verify_archive(archive) == {'files': 4, 'arch': 'arm64', 'native_execution': False}
    else:
        with pytest.raises(ValueError):
            bundle.verify_archive(archive)
