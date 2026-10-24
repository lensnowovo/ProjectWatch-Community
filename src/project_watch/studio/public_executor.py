"""Which public-research executor this desktop process runs, and why.

Installed builds carry their own DSH runtime and Node.js beside the Python
runtime and enable them by default. A missing or damaged runtime degrades to
``unavailable``: the app still starts, automatic public research is held with a
visible reason, and the legacy collector is never run in its place.
No downloads, global environment changes or credential changes happen here.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

RUNTIME = 'PROJECT_WATCH_DSH_RUNTIME'
MONITOR = 'PROJECT_WATCH_DSH_MONITOR'
EXECUTOR = 'PROJECT_WATCH_EXECUTOR'
SOURCE = 'PROJECT_WATCH_EXECUTOR_SOURCE'
UNAVAILABLE = 'PROJECT_WATCH_DSH_UNAVAILABLE'
CONFIG_NAME = 'desktop-runtime.json'

# Plain-language reasons; the code is stored, the page shows the text.
REASONS = {
    'bundled_missing': '安装包自带的 DSH 运行时缺失或不完整，请重新安装。',
    'node_missing': '未找到 DSH 所需的 Node.js，请重新安装或检查自定义路径。',
    'version_mismatch': 'DSH 运行时版本与本程序不匹配，请重新安装。',
    'invalid_config': '运行设置文件格式无效，请在设置中重新选择。',
    'invalid_install_state': '安装状态记录无效，请修复安装；不会擅自切换执行器。',
    'invalid_paths': '自定义的 DSH 或 Node.js 路径无效。',
}


def bundle_root() -> Path | None:
    """Windows version directory or a Mac application's Resources directory."""
    executable = Path(sys.executable).resolve()
    root = executable.parent.parent
    if (sys.platform == 'darwin' and executable.parent.name == 'bin'
            and root.name == 'runtime' and root.parent.name == 'Resources'
            and root.parent.parent.name == 'Contents'
            and root.parent.parent.parent.suffix == '.app'):
        # Keep damaged installed bundles on the unavailable path, not source defaults.
        return root.parent
    return root if (root/'bundle-manifest.json').is_file() else None


def bundled_paths(root: Path) -> tuple[Path, Path, dict]:
    """DSH and Node are shared runtimes: ``runtimes/<id>`` beside the bundle before install,
    ``<install>/runtimes/<id>`` once installed (``root`` is then ``versions/<v>``)."""
    manifest = json.loads((root/'bundle-manifest.json').read_text(encoding='utf-8'))
    dsh = manifest.get('dsh') if isinstance(manifest, dict) else None
    dsh = dsh if isinstance(dsh, dict) else {}
    if isinstance(manifest, dict) and manifest.get('format') == 'projectwatch-macos-bundle-v1':
        return root/'dsh', root/'node'/'bin'/'node', dsh
    dsh_id, node_id = dsh.get('dsh_runtime_id'), dsh.get('node_runtime_id')
    if not dsh_id or not node_id:
        return root/'dsh', root/'node'/'node.exe', dsh  # Earlier layout inside the version.
    for base in (root/'runtimes', root.parent.parent/'runtimes'):
        if (base/dsh_id).is_dir() or (base/node_id).is_dir():
            return base/dsh_id, base/node_id/'node.exe', dsh
    return root.parent.parent/'runtimes'/dsh_id, root.parent.parent/'runtimes'/node_id/'node.exe', dsh


def _validate(runtime: Path, node: Path) -> str | None:
    from project_watch.studio.dsh_runtime import runtime_entry

    if not node.is_file() or node.name.lower() not in ('node', 'node.exe'):
        return 'node_missing'
    if sys.platform == 'darwin' and not os.access(node, os.X_OK):
        return 'node_missing'
    try:
        entry = runtime_entry(runtime)
    except ValueError:
        return 'version_mismatch'
    except (OSError, json.JSONDecodeError):
        return 'bundled_missing'
    return None if entry.is_file() else 'bundled_missing'


