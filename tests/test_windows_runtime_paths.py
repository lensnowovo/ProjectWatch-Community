"""Embedded Python must import deep dependencies without changing Windows policy."""
import importlib.util
import ntpath
from pathlib import Path
from types import SimpleNamespace


def test_runtime_site_customization_only_extends_absolute_local_import_paths():
    script = Path(__file__).resolve().parents[1]/'scripts/windows/runtime_sitecustomize.py'
    spec = importlib.util.spec_from_file_location('runtime_paths', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    paths = ['C:\\安装\\runtime\\python313.zip', 'D:/program/runtime/Lib/site-packages',
             '\\\\?\\D:\\already', '\\\\server\\share', '', 'relative', 'C:relative']
    fake = SimpleNamespace(path=list(paths), platform='win32')
    module.configure(fake, ntpath)
    assert fake.path[:2] == ['\\\\?\\C:\\安装\\runtime\\python313.zip',
                            '\\\\?\\D:\\program\\runtime\\Lib\\site-packages']
    assert fake.path[2:] == paths[2:]
    once = fake.path[:]
    module.configure(fake, ntpath)
    assert fake.path == once
    fake = SimpleNamespace(path=list(paths), platform='linux')
    module.configure(fake, ntpath)
    assert fake.path == paths


def test_windows_bundle_installs_the_reviewed_site_customization():
    builder = (Path(__file__).resolve().parents[1]/'scripts/build_windows_bundle.py').read_text(encoding='utf-8')
    assert "repo/'scripts/windows/runtime_sitecustomize.py', runtime/'sitecustomize.py'" in builder
    assert 'import site\\n' in builder
