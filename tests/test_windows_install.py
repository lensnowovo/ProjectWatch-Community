"""Offline synthetic installer transactions; never touches a real data home."""
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from scripts.windows.maintain import digest, install, lock, manifest, safe, uninstall


def test_installer_exposes_and_remembers_directory_without_overriding_uninstaller():
    script = (Path(__file__).resolve().parents[1]/'scripts/windows/Installer.nsi').read_text(encoding='utf-8')
    assert '!insertmacro MUI_PAGE_DIRECTORY' in script
    assert 'InstallDirRegKey HKCU "${REG_KEY}" "InstallLocation"' in script
    guard = script.split('!macro GuardInstaller', 1)[1].split('!macroend', 1)[0]
    assert 'StrCpy $INSTDIR' not in guard
    assert 'Call ValidateInstallLocation' in script
    assert '!ifdef TEST_ROOT' in script
    assert 'kernel32::GetFullPathNameW' in script
    assert 'GetFullPathName $INSTDIR' not in script
    builder = (Path(__file__).resolve().parents[1]/'scripts/build_windows_installer.py').read_text(encoding='utf-8')
    assert "'/INPUTCHARSET', 'UTF8'" in builder


def test_nsis_payload_extraction_uses_extended_paths_and_retains_failure_evidence():
    script = (Path(__file__).resolve().parents[1]/'scripts/windows/Installer.nsi').read_text(encoding='utf-8')
    core = script.split('Section "ProjectWatch（完整功能）" Core', 1)[1].split('SectionEnd', 1)[0]
    for suffix in ('', '\\runtimes\\${DSH_RUNTIME_ID}', '\\runtimes\\${NODE_RUNTIME_ID}'):
        assert 'SetOutPath "\\\\?\\$INSTDIR\\.payload'+suffix+'"' in core
    failure = core.split('IfErrors 0 +2', 1)[1].split('  ${EndIf}', 1)[0]
    assert 'installer-result.json' in failure
    assert 'RMDir /r' not in failure
    assert core.index('RMDir /r') > core.index('未完成安装')


def test_custom_chinese_directory_upgrade_uninstall_preserves_data(tmp_path):
    root = tmp_path/'自选 软件目录'/'ProjectWatch'
    home = home_at(tmp_path)
    before = hashes(home)
    install(payload(tmp_path), root, home)
    install(payload(tmp_path, 'two'), root, home)
    assert uninstall(root, home)['data_preserved']
    assert hashes(home) == before
    assert not (root/'ProjectWatch.exe').exists()


def payload(tmp_path, tag='one'):
    folder = tmp_path/tag
    (folder/'runtime').mkdir(parents=True)
    (folder/'ProjectWatch.exe').write_bytes(b'synthetic fixed launcher')
    (folder/'runtime/pythonw.exe').write_bytes(tag.encode())
    files = {p.relative_to(folder).as_posix(): digest(p) for p in folder.rglob('*') if p.is_file()}
    (folder/'bundle-manifest.json').write_text(json.dumps({'format': 'projectwatch-windows-bundle-v1',
                                                         'version': '0.2.0a1', 'files': files}))
    return folder


def home_at(tmp_path):
    home = tmp_path/'data'
    home.mkdir()
    (home/'settings.json').write_text('synthetic secret')
    (home/'original.txt').write_text('synthetic original')
    with sqlite3.connect(home/'projectwatch.sqlite3') as db:
        db.execute('create table projects (name text, paused integer)')
        db.execute("insert into projects values ('synthetic', 1)")
    return home


def hashes(home):
    return {p.name: digest(p) for p in home.iterdir() if p.is_file() and p.name != '.studio.lock'}


def test_install_upgrade_uninstall_preserves_data_and_old_version(tmp_path):
    one, two, home, root = payload(tmp_path), payload(tmp_path, 'two'), home_at(tmp_path), tmp_path/'program'
    before = hashes(home)
    first = install(one, root, home)
    second = install(two, root, home)
    assert first['active'] != second['active']
    assert (root/'versions'/first['active']/'runtime/pythonw.exe').read_bytes() == b'one'
    state = json.loads((root/'install-state.json').read_text())
    backup = __import__('pathlib').Path(state['backup'])
    assert (backup/'original.txt').read_text() == 'synthetic original'
    with sqlite3.connect(backup/'projectwatch.sqlite3') as db:
        assert db.execute('select paused from projects').fetchone() == (1,)
    result = uninstall(root, home)
    assert result['data_preserved']
    assert hashes(home) == before
    assert not (root/'ProjectWatch.exe').exists()


