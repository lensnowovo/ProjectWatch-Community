"""Keep dependency commands without embedding the build interpreter path."""
import base64
import csv
import hashlib
import io
import sys
import zipfile

import pytest


def fixture(tmp_path, *, unknown=False, newline=b'\n'):
    site = tmp_path/'runtime/Lib/site-packages'
    folder = site/'bin'
    folder.mkdir(parents=True)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('__main__.py', 'print("synthetic command")')
    prefix = b'MZ' + '<launcher_dir>\\'.encode('utf-16le')
    exe = prefix + b'#!' + sys.executable.encode() + b'\n' + archive.getvalue()
    (folder/'tool.exe').write_bytes(b'unknown executable' if unknown else exe)
    (folder/'script.py').write_bytes(b'#!' + sys.executable.encode() + newline + b'print("synthetic")' + newline)
    meta = site/'synthetic-1.dist-info'
    meta.mkdir()
    with (meta/'RECORD').open('w', newline='', encoding='utf-8') as stream:
        writer = csv.writer(stream)
        for path in folder.iterdir():
            data = path.read_bytes()
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()
            writer.writerow(['../../bin/'+path.name, 'sha256='+digest, len(data)])
        writer.writerow(['synthetic-1.dist-info/RECORD', '', ''])
    return site, prefix, archive.getvalue()


def test_scripts_are_relocatable_and_records_match_without_losing_payload(tmp_path):
    from scripts.windows.portable_scripts import make_portable
    site, prefix, archive = fixture(tmp_path)
    result = make_portable(site)
    assert result == ['script.py', 'tool.exe']
    data = (site/'bin/tool.exe').read_bytes()
    assert data == prefix + b'#!<launcher_dir>\\..\\..\\..\\python.exe\n' + archive
    assert sys.executable.encode() not in data
    assert (site/'bin/script.py').read_bytes() == b'#!/usr/bin/env python\nprint("synthetic")\n'
    with (site/'synthetic-1.dist-info/RECORD').open(newline='', encoding='utf-8') as stream:
        rows = list(csv.reader(stream))
    for name, digest, size in rows[:2]:
        assert name.startswith('bin/')
        content = (site/name).read_bytes()
        assert digest == 'sha256='+base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b'=').decode()
        assert int(size) == len(content)
    assert make_portable(site) == []


def test_unknown_launcher_fails_before_any_script_is_changed(tmp_path):
    from scripts.windows.portable_scripts import make_portable
    site, _, _ = fixture(tmp_path, unknown=True)
    before = {p: p.read_bytes() for p in site.rglob('*') if p.is_file()}
    with pytest.raises(ValueError, match='launcher'):
        make_portable(site)
    assert before == {p: p.read_bytes() for p in site.rglob('*') if p.is_file()}


def test_windows_crlf_shebang_does_not_retain_the_build_machine_path(tmp_path):
    from scripts.windows.portable_scripts import make_portable
    site, _, _ = fixture(tmp_path, newline=b'\r\n')
    assert make_portable(site) == ['script.py', 'tool.exe']
    data = (site/'bin/script.py').read_bytes()
    assert data == b'#!/usr/bin/env python\r\nprint("synthetic")\r\n'
    assert sys.executable.encode() not in data
    assert make_portable(site) == []


def test_record_mismatch_is_not_silently_rewritten(tmp_path):
    from scripts.windows.portable_scripts import make_portable
    site, _, _ = fixture(tmp_path)
    path = site/'bin/script.py'
    path.write_bytes(path.read_bytes()+b'# altered\n')
    with pytest.raises(ValueError, match='RECORD'):
        make_portable(site)


def test_unowned_script_is_rejected_and_no_script_directory_is_a_noop(tmp_path):
    from scripts.windows.portable_scripts import make_portable
    assert make_portable(tmp_path/'empty') == []
    site, _, _ = fixture(tmp_path)
    (site/'synthetic-1.dist-info/RECORD').write_text('', encoding='utf-8')
    with pytest.raises(ValueError, match='ownership'):
        make_portable(site)