def resolve(home: Path, bundle: Path | None) -> dict:
    """Decide the executor without touching the process environment."""
    path = home/CONFIG_NAME
    config = None
    if path.exists():
        try:
            config = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {'executor': 'unavailable', 'source': 'custom', 'reason': 'invalid_config'}
        if not isinstance(config, dict) or type(config.get('dsh_enabled')) is not bool:
            return {'executor': 'unavailable', 'source': 'custom', 'reason': 'invalid_config'}
        if not config['dsh_enabled']:
            return {'executor': 'classic', 'source': 'setting'}
    custom = config and any(k in config for k in ('dsh_runtime', 'node_executable'))
    if custom:
        values = [config.get(k) for k in ('dsh_runtime', 'node_executable')]
        if not all(isinstance(v, str) and v.strip() for v in values):
            return {'executor': 'unavailable', 'source': 'custom', 'reason': 'invalid_paths'}
        runtime, node = (Path(v) for v in values)
        if not runtime.is_absolute() or not node.is_absolute():
            return {'executor': 'unavailable', 'source': 'custom', 'reason': 'invalid_paths'}
        fault = _validate(runtime, node)
        if fault:
            return {'executor': 'unavailable', 'source': 'custom', 'reason': fault}
        return {'executor': 'dsh', 'source': 'custom', 'runtime': str(runtime.resolve()), 'node': str(node.resolve())}
    if bundle is None:
        if config:  # Enabled without paths, but this build carries no runtime.
            return {'executor': 'unavailable', 'source': 'bundled', 'reason': 'bundled_missing'}
        # Source/developer runs keep their explicit environment opt-in.
        inherited = os.environ.get(RUNTIME) and os.environ.get(MONITOR) == '1'
        return {'executor': 'dsh', 'source': 'environment'} if inherited else {'executor': 'classic', 'source': 'default'}
    if config is None and bundle.parent.name == 'versions':
        state_path = bundle.parent.parent/'install-state.json'
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding='utf-8'))
                if not isinstance(state, dict):
                    raise ValueError('Invalid install state')
                default = state.get('public_executor_default')
                if default not in (None, 'classic', 'dsh'):
                    raise ValueError('Invalid executor default')
                if default == 'classic':
                    return {'executor': 'classic', 'source': 'upgrade_preserved'}
            except (OSError, ValueError):
                return {'executor': 'unavailable', 'source': 'bundled', 'reason': 'invalid_install_state'}
    try:
        runtime, node, meta = bundled_paths(bundle)
    except (OSError, ValueError):
        return {'executor': 'unavailable', 'source': 'bundled', 'reason': 'bundled_missing'}
    if not meta:
        # An older bundle without DSH: honest classic unless DSH was asked for.
        if config:
            return {'executor': 'unavailable', 'source': 'bundled', 'reason': 'bundled_missing'}
        return {'executor': 'classic', 'source': 'default'}
    fault = _validate(runtime, node)
    if fault:
        return {'executor': 'unavailable', 'source': 'bundled', 'reason': fault}
    return {'executor': 'dsh', 'source': 'bundled', 'runtime': str(runtime.resolve()), 'node': str(node.resolve()),
            'node_version': meta.get('node_version')}


def apply(state: dict) -> str:
    """Write the decided state into this process only; never a global setting."""
    for key in (EXECUTOR, SOURCE, UNAVAILABLE):
        os.environ.pop(key, None)
    if state['executor'] != 'dsh' or state['source'] != 'environment':
        os.environ.pop(RUNTIME, None)
        os.environ.pop(MONITOR, None)
    if state['executor'] == 'dsh' and state.get('runtime'):
        os.environ[RUNTIME] = state['runtime']
        os.environ[MONITOR] = '1'
        os.environ['PATH'] = str(Path(state['node']).parent) + os.pathsep + os.environ.get('PATH', '')
    if state['executor'] == 'unavailable':
        os.environ[UNAVAILABLE] = state['reason']
    os.environ[EXECUTOR] = state['executor']
    os.environ[SOURCE] = state['source']
    if state.get('node_version'):
        os.environ['PROJECT_WATCH_NODE_VERSION'] = str(state['node_version'])
    return state['executor']


def unavailable_reason() -> str | None:
    return os.environ.get(UNAVAILABLE) or None


def require_available() -> None:
    """Hold public research visibly instead of running the legacy collector."""
    if unavailable_reason():
        raise ValueError('dsh_unavailable')


def current(home: Path | None = None) -> dict:
    """Read-only view for the settings page."""
    from project_watch.studio.dsh_runtime import DSH_VERSION

    executor = os.environ.get(EXECUTOR)
    configurable = executor is not None  # Only the desktop process decides from a setting file.
    if executor is None:
        executor = 'dsh' if os.environ.get(RUNTIME) and os.environ.get(MONITOR) == '1' else 'classic'
    reason = unavailable_reason()
    saved = None
    if configurable and home is not None and (home/CONFIG_NAME).exists():
        try:
            value = json.loads((home/CONFIG_NAME).read_text(encoding='utf-8'))
            saved = value.get('dsh_enabled') if isinstance(value, dict) and type(value.get('dsh_enabled')) is bool else None
        except (OSError, ValueError):
            saved = None
    return {'executor': executor, 'source': os.environ.get(SOURCE) or ('environment' if executor == 'dsh' else 'default'),
            'reason': reason, 'reason_text': REASONS.get(reason) if reason else None,
            'dsh_version': DSH_VERSION, 'node_version': os.environ.get('PROJECT_WATCH_NODE_VERSION'),
            'configurable': configurable, 'saved_enabled': saved}


def write_choice(home: Path, enabled: bool) -> None:
    """Persist the on/off choice; custom paths, if any, are kept. Takes effect on restart."""
    from project_watch.studio.settings import _atomic_write

    path = home/CONFIG_NAME
    config: dict = {}
    if path.exists():
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
            if isinstance(value, dict):
                config = {k: value[k] for k in ('dsh_runtime', 'node_executable') if isinstance(value.get(k), str)}
        except (OSError, ValueError):
            config = {}
    _atomic_write(path, json.dumps({'dsh_enabled': bool(enabled), **config}, ensure_ascii=False))
