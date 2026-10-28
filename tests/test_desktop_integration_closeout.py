"""Synthetic offline preservation cases for the integrated desktop candidate."""
import json

import pytest
from test_windows_install import hashes, home_at, payload, with_runtime

from project_watch.studio import public_executor
from scripts.windows import maintain


def test_old_classic_upgrade_preserves_executor_without_writing_user_config(tmp_path):
    home, root = home_at(tmp_path), tmp_path/'program'
    maintain.install(payload(tmp_path), root, home)
    # Simulate a pre-migration install-state with no executor-default field.
    state_path = root/'install-state.json'
    state = json.loads(state_path.read_text())
    state.pop('public_executor_default', None)
    state_path.write_text(json.dumps(state))
    before = hashes(home)
    bundle, _ = with_runtime(tmp_path, 'two')
    result = maintain.install(bundle, root, home)
    assert json.loads(state_path.read_text())['public_executor_default'] == 'classic'
    assert public_executor.resolve(home, root/'versions'/result['active'])['executor'] == 'classic'
    assert hashes(home) == before
    assert not (home/'desktop-runtime.json').exists()


@pytest.mark.parametrize('choice', [True, False])
def test_explicit_choice_bytes_survive_upgrade(tmp_path, choice):
    home, root = home_at(tmp_path), tmp_path/'program'
    config = home/'desktop-runtime.json'
    config.write_text(json.dumps({'dsh_enabled': choice}, indent=4))
    before = config.read_bytes()
    maintain.install(payload(tmp_path), root, home)
    bundle, _ = with_runtime(tmp_path, 'two')
    maintain.install(bundle, root, home)
    assert config.read_bytes() == before


def test_fresh_bundled_install_default_survives_another_upgrade(tmp_path):
    home, root = home_at(tmp_path), tmp_path/'program'
    bundle, _ = with_runtime(tmp_path)
    maintain.install(bundle, root, home)
    bundle2, _ = with_runtime(tmp_path, 'two')
    maintain.install(bundle2, root, home)
    assert json.loads((root/'install-state.json').read_text())['public_executor_default'] == 'dsh'
    assert not (home/'desktop-runtime.json').exists()


def test_keep_program_preserves_customized_and_shared_files_without_content_reads(tmp_path, monkeypatch):
    home, root = home_at(tmp_path), tmp_path/'program'
    bundle, rid = with_runtime(tmp_path)
    installed = maintain.install(bundle, root, home)
    custom = root/'versions'/installed['active']/'runtime/pythonw.exe'
    custom.write_bytes(b'local customization')
    before_home = hashes(home)
    before_program = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    monkeypatch.setattr(maintain, 'digest', lambda *_: pytest.fail('keep-program must not hash files'))
    result = maintain.uninstall(root, home, keep_program=True)
    assert result['program_preserved'] is True
    assert hashes_without_digest(home) == before_home
    for name, data in before_program.items():
        assert (root/name).read_bytes() == data
    assert (root/'runtimes'/rid/'.complete').exists()


def hashes_without_digest(home):
    import hashlib
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in home.iterdir()
            if p.is_file() and p.name != '.studio.lock'}


def test_keep_program_still_respects_running_home_lock(tmp_path):
    home, root = home_at(tmp_path), tmp_path/'program'
    maintain.install(payload(tmp_path), root, home)
    with maintain.lock(home/'.studio.lock'), pytest.raises(OSError):
        maintain.uninstall(root, home, keep_program=True)


def test_prepare_keeps_existing_staging_and_rejects_unowned_directory(tmp_path):
    home, root = home_at(tmp_path), tmp_path/'program'
    maintain.prepare(root, home)
    note = root/'.payload'/'custom.txt'
    note.write_text('preserve')
    with pytest.raises(ValueError, match='Existing staging'):
        maintain.prepare(root, home)
    assert note.read_text() == 'preserve'
    unknown = tmp_path/'other'
    unknown.mkdir()
    (unknown/'custom.txt').write_text('preserve')
    with pytest.raises(ValueError, match='unowned'):
        maintain.prepare(unknown, home)


def test_uninstaller_ui_is_explicit_and_retention_is_default():
    from pathlib import Path
    script = (Path(__file__).resolve().parents[1]/'scripts/windows/Installer.nsi').read_text(encoding='utf-8')
    assert 'UninstPage custom un.ProgramOptions un.ProgramOptionsLeave' in script
    assert 'StrCpy $KeepProgram ${BST_CHECKED}' in script
    assert '/REMOVEFILES' in script and '--keep-program' in script
    payload_start = 'SetOutPath "\\\\?\\$INSTDIR\\.payload"'
    assert payload_start in script
    extraction = script.split('Section "ProjectWatch（完整功能）"', 1)[1].split(payload_start, 1)[0]
    assert ' prepare --root ' in extraction
    assert 'RMDir /r' not in extraction
