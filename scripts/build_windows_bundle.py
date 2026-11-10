"""Build a local-only Windows bundle from reviewed runtime and offline wheels.

Never discovers user settings or downloads providers. A caller supplies the
runtime ZIP (official SHA-256 pinned here), wheelhouse and complete product wheel.
No agent runtime or Node.js is shipped: the app calls the configured model directly.
Output must be new and outside the repository; publication is a separate action.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath

PYTHON_VERSION = '3.13.17'
RUNTIME_URL = 'https://www.python.org/ftp/python/3.13.17/python-3.13.17-embed-amd64.zip'
RUNTIME_SHA256 = '9e35319bffedab8b36ad44d8f377cb4f1733bcec257a0b6d04d94f01486ddb38'


def runtime_digest(files):
    """Must match scripts/windows/maintain.py: the runtime id is its inventory's identity."""
    return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def validate_output(dest: Path, repo: Path):
    dest, repo = dest.resolve(), repo.resolve()
    if dest.exists() or dest.is_relative_to(repo) or repo.is_relative_to(dest):
        raise ValueError('Output must be new and outside source')
    if not dest.parent.is_dir():
        raise ValueError('Output parent must exist')
    if any(p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction()) for p in dest.parents):
        raise ValueError('Linked output path')


def extract_runtime(archive: Path, dest: Path, expected: str):
    if digest(archive) != expected:
        raise ValueError('Runtime hash mismatch')
    if dest.exists():
        raise ValueError('Runtime destination exists')
    with zipfile.ZipFile(archive) as z:
        seen = set()
        for item in z.infolist():
            name = item.orig_filename
            path = PurePosixPath(name)
            if (not name or path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name
                    or name.casefold() in seen or (item.external_attr >> 16) & 0o170000 == 0o120000):
                raise ValueError('Unsafe runtime path')
            seen.add(name.casefold())
        dest.mkdir()
        z.extractall(dest)


def verify_product(wheel: Path, site: Path):
    """Never label another same-version wheel as the supplied release input."""
    with zipfile.ZipFile(wheel) as z:
        for name in z.namelist():
            if name.startswith('project_watch/') and not name.endswith('/'):
                installed = site/name
                if not installed.is_file() or installed.read_bytes() != z.read(name):
                    raise ValueError('Installed product differs from supplied wheel')


def dependency_install_command(wheelhouse: Path, lock: Path, site: Path) -> list[str]:
    return [sys.executable, '-m', 'pip', '--isolated', 'install', '--no-index',
            '--only-binary=:all:', '--require-hashes', '--find-links', str(wheelhouse.resolve()),
            '--target', str(site), '--python-version', '3.13', '--platform', 'win_amd64',
            '--implementation', 'cp', '--no-compile', '-r', str(lock)]


