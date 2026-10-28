"""Native R11b packaging and offline acceptance. Never publishes a Release."""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

from scripts import build_macos_bundle as bundle

PRODUCT_COMMIT = 'aa53cf1f08e1fe48cacec8b93c36571892216bd5'
ROOT = Path(__file__).resolve().parents[1]


def run(*args):
    print('RUN ' + ' '.join(map(str, args)), flush=True)
    subprocess.run([str(x) for x in args], cwd=ROOT, check=True, timeout=900)


def download(url, path, expected):
    if path.exists():
        raise ValueError('Download target exists')
    run('curl', '--fail', '--location', '--show-error', '--connect-timeout', '30',
        '--max-time', '240', '--speed-limit', '1024', '--speed-time', '60',
        '--output', path, url)
    if bundle.digest(path) != expected:
        raise ValueError('Upstream archive hash mismatch: ' + path.name)
    print('Verified download: ' + path.name, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, type=Path)
    out = parser.parse_args().out.resolve()
    if sys.platform != 'darwin' or platform.machine() != 'arm64':
        raise ValueError('Native Apple Silicon runner required')
    if out.exists() or out.is_relative_to(ROOT):
        raise ValueError('Output must be new and outside the checkout')
    # Tooling may change; the shipped application and its metadata must not.
    run('git', 'diff', '--exit-code', PRODUCT_COMMIT, '--', 'src', 'pyproject.toml')
    out.mkdir()
    receipts, delivery = out / 'receipts', out / 'delivery'
    receipts.mkdir()
    delivery.mkdir()
    python_archive, node_archive = out / 'python.tar.gz', out / 'node.tar.gz'
    download(bundle.runtime_url('arm64'), python_archive, bundle.RUNTIMES['arm64']['sha256'])
    download(bundle.node_url('arm64'), node_archive, bundle.NODE_HASHES['arm64'])
    run(sys.executable, '-m', 'build', '--wheel', '--outdir', out / 'wheel')
    wheel, = (out / 'wheel').glob('*.whl')
    dsh = out / 'dsh'
    run(sys.executable, '-m', 'scripts.prepare_dsh_runtime', '--target', 'darwin-arm64',
        '--approved', ROOT / 'packaging/dsh/third-party-approved.json', '--out', dsh)
    bundle.build(SimpleNamespace(arch='arm64', out=out / 'bundle', runtime=python_archive,
        node_archive=node_archive, dsh=dsh, wheel=wheel, uv='uv',
        constraints=ROOT / 'packaging/macos/r11b-constraints.txt'))
    archive = out / 'bundle/ProjectWatch-macOS-arm64-candidate.zip'
    # Test the actual ZIP after relocation, including spaces and non-ASCII paths.
    relocated = out / 'Mac 安装验证'
    run('ditto', '-xk', archive, relocated)
    app = relocated / 'ProjectWatch.app'
    resources = app / 'Contents/Resources'
    app_python = resources / 'runtime/bin/python3.12'
    app_node = resources / 'node/bin/node'
    run(app_python, '-I', '-B', '-c',
        "import platform,sys; assert platform.machine()=='arm64'; print(sys.version)")
    run(app_node, '--version')
    run(app_python, '-I', '-B', ROOT / 'scripts/installed_wheel_smoke.py',
        '--wheel', wheel, '--full-journey', '--output', out / 'installed-journey')
    shutil.copyfile(out / 'installed-journey/receipt.json', receipts / 'installed-journey.json')
    # Test-only dependencies live outside the app, using its exact runtime/site-packages.
    test_env = out / 'test-env'
    run('uv', 'venv', '--python', app_python, '--system-site-packages', test_env)
    test_python = test_env / 'bin/python'
    run('uv', 'pip', 'install', '--python', test_python, 'pytest>=9.0.3,<10',
        'httpx>=0.28,<1', 'reportlab>=4,<5', 'xlwt>=1.3,<2', 'packaging>=24,<27')
    os.environ['PATH'] = str(app_node.parent) + os.pathsep + os.environ['PATH']
    run(test_python, '-I', '-B', ROOT / 'scripts/run_offline_tests.py',
        '--output', out / 'native-tests', '--dsh-runtime', resources / 'dsh',
        'tests/test_macos_bundle.py', 'tests/test_macos_developer.py',
        'tests/test_macos_executor.py', 'tests/test_studio_dsh_runtime.py',
        'tests/test_studio_dsh_monitor.py', 'tests/test_collection_model_continuation.py')
    shutil.copyfile(out / 'native-tests/pytest.xml', receipts / 'native-tests.xml')
    suites = ET.parse(receipts / 'native-tests.xml').getroot().iter('testsuite')
    counts = {key: 0 for key in ('tests', 'failures', 'errors', 'skipped')}
    for suite in suites:
        for key in counts:
            counts[key] += int(suite.get(key, 0))
    assert counts['tests'] and not counts['failures'] and not counts['errors']
    # Inventory the shipped environment, not the maintainer/test venv.
    code = '''import importlib.metadata as m,json,psycopg.pq
rows=[]
for d in m.distributions():
 rows.append({'name':d.metadata['Name'],'version':d.version,
 'license':d.metadata.get('License-Expression') or d.metadata.get('License'),
 'classifiers':[x for x in d.metadata.get_all('Classifier',[]) if x.startswith('License')],
 'license_files':[str(p) for p in d.files or [] if any(x in str(p).lower() for x in ('license','copying','notice'))]})
print(json.dumps({'dependencies':rows,'libpq_version':psycopg.pq.version()}))'''
    inventory = json.loads(subprocess.check_output([str(app_python), '-I', '-B', '-c', code], text=True))
    (receipts / 'dependency-inventory.json').write_text(json.dumps(inventory, indent=2), encoding='utf-8')
    requirements = receipts / 'audit-requirements.txt'
    requirements.write_text(''.join(f"{d['name']}=={d['version']}\n"
        for d in inventory['dependencies'] if d['name'] != 'projectwatch-local'), encoding='utf-8')
    run(sys.executable, '-m', 'pip_audit', '--no-deps', '--disable-pip',
        '-r', requirements, '-f', 'json', '-o', receipts / 'dependency-audit.json')
    # Confirm tests did not mutate any shipped bytes.
    manifest = json.loads((resources / 'bundle-manifest.json').read_text(encoding='utf-8'))
    assert all(bundle.digest(app / name) == digest for name, digest in manifest['files'].items())
    bundle.verify_archive(archive)
    name = 'ProjectWatch-macOS-arm64-0.2.0rc2-r11b-Preview.zip'
    shutil.copyfile(archive, delivery / name)
    source_name = 'ProjectWatch-macOS-r11b-build-source.zip'
    run('git', 'archive', '--format=zip', '--output', delivery / source_name, 'HEAD')
    readme = '''# ProjectWatch R11b Apple Silicon 预览包

适用于 M 系列 Mac（arm64）。内置 Python、Node、DSH，无需自行安装这些运行时。
解压后将 ProjectWatch.app 复制到“应用程序”并打开；替换旧版前先从菜单栏正常退出并备份。
数据保存在 ~/Library/Application Support/ProjectWatch-Desktop，替换应用不等于删除数据。

本包未做 Developer ID 签名和 Apple 公证。若系统拦截，请记录提示，不关闭 Gatekeeper。
完成 GitHub macOS 15 arm64 原生导入、离线合成安装流程、原生 DSH 和中文路径验证；
没有完成独立用户的 Finder 首次打开、菜单栏交互、休眠、真实付费服务验收。
plist 声明 macOS 13.0，但不能据此声称所有13+系统已验证。Intel 不支持此包。

产品源码与已发布 R11b 一致。构建源码附件另包含 Mac 打包流程与 Node 24.22.1 固定哈希。
Windows Release 已披露的分析/来源覆盖限制同样适用。源码使用 Community 1.0，非 OSI 开源。
许可证位于应用资源、Python dist-info 和 DSH notices；第三方对应源码见同一 Release 的附件。
'''
    (delivery / 'ProjectWatch-macOS-r11b-README.md').write_text(readme, encoding='utf-8')
    receipt = {'product_commit': PRODUCT_COMMIT,
        'build_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
        'platform': platform.platform(), 'arch': platform.machine(), 'tests': counts,
        'native_installed_journey': True, 'native_dsh_synthetic': True,
        'relocated_non_ascii_path': True, 'shipped_files_unchanged_after_tests': True,
        'real_provider_calls': 0, 'independent_gui_acceptance': False,
        'developer_id_signed': False, 'notarized': False,
        'product_wheel_sha256': bundle.digest(wheel), 'node': bundle.NODE_VERSION,
        'python': bundle.PYTHON_VERSION,
        'assets': [{'name': p.name, 'bytes': p.stat().st_size, 'sha256': bundle.digest(p)}
                   for p in sorted(delivery.iterdir())]}
    (receipts / 'verification.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    (delivery / 'ProjectWatch-macOS-r11b-SHA256SUMS.txt').write_text(
        ''.join(f"{a['sha256']}  {a['name']}\n" for a in receipt['assets']), encoding='utf-8')
    print(json.dumps(receipt))


if __name__ == '__main__':
    main()
