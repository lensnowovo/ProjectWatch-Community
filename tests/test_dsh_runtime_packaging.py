"""Bundled DSH runtime preparation; synthetic trees only, no npm, Node or network."""
import hashlib
import json
import shutil
import subprocess
from zipfile import ZipFile

import pytest

from scripts import prepare_dsh_runtime as prep
from scripts.build_windows_bundle import NODE_VERSION, extract_node


def package(root, rel, name, version, license='MIT', files=()):
    path = root/rel
    path.mkdir(parents=True, exist_ok=True)
    (path/'package.json').write_text(json.dumps({'name': name, 'version': version, 'license': license}))
    for f in files:
        (path/f).write_text('synthetic ' + f)
    return path


def tree(tmp_path):
    modules = tmp_path/'node_modules'
    package(modules, '@deepseek-ai/dsh', '@deepseek-ai/dsh', '1.0.0', files=['LICENSE', 'index.js', 'index.d.ts', 'README.md'])
    package(modules, 'a', 'a', '1.0.0', files=['LICENSE', 'a.js.map'])
    package(modules, 'a/node_modules/@s/b', '@s/b', '2.0.0', files=['LICENSE.md'])
    package(modules, 'unused', 'unused', '1.0.0', files=['LICENSE'])
    (modules/'a/dist').mkdir()
    (modules/'a/dist/package.json').write_text(json.dumps({'type': 'module'}))
    lock = {'packages': {'': {}, 'node_modules/@deepseek-ai/dsh': {'version': '1.0.0'},
                         'node_modules/a': {'version': '1.0.0'},
                         'node_modules/a/node_modules/@s/b': {'version': '2.0.0'},
                         'node_modules/unused': {'version': '1.0.0'},
                         'node_modules/other-os-native': {'version': '1.0.0', 'optional': True}}}
    rules = {'remove_packages': ['unused'], 'remove_paths': [], 'remove_suffixes': ['.map', '.d.ts', '.md'],
             'keep_prefixes': ['license']}
    return modules, lock, rules


def test_scoped_and_nested_packages_are_found_without_inner_manifests(tmp_path):
    modules, _, _ = tree(tmp_path)
    assert prep.installed_packages(modules) == {
        '@deepseek-ai/dsh': '1.0.0', 'a': '1.0.0', 'a/node_modules/@s/b': '2.0.0', 'unused': '1.0.0'}


def test_prune_keeps_license_files_and_removes_listed_parts(tmp_path):
    modules, lock, rules = tree(tmp_path)
    prep.prune(modules, rules)
    assert not (modules/'unused').exists()
    assert not (modules/'a/a.js.map').exists() and not (modules/'@deepseek-ai/dsh/index.d.ts').exists()
    assert not (modules/'@deepseek-ai/dsh/README.md').exists()
    assert (modules/'a/node_modules/@s/b/LICENSE.md').exists()
    # Absent optional packages for other platforms and pruned packages are expected.
    assert set(prep.verify_against_lock(modules, lock, rules)) == {'@deepseek-ai/dsh', 'a', 'a/node_modules/@s/b'}


@pytest.mark.parametrize('fault', ['version', 'unlocked', 'missing'])
def test_install_must_match_the_lock_exactly(tmp_path, fault):
    modules, lock, rules = tree(tmp_path)
    if fault == 'version':
        package(modules, 'a', 'a', '1.0.1')
    elif fault == 'unlocked':
        package(modules, 'stray', 'stray', '1.0.0')
    else:
        import shutil
        shutil.rmtree(modules/'a/node_modules/@s/b')
    with pytest.raises(ValueError):
        prep.verify_against_lock(modules, lock, rules)


def test_notices_flag_missing_texts_and_non_permissive_licenses(tmp_path):
    modules, _, _ = tree(tmp_path)
    package(modules, 'bare', 'bare', '1.0.0')
    package(modules, 'copyleft', 'copyleft', '1.0.0', license='GPL-3.0-only', files=['LICENSE'])
    package(modules, 'a/benchmark', 'benchmark', '1.0.0', license='ISC')  # a folder inside a package
    report = prep.notices(modules, tmp_path/'NOTICES.txt', upstream={})
    reasons = {(r['package'], r['reason']) for r in report['review']}
    assert ('bare@1.0.0', 'no_license_text') in reasons
    assert ('copyleft@1.0.0', 'not_on_permissive_list') in reasons
    assert ('a@1.0.0', 'no_license_text') not in reasons
    assert not any(r['package'] == 'benchmark@1.0.0' for r in report['review'])
    text = (tmp_path/'NOTICES.txt').read_text(encoding='utf-8')
    assert 'synthetic LICENSE' in text and 'Declared license: MIT' in text