def build(args):
    from pip._vendor import distlib

    from scripts.freeze_windows_dependencies import requirements, verify, verify_installed
    from scripts.windows.portable_scripts import make_portable

    repo = Path(__file__).resolve().parents[1]
    dest = args.out.resolve()
    validate_output(dest, repo)
    if os.name != 'nt':
        raise ValueError('Build on Windows x64')
    wheel = args.wheel.resolve()
    if not wheel.is_file() or not args.wheelhouse.is_dir() or not args.compiler.is_file():
        raise ValueError('Missing wheel, offline wheelhouse or C# compiler')
    from scripts.build_manifest import verify_source_correspondence

    source_receipt = verify_source_correspondence(wheel, repo)
    dependencies = verify(args.dependency_lock.resolve(), args.wheelhouse.resolve(), wheel)
    if distlib.__version__ != '0.4.2':
        raise ValueError('Build requires reviewed distlib 0.4.2 from pip')
    launcher_license = Path(distlib.__file__).parent/'LICENSE.txt'
    if not launcher_license.is_file():
        raise ValueError('Dependency launcher license missing')
    # Validate every input before creating a partial output.
    if digest(args.runtime) != RUNTIME_SHA256:
        raise ValueError('Runtime hash mismatch')
    from email.parser import BytesParser
    with zipfile.ZipFile(wheel) as z:
        meta, = [n for n in z.namelist() if n.endswith('.dist-info/METADATA')]
        metadata = BytesParser().parsebytes(z.read(meta))
        if metadata['Name'] != 'projectwatch-local':
            raise ValueError('Not a ProjectWatch complete wheel')
        for required in ('project_watch/studio/desktop.py', 'project_watch/studio/insight_generation.py',
                         'project_watch/web/index.html', 'project_watch/api.py'):
            if required not in z.namelist():
                raise ValueError('Complete wheel required')
        version = metadata['Version']
    dest.mkdir()
    shutil.copyfile(args.dependency_lock, dest/'dependency-lock.json')
    (dest/'requirements.lock').write_text(requirements(dependencies), encoding='utf-8', newline='\n')
    runtime = dest/'runtime'
    extract_runtime(args.runtime, runtime, RUNTIME_SHA256)
    (runtime/'python313._pth').write_text('python313.zip\n.\nLib/site-packages\nimport site\n', encoding='ascii')
    shutil.copyfile(repo/'scripts/windows/runtime_sitecustomize.py', runtime/'sitecustomize.py')
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(('PYTHON', 'PROJECT_WATCH_', 'PIP_')):
            env.pop(key)
    env['PIP_DISABLE_PIP_VERSION_CHECK'] = '1'
    subprocess.run(dependency_install_command(args.wheelhouse, dest/'requirements.lock',
                                             runtime/'Lib/site-packages'), env=env, check=True)
    verify(args.dependency_lock.resolve(), args.wheelhouse.resolve(), wheel)
    verify_installed(dependencies, runtime/'Lib/site-packages')
    verify_product(wheel, runtime/'Lib/site-packages')
    portable_scripts = make_portable(runtime/'Lib/site-packages')
    subprocess.run([str(runtime/'python.exe'), '-I', '-B',
        str(repo/'scripts/windows/render_icon.py'), str(dest/'ProjectWatch.ico')], check=True)
    subprocess.run([str(args.compiler), '/nologo', '/codepage:65001', '/target:winexe', '/platform:x64',
        '/win32icon:'+str(dest/'ProjectWatch.ico'),
        '/reference:System.Windows.Forms.dll', '/reference:System.Web.Extensions.dll', '/out:'+str(dest/'ProjectWatch.exe'),
        str(repo/'scripts/windows/Launcher.cs')], check=True)
    # Keep product and runtime notices; dependency dist-info license files remain intact.
    for name in ('LICENSE', 'NOTICE'):
        shutil.copyfile(repo/name, dest/name)
    shutil.copytree(repo/'LICENSES', dest/'LICENSES')
    shutil.copyfile(launcher_license, dest/'LICENSES/third-party/Distlib-0.4.2.txt')
    # The shared-runtime area stays in the manifest format (empty); maintain.py
    # retires runtimes that no installed version references any more.
    runtimes = {}
    inventory = {}
    for path in sorted(dest.rglob('*')):
        if path.is_file() and not path.relative_to(dest).as_posix().startswith('runtimes/'):
            inventory[path.relative_to(dest).as_posix()] = digest(path)
    manifest = {'format': 'projectwatch-windows-bundle-v1', 'python': PYTHON_VERSION,
        'launcher_source_sha256': hashlib.sha256((repo/'scripts/windows/Launcher.cs').read_text(encoding='utf-8').encode()).hexdigest(),
        'version': version, 'runtime_url': RUNTIME_URL, 'runtime_sha256': RUNTIME_SHA256,
        'product_wheel_sha256': digest(wheel), 'source_correspondence': source_receipt,
        'dependency_lock_sha256': digest(dest/'dependency-lock.json'),
        'portable_dependency_scripts': portable_scripts,
        'files': inventory, 'runtimes': runtimes, 'publication_authorized': False}
    (dest/'bundle-manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(json.dumps({'files': len(inventory), 'runtime_files': {k: len(v['files']) for k, v in runtimes.items()},
                      'version': version, 'publication_authorized': False}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('runtime', 'wheel', 'wheelhouse', 'dependency-lock', 'compiler', 'out'):
        parser.add_argument('--'+name, type=Path, required=True)
    build(parser.parse_args())


if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    main()