def test_uninstall_removes_listed_files_by_name_and_keeps_unlisted(tmp_path, monkeypatch):
    from scripts.windows import maintain
    data, root = home_at(tmp_path), tmp_path/'program'
    result = install(payload(tmp_path), root, data)
    folder = root/'versions'/result['active']
    (folder/'runtime/pythonw.exe').write_bytes(b'changed listed file')
    (folder/'my-document.txt').write_text('keep')
    reads = []
    real = maintain.digest
    monkeypatch.setattr(maintain, 'digest', lambda path: reads.append(path) or real(path))
    assert uninstall(root, data)['retained_files'] > 0
    # Listed program files are removed without reading their content; the launcher is the one check.
    assert all(Path(str(p)).name == 'ProjectWatch.exe' for p in reads)
    assert not (folder/'runtime/pythonw.exe').exists()
    assert (folder/'my-document.txt').read_text() == 'keep'


@pytest.mark.parametrize('relative', ['desktop-session/.studio.lock', '.studio.lock'])
def test_running_home_blocks_upgrade_and_uninstall(tmp_path, relative):
    data, root = home_at(tmp_path), tmp_path/'program'
    install(payload(tmp_path), root, data)
    before = (root/'install-state.json').read_bytes()
    two = payload(tmp_path, 'two')
    with lock(data/relative):
        with pytest.raises(OSError):
            install(two, root, data)
        with pytest.raises(OSError):
            uninstall(root, data)
    assert (root/'install-state.json').read_bytes() == before


def test_bad_payload_never_switches_pointer(tmp_path):
    data, root = home_at(tmp_path), tmp_path/'program'
    install(payload(tmp_path), root, data)
    before = (root/'install-state.json').read_bytes()
    two = payload(tmp_path, 'two')
    (two/'runtime/pythonw.exe').write_bytes(b'bad')
    with pytest.raises(ValueError):
        install(two, root, data)
    assert (root/'install-state.json').read_bytes() == before


@pytest.mark.parametrize('name', ['../data', '/data', 'runtime\\data', 'runtime/a:stream', 'runtime/A.', 'runtime/../data'])
def test_unsafe_inventory(tmp_path, name):
    folder = payload(tmp_path)
    (folder/'bundle-manifest.json').write_text(json.dumps({'format': 'projectwatch-windows-bundle-v1',
                                                         'files': {name: hashlib.sha256(b'x').hexdigest()}}))
    with pytest.raises(ValueError):
        manifest(folder)


def test_nonowned_and_overlapping_directories_rejected(tmp_path):
    data = home_at(tmp_path)
    bundle = payload(tmp_path)
    for root in (data, data/'program', tmp_path):
        with pytest.raises(ValueError):
            install(bundle, root, data)


def test_failed_backup_keeps_active_pointer_and_original(tmp_path, monkeypatch):
    from scripts.windows import maintain
    data, root = home_at(tmp_path), tmp_path/'program'
    install(payload(tmp_path), root, data)
    before, original = (root/'install-state.json').read_bytes(), hashes(data)
    def fail(*_args):
        raise OSError('synthetic disk full')
    monkeypatch.setattr(maintain, 'snapshot', fail)
    with pytest.raises(OSError):
        install(payload(tmp_path, 'two'), root, data)
    assert (root/'install-state.json').read_bytes() == before
    assert hashes(data) == original


def test_changed_launcher_requires_agent(tmp_path):
    data, root = home_at(tmp_path), tmp_path/'program'
    install(payload(tmp_path), root, data)
    (root/'ProjectWatch.exe').write_bytes(b'my custom launcher')
    before = (root/'install-state.json').read_bytes()
    with pytest.raises(ValueError, match='Customized'):
        install(payload(tmp_path, 'two'), root, data)
    assert (root/'install-state.json').read_bytes() == before
    assert (root/'ProjectWatch.exe').read_bytes() == b'my custom launcher'


def test_corrupt_database_blocks_upgrade(tmp_path):
    data, root = home_at(tmp_path), tmp_path/'program'
    install(payload(tmp_path), root, data)
    before = (root/'install-state.json').read_bytes()
    (data/'projectwatch.sqlite3').write_bytes(b'broken synthetic sqlite')
    with pytest.raises(sqlite3.DatabaseError):
        install(payload(tmp_path, 'two'), root, data)
    assert (root/'install-state.json').read_bytes() == before