def prepared(tmp_path, review_open=0):
    root = tmp_path/'prepared'
    root.mkdir()
    (root/'a.js').write_text('synthetic')
    files = prep.file_manifest(root)
    manifest = {'format': 'projectwatch-dsh-runtime-v1', 'dsh_version': prep.dsh_version(),
                'lock_sha256': prep.digest(prep.PACKAGING/'package-lock.json'),
                'prune_sha256': prep.digest(prep.PACKAGING/'prune.json'),
                'third_party_review_open': review_open, 'files': files}
    (root/prep.MANIFEST).write_text(json.dumps(manifest))
    return root


def test_bundle_accepts_only_the_listed_prepared_tree(tmp_path):
    root = prepared(tmp_path)
    assert prep.verify_prepared(root)['dsh_version'] == prep.dsh_version()
    (root/'a.js').write_text('changed')
    with pytest.raises(ValueError, match='changed'):
        prep.verify_prepared(root)
    (root/'a.js').write_text('synthetic')
    (root/'extra.js').write_text('synthetic')
    with pytest.raises(ValueError, match='unlisted'):
        prep.verify_prepared(root)


def test_open_license_review_blocks_the_bundle(tmp_path):
    with pytest.raises(ValueError, match='review'):
        prep.verify_prepared(prepared(tmp_path, review_open=3))


def test_prepare_refuses_output_inside_source(tmp_path):
    with pytest.raises(ValueError, match='outside'):
        prep.prepare(prep.REPO/'dsh-out', installed=None, npm=None, approved_resolutions=set())


def node_zip(tmp_path, names):
    archive = tmp_path/'node.zip'
    with ZipFile(archive, 'w') as z:
        for name in names:
            z.writestr(name, b'synthetic')
    return archive, hashlib.sha256(archive.read_bytes()).hexdigest()


def test_node_archive_is_pinned_and_only_the_executable_ships(tmp_path):
    prefix = f'node-{NODE_VERSION}-win-x64/'
    archive, digest = node_zip(tmp_path, [prefix + 'node.exe', prefix + 'LICENSE', prefix + 'npm.cmd'])
    with pytest.raises(ValueError, match='not pinned'):
        extract_node(archive, tmp_path/'node', '')
    with pytest.raises(ValueError, match='mismatch'):
        extract_node(archive, tmp_path/'node', '0' * 64)
    assert not (tmp_path/'node').exists()
    extract_node(archive, tmp_path/'node', digest)
    assert sorted(p.name for p in (tmp_path/'node').iterdir()) == ['LICENSE', 'node.exe']


def test_unexpected_node_layout_is_refused(tmp_path):
    archive, digest = node_zip(tmp_path, ['node.exe'])
    with pytest.raises(ValueError, match='layout'):
        extract_node(archive, tmp_path/'node', digest)


def test_upstream_text_is_used_but_still_needs_approval(tmp_path):
    modules, _, _ = tree(tmp_path)
    package(modules, 'bare', 'bare', '1.0.0')
    supplied = {'bare@1.0.0': {'kind': 'upstream_file', 'source': 'https://example.invalid/LICENSE',
                               'text': 'Synthetic upstream license text'}}
    report = prep.notices(modules, tmp_path/'NOTICES.txt', upstream=supplied)
    assert {'package': 'bare@1.0.0', 'license': 'MIT', 'reason': 'upstream_file',
            'source': 'https://example.invalid/LICENSE', 'note': ''} in report['review']
    text = (tmp_path/'NOTICES.txt').read_text(encoding='utf-8')
    assert 'Synthetic upstream license text' in text and 'Not shipped in the package' in text


