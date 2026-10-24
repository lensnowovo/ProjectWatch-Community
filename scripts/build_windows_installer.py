"""Compile a private per-user installer from a verified, complete Windows bundle."""
import argparse
import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path

from scripts.build_windows_bundle import digest, validate_output

NSIS_VERSION = '3.13'
NSIS_ZIP_SHA256 = 'ba63dffc4410ee89193e1cb5a41989991bd77c61068da17e3156d136b7b0b3d8'


def build(args):
    repo = Path(__file__).resolve().parents[1]
    out = args.out.resolve()
    validate_output(out, repo)
    helper = repo/'scripts/windows/maintain.py'
    spec = importlib.util.spec_from_file_location('install_maintenance', helper)
    maintenance = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(maintenance)
    bundle = args.bundle.resolve()
    value = maintenance.manifest(bundle)
    maintenance.verify(bundle, value)
    if not re.fullmatch(r'[A-Za-z0-9.+-]+', value['version']):
        raise ValueError('Invalid version')
    # Refuse cache/test residue; ship only the reviewed manifest and its files.
    actual = {p.relative_to(bundle).as_posix() for p in bundle.rglob('*') if p.is_file()}
    listed = set(value['files']) | {f'runtimes/{rid}/{name}' for rid, entry in value.get('runtimes', {}).items()
                                    for name in entry['files']}
    if actual != listed | {'bundle-manifest.json'}:
        raise ValueError('Bundle has unlisted files')
    nsis = args.nsis.resolve()
    if subprocess.check_output([str(nsis), '/VERSION'], text=True).strip() != 'v'+NSIS_VERSION:
        raise ValueError('Unexpected NSIS version')
    settings = {'INSTALL_ROOT': '$LOCALAPPDATA\\Programs\\ProjectWatch',
                'DATA_HOME': '$LOCALAPPDATA\\ProjectWatch-Desktop',
                'MENU_DIR': '$SMPROGRAMS\\ProjectWatch', 'DESKTOP_DIR': '$DESKTOP',
                'REG_KEY': 'Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\ProjectWatchDesktop',
                'MUTEX': 'Local\\ProjectWatchDesktopInstaller'}
    if args.test_root:
        test = args.test_root.resolve()
        validate_output(test, repo)
        # Test builds have all destinations sealed at compile-time. No user-home flag.
        for key, suffix in [('INSTALL_ROOT', 'program'), ('DATA_HOME', 'data'),
                            ('MENU_DIR', 'start-menu'), ('DESKTOP_DIR', 'desktop')]:
            settings[key] = str(test/suffix)
        token = __import__('hashlib').sha256(str(test).encode()).hexdigest()[:16]
        settings['REG_KEY'] = 'Software\\ProjectWatchInstallerTests\\'+token
        settings['MUTEX'] = 'Local\\ProjectWatchInstallerTest-'+token
        settings['TEST_ROOT'] = str(test)
    out.mkdir()
    # Freeze compiler inputs: a developer editing the source during compression
    # must not create a mixed installer or invalidate NSIS's source-file offsets.
    frozen_script = out/'Installer.nsi'
    frozen_helper = out/'maintain.py'
    shutil.copy2(repo/'scripts/windows/Installer.nsi', frozen_script)
    shutil.copy2(helper, frozen_helper)
    artifact = out/'ProjectWatch-Setup.exe'
    settings.update(OUTPUT=str(artifact), BUNDLE=str(bundle), HELPER=str(frozen_helper), VERSION=value['version'])
    # Shared runtimes are extracted only when the target install lacks a complete copy.
    for kind in ('dsh', 'node'):
        rid = (value.get('dsh') or {}).get(kind+'_runtime_id')
        if rid:
            if rid not in value.get('runtimes', {}):
                raise ValueError('Runtime id missing from inventory')
            settings[kind.upper()+'_RUNTIME_ID'] = rid
    if any('"' in v or '\n' in v for v in settings.values()):
        raise ValueError('Unsafe compiler path')
    command = [str(nsis), '/NOCONFIG', '/INPUTCHARSET', 'UTF8', '/V2', *['/D'+k+'='+v for k, v in settings.items()],
               str(frozen_script)]
    subprocess.run(command, check=True)
    receipt = {'format': 'projectwatch-installer-build-v1', 'version': value['version'],
               'sha256': digest(artifact), 'bytes': artifact.stat().st_size,
               'bundle_manifest_sha256': digest(bundle/'bundle-manifest.json'),
               'installer_script_sha256': digest(frozen_script), 'helper_sha256': digest(frozen_helper),
               'nsis_version': NSIS_VERSION, 'nsis_distribution_sha256': NSIS_ZIP_SHA256,
               'isolated_test_build': bool(args.test_root), 'publication_authorized': False}
    (out/'installer-build.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    print(json.dumps(receipt))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('bundle', 'nsis', 'out'):
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--test-root', type=Path)
    build(parser.parse_args())