def test_failed_staging_can_be_retried_without_deleting_unknown_files(tmp_path, monkeypatch):
    from scripts.windows import maintain
    data, root = home_at(tmp_path), tmp_path/'program'
    bundle = payload(tmp_path)
    real_place = maintain.place
    def fail_stage(source, destination, files, *, move):
        raise OSError('synthetic interruption')
    monkeypatch.setattr(maintain, 'place', fail_stage)
    with pytest.raises(OSError):
        install(bundle, root, data)
    assert not (root/'install-state.json').exists()
    (root/'keep.txt').write_text('user file')
    monkeypatch.setattr(maintain, 'place', real_place)
    install(bundle, root, data)
    assert (root/'keep.txt').read_text() == 'user file'


def test_downgrade_is_not_a_safe_data_rollback(tmp_path):
    data, root = home_at(tmp_path), tmp_path/'program'
    install(payload(tmp_path), root, data)
    older = payload(tmp_path, 'older')
    path = older/'bundle-manifest.json'
    value = json.loads(path.read_text())
    value['version'] = '0.1.0'
    path.write_text(json.dumps(value))
    before = (root/'install-state.json').read_bytes()
    with pytest.raises(ValueError, match='Downgrade'):
        install(older, root, data)
    assert (root/'install-state.json').read_bytes() == before


def test_deep_dependency_survives_activation_and_is_removed_on_uninstall(tmp_path):
    data, root = home_at(tmp_path), tmp_path/'program'
    bundle = payload(tmp_path)
    name = 'runtime/Lib/site-packages/'+('a'*60)+'/'+('b'*60)+'/long-dependency.py'
    path = safe(bundle/name)
    path.parent.mkdir(parents=True)
    path.write_bytes(b'synthetic dependency')
    value = json.loads((bundle/'bundle-manifest.json').read_text())
    value['files'][name] = digest(path)
    (bundle/'bundle-manifest.json').write_text(json.dumps(value))
    result = install(bundle, root, data)
    installed = safe(root/'versions'/result['active']/name)
    assert len(str(installed)) > 260
    assert installed.read_bytes() == b'synthetic dependency'
    assert uninstall(root, data)['retained_files'] == 0
    assert not installed.exists()



# ---------------------------------------------------------------- shared runtimes
from scripts.windows.maintain import runtime_digest  # noqa: E402


def with_runtime(tmp_path, tag='one', content=b'synthetic dsh', where=None):
    """A payload carrying a shared runtime, laid out as the bundle build writes it."""
    folder = where or tmp_path/tag
    (folder/'runtime').mkdir(parents=True)
    (folder/'ProjectWatch.exe').write_bytes(b'synthetic fixed launcher')
    (folder/'runtime/pythonw.exe').write_bytes(tag.encode())
    files = {p.relative_to(folder).as_posix(): digest(p) for p in folder.rglob('*') if p.is_file()}
    staged = folder/'runtimes/tmp'
    (staged/'node_modules/x').mkdir(parents=True)
    (staged/'node_modules/x/index.js').write_bytes(content)
    (staged/'LICENSE').write_text('synthetic license')
    rfiles = {p.relative_to(staged).as_posix(): digest(p) for p in staged.rglob('*') if p.is_file()}
    rid = 'dsh-0.2.1-rc.2-' + runtime_digest(rfiles)[:16]  # Version labels may contain hyphens.
    staged.rename(folder/'runtimes'/rid)
    (folder/'bundle-manifest.json').write_text(json.dumps({
        'format': 'projectwatch-windows-bundle-v1', 'version': '0.2.0a1', 'files': files,
        'dsh': {'dsh_runtime_id': rid}, 'runtimes': {rid: {'files': rfiles}}}))
    return folder, rid


def count_reads(monkeypatch):
    from scripts.windows import maintain
    reads = []
    real = maintain.digest
    monkeypatch.setattr(maintain, 'digest', lambda path: reads.append(str(path)) or real(path))
    return reads


def test_shared_runtime_is_published_complete_and_outside_the_version(tmp_path):
    data, root = home_at(tmp_path), tmp_path/'program'
    bundle, rid = with_runtime(tmp_path)
    result = install(bundle, root, data)
    shared = root/'runtimes'/rid
    assert (shared/'node_modules/x/index.js').read_bytes() == b'synthetic dsh'
    assert (shared/'.complete').read_text() == runtime_digest(json.loads((bundle/'bundle-manifest.json').read_text())['runtimes'][rid]['files'])
    assert not (root/'versions'/result['active']/'runtimes').exists()
    assert not list((root/'runtimes').glob('staging-*'))