def test_committed_upstream_texts_match_their_recorded_hashes():
    entries = prep.upstream_texts()
    assert entries and all(e['text'].strip() and e['source'].startswith('https://') for e in entries.values())


def test_prepared_runtime_cannot_cross_target_platforms(tmp_path):
    root = prepared(tmp_path)
    # Existing v1 artifacts are Windows-only, never implicitly re-labelled Darwin.
    assert prep.verify_prepared(root, target='win32-x64')['dsh_version']
    with pytest.raises(ValueError, match='target'):
        prep.verify_prepared(root, target='darwin-arm64')


def test_target_rules_keep_required_mac_native_addons():
    rules = prep.target_rules('darwin-arm64')
    assert '@img/sharp-darwin-arm64' in rules['remove_packages']
    for name in prep.required_native_packages('darwin-arm64'):
        assert name not in rules['remove_packages']
    with pytest.raises(ValueError, match='target'):
        prep.target_rules('linux-x64')


def test_windows_native_names_follow_upstream_not_darwin_conventions():
    assert prep.required_native_packages('win32-x64') == (
        '@koromix/koffi-win32-x64', 'node-addon-require-builtin-win32-x64-msvc',
    )


@pytest.mark.parametrize('target', prep.TARGETS)
def test_required_native_packages_exist_in_pinned_upstream_lock(target):
    entries = prep.lock_entries(json.loads((prep.PACKAGING / 'package-lock.json').read_text()))
    platform, cpu = prep.target_parts(target)
    for name in prep.required_native_packages(target):
        assert name in entries, f'{target}: {name} does not exist upstream'
        assert platform in entries[name]['os']
        assert cpu in entries[name]['cpu']
        assert name not in prep.target_rules(target)['remove_packages']


@pytest.mark.parametrize('missing', [None, 'koffi', 'loader'])
def test_windows_requires_both_native_binaries(tmp_path, missing):
    modules = tmp_path / 'node_modules'
    for key, name in [('koffi', '@koromix/koffi-win32-x64'),
                      ('loader', 'node-addon-require-builtin-win32-x64-msvc')]:
        package(modules, name, name, '1.0.0', files=[] if missing == key else ['addon.node'])
    if missing:
        with pytest.raises(ValueError, match='Required native package missing'):
            prep.verify_target_packages(modules, 'win32-x64')
    else:
        prep.verify_target_packages(modules, 'win32-x64')


def test_mac_target_requires_native_optional_packages(tmp_path):
    modules = tmp_path / 'node_modules'
    modules.mkdir()
    with pytest.raises(ValueError, match='native'):
        prep.verify_target_packages(modules, 'darwin-arm64')
    for name in prep.required_native_packages('darwin-arm64'):
        package(modules, name, name, '1.0.0', files=['LICENSE', 'addon.node'])
    prep.verify_target_packages(modules, 'darwin-arm64')


def test_cross_install_command_has_explicit_os_cpu_and_no_scripts():
    command = prep.install_command('npm', 'darwin-arm64')
    assert '--os=darwin' in command and '--cpu=arm64' in command
    assert '--ignore-scripts' in command and '--bin-links=false' in command
    assert '--registry=https://registry.npmjs.org/' in command


def test_wrong_platform_optional_native_package_is_not_accepted(tmp_path):
    modules = tmp_path / 'node_modules'
    for name in prep.required_native_packages('darwin-arm64'):
        package(modules, name, name, '1.0.0', files=['addon.node'])
    wrong = package(modules, 'foreign', 'foreign', '1.0.0', files=['addon.node'])
    (wrong / 'package.json').write_text(json.dumps({
        'name': 'foreign', 'version': '1.0.0', 'os': ['win32'], 'cpu': ['x64'],
    }))
    with pytest.raises(ValueError, match='target mismatch'):
        prep.verify_target_packages(modules, 'darwin-arm64')


def test_missing_license_review_result_fails_closed(tmp_path):
    root = prepared(tmp_path)
    manifest = json.loads((root / prep.MANIFEST).read_text())
    del manifest['third_party_review_open']
    (root / prep.MANIFEST).write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='review'):
        prep.verify_prepared(root)