def test_unchanged_runtime_is_reused_untouched_even_when_not_extracted(tmp_path, monkeypatch):
    data, root = home_at(tmp_path), tmp_path/'program'
    one, rid = with_runtime(tmp_path)
    install(one, root, data)
    shared = root/'runtimes'/rid/'node_modules/x/index.js'
    before = shared.stat().st_mtime_ns
    two, rid_two = with_runtime(tmp_path, 'two')
    assert rid_two == rid
    import shutil
    shutil.rmtree(two/'runtimes')  # The installer skips extracting a runtime that is already complete.
    reads = count_reads(monkeypatch)
    second = install(two, root, data)
    assert second['active'] != one.name
    assert not any('runtimes' in r for r in reads)
    assert shared.stat().st_mtime_ns == before


def test_changed_runtime_installs_beside_the_old_one(tmp_path):
    data, root = home_at(tmp_path), tmp_path/'program'
    one, rid_one = with_runtime(tmp_path)
    two, rid_two = with_runtime(tmp_path, 'two', content=b'newer dsh')
    install(one, root, data)
    install(two, root, data)
    assert rid_one != rid_two
    assert (root/'runtimes'/rid_one/'.complete').exists() and (root/'runtimes'/rid_two/'.complete').exists()


def test_each_new_file_is_hashed_once_and_moved_on_the_same_volume(tmp_path, monkeypatch):
    data, root = home_at(tmp_path), tmp_path/'program'
    root.mkdir()
    bundle, rid = with_runtime(tmp_path, where=root/'.payload')  # As the installer extracts it.
    value = json.loads((bundle/'bundle-manifest.json').read_text())
    listed = len(value['files']) + len(value['runtimes'][rid]['files'])
    reads = count_reads(monkeypatch)
    install(bundle, root, data, move=True)
    # Program files only (the data backup hashes its own copies), launcher checks aside.
    program = str(safe(root)) + '\\'
    file_reads = [r for r in reads if r.startswith(program) and not r.endswith(('ProjectWatch.exe', '.tmp', 'bundle-manifest.json'))]
    assert len(file_reads) == listed - 1  # every listed file once (the launcher is checked separately)
    assert not (bundle/'runtime/pythonw.exe').exists() and not (bundle/'runtimes'/rid/'LICENSE').exists()


def test_corrupt_runtime_never_publishes_or_switches(tmp_path):
    data, root = home_at(tmp_path), tmp_path/'program'
    install(payload(tmp_path), root, data)
    before = (root/'install-state.json').read_bytes()
    bundle, rid = with_runtime(tmp_path, 'two')
    (bundle/'runtimes'/rid/'LICENSE').write_text('tampered')
    with pytest.raises(ValueError, match='hash'):
        install(bundle, root, data)
    assert (root/'install-state.json').read_bytes() == before
    assert not (root/'runtimes'/rid).exists()


def test_interrupted_runtime_publish_is_set_aside_not_trusted(tmp_path):
    data, root = home_at(tmp_path), tmp_path/'program'
    bundle, rid = with_runtime(tmp_path)
    leftover = root/'runtimes'/rid
    leftover.mkdir(parents=True)
    (leftover/'partial.js').write_text('incomplete')
    (root/'install-owner.json').parent.mkdir(exist_ok=True)
    from scripts.windows.maintain import display
    (root/'install-owner.json').write_text(json.dumps({'format': 'projectwatch-desktop-owner-v1',
                                                        'root': display(safe(root)), 'home': display(safe(data))}))
    install(bundle, root, data)
    assert (root/'runtimes'/rid/'.complete').exists() and not (root/'runtimes'/rid/'partial.js').exists()
    assert [p.read_text() for p in root.glob('runtimes/abandoned-*/partial.js')] == ['incomplete']


def test_uninstall_removes_shared_runtimes_but_keeps_unlisted_files(tmp_path):
    data, root = home_at(tmp_path), tmp_path/'program'
    bundle, rid = with_runtime(tmp_path)
    install(bundle, root, data)
    (root/'runtimes'/rid/'my-note.txt').write_text('keep')
    result = uninstall(root, data)
    assert result['retained_files'] == 1
    assert (root/'runtimes'/rid/'my-note.txt').read_text() == 'keep'
    assert not (root/'runtimes'/rid/'node_modules/x/index.js').exists()


def test_new_install_directory_may_hold_only_the_installer_payload(tmp_path):
    data, root = home_at(tmp_path), tmp_path/'program'
    root.mkdir()
    bundle, _ = with_runtime(tmp_path, where=root/'.payload')
    install(bundle, root, data, move=True)
    assert (root/'install-state.json').exists()