def test_target_prune_digest_is_line_ending_independent(tmp_path, monkeypatch):
    content = (prep.PACKAGING / 'prune-darwin.json').read_text(encoding='utf-8')
    expected = prep.target_prune_digest('darwin-arm64')
    (tmp_path / 'prune-darwin.json').write_bytes(content.replace('\n', '\r\n').encode())
    monkeypatch.setattr(prep, 'PACKAGING', tmp_path)
    assert prep.target_prune_digest('darwin-arm64') == expected


def test_changed_license_text_is_still_rejected(tmp_path):
    (tmp_path / 'LICENSE.txt').write_bytes(b'altered')
    (tmp_path / 'index.json').write_text(json.dumps({'a@1': {
        'file': 'LICENSE.txt', 'sha256': hashlib.sha256(b'original').hexdigest(),
    }}))
    with pytest.raises(ValueError, match='license text changed'):
        prep.upstream_texts(tmp_path)


def test_git_checkout_keeps_pinned_license_and_lock_bytes(tmp_path):
    """A fresh LF-default checkout must not silently re-encode byte-pinned inputs."""
    git = shutil.which('git')
    if not git:
        pytest.skip('git required for checkout contract')
    repo, output = tmp_path / 'repo', tmp_path / 'checkout'
    repo.mkdir()
    shutil.copyfile(prep.REPO / '.gitattributes', repo / '.gitattributes')
    shutil.copytree(prep.PACKAGING, repo / 'packaging/dsh')
    for arguments in (['init', '--quiet'], ['-c', 'core.autocrlf=false', 'add', '.'],
                      ['-c', 'core.autocrlf=false', '-c', 'core.eol=lf', 'checkout-index', '--all',
                       '--prefix=' + output.as_posix() + '/']):
        subprocess.run([git, '-C', str(repo), *arguments], check=True, capture_output=True)
    checked = output / 'packaging/dsh'
    for name in ('package-lock.json', 'prune.json'):
        assert prep.digest(checked / name) == prep.digest(prep.PACKAGING / name)
    assert prep.upstream_texts(checked / 'upstream-licenses')


def test_git_tree_keeps_license_bytes_without_checkout_conversion(tmp_path):
    """Source ZIPs/exporters read blobs, without checkout's eol conversion."""
    git = shutil.which('git')
    if not git:
        pytest.skip('git required for source archive contract')
    repo = tmp_path / 'repo'
    repo.mkdir()
    shutil.copyfile(prep.REPO / '.gitattributes', repo / '.gitattributes')
    shutil.copytree(prep.PACKAGING, repo / 'packaging/dsh')
    for arguments in (['init', '--quiet'], ['-c', 'core.autocrlf=false', 'add', '.']):
        subprocess.run([git, '-C', str(repo), *arguments], check=True, capture_output=True)
    entries = prep.upstream_texts(prep.PACKAGING / 'upstream-licenses')
    for key, entry in entries.items():
        blob = subprocess.check_output([git, '-C', str(repo), 'show',
                                        ':packaging/dsh/upstream-licenses/'+entry['file']])
        assert hashlib.sha256(blob).hexdigest() == entry['sha256'], key


@pytest.mark.parametrize('newline', [b'\n', b'\r\n'])
def test_legacy_config_hash_accepts_only_checkout_newlines(tmp_path, newline):
    root = prepared(tmp_path)
    manifest = json.loads((root / prep.MANIFEST).read_text())
    for field, name in [('lock_sha256', 'package-lock.json'), ('prune_sha256', 'prune.json')]:
        content = (prep.PACKAGING / name).read_bytes().replace(b'\r\n', b'\n')
        manifest[field] = hashlib.sha256(content.replace(b'\n', newline)).hexdigest()
    (root / prep.MANIFEST).write_text(json.dumps(manifest))
    assert prep.verify_prepared(root)['dsh_version']
    manifest['lock_sha256'] = hashlib.sha256(b'{"changed": true}').hexdigest()
    (root / prep.MANIFEST).write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='different lock'):
        prep.verify_prepared(root)


def test_new_config_hash_uses_lf_only(tmp_path):
    file = tmp_path / 'config.json'
    file.write_bytes(b'{\r\n "version": 1\r\n}\r\n')
    assert prep.config_digest(file) == hashlib.sha256(b'{\n "version": 1\n}\n').hexdigest()
