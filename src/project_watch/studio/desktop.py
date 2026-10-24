"""Windowless desktop lifecycle; owns only its app, never another user's service."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import webbrowser
from pathlib import Path


def desktop_home() -> Path:
    # Deliberately do not adopt the classic/default PROJECT_WATCH_HOME.
    if sys.platform == 'darwin':
        return Path.home()/'Library/Application Support/ProjectWatch-Desktop'
    base = os.environ.get('LOCALAPPDATA') or str(Path.home()/'.local/share')
    return Path(base)/'ProjectWatch-Desktop'


def configure_runtime(home: Path, bundle: Path | bool | None = True) -> str:
    """Load the executor choice before starting any workers.

    Installed builds use their own DSH and Node.js by default; ``desktop-runtime.json``
    can turn DSH off or point at a custom runtime. A missing or invalid runtime
    starts the app as ``unavailable`` (research held, reason shown), never the
    legacy collector. No global environment changes, downloads or credentials.
    """
    from project_watch.studio import public_executor

    root = public_executor.bundle_root() if bundle is True else (bundle or None)
    return public_executor.apply(public_executor.resolve(home, root))


def local_identity(url: str) -> dict:
    # Loopback must not traverse an environment proxy or follow a redirect.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(url, timeout=1) as response:
        return json.loads(response.read(4096))


def existing_url(marker: Path) -> str | None:
    try:
        value = json.loads(marker.read_text(encoding='utf-8'))
        port, identity = value['port'], value['instance']
        if type(port) is not int or not 1 <= port <= 65535:
            return None
        if not isinstance(identity, str) or not re.fullmatch('[0-9a-f]{32}', identity):
            return None
        url = f'http://127.0.0.1:{port}/'
        if local_identity(url+'api/desktop/identity').get('instance') == identity:
            return url
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return None


class ExitRequest:
    def __init__(self, server):
        self.server = server
        self.requested = threading.Event()
        self._lock = threading.Lock()

    def request(self) -> bool:
        with self._lock:
            if self.requested.is_set():
                return False
            self.requested.set()
            # Uvicorn awaits the existing application drain/lifespan. Never force_exit.
            self.server.should_exit = True
            return True


def wait_ready(server, thread: threading.Thread, timeout=60):
    until = time.monotonic()+timeout
    while not server.started:
        if not thread.is_alive():
            raise RuntimeError('启动失败，请检查安装完整性或资料目录权限。')
        if time.monotonic() >= until:
            raise RuntimeError('启动仍未完成，请稍后重试；不要删除资料目录。')
        time.sleep(.05)


def message(text: str):
    if os.name == 'nt':
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, 'ProjectWatch', 0x10)
    elif sys.platform == 'darwin':
        # Pass text as argv, never interpolate private paths into AppleScript.
        script = ('on run argv\n'
                  'display alert "ProjectWatch" message (item 1 of argv) as critical\n'
                  'end run')
        try:
            subprocess.run(['/usr/bin/osascript', '-e', script, text], check=False, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            print(text, file=sys.stderr)
    else:
        print(text, file=sys.stderr)


def open_data_folder(home: Path):
    if sys.platform == 'darwin':
        subprocess.run(['/usr/bin/open', str(home)], check=False, timeout=10)
    elif os.name == 'nt':
        os.startfile(home)
    else:
        subprocess.run(['xdg-open', str(home)], check=False, timeout=10)


def start_page(app) -> str:
    """Only a genuinely new, incomplete home starts in the small setup wizard."""
    if app.state.studio_store.list_projects():
        return ''
    settings = app.state.studio_pipeline.settings_store.load()
    if settings.preferred_mode == 'agent':
        return 'agents'
    return '' if settings.model.ready and settings.search.ready_providers() else 'setup'


def tray_image():
    from PIL import Image
    with Image.open(Path(__file__).resolve().parents[1]/'static/brand-tray.png') as image:
        return image.convert('RGBA')


def check_installed_version(home):
    """After acquiring the session lock, reject a launch racing an upgrade."""
    runtime = Path(sys.executable).resolve().parent
    if runtime.name == 'runtime' and runtime.parent.parent.name == 'versions':
        state = json.loads((runtime.parent.parent.parent/'install-state.json').read_text(encoding='utf-8'))
        if state.get('active') != runtime.parent.name or Path(state['home']).resolve() != home:
            raise RuntimeError('版本已切换，请通过快捷方式重新打开。')


def run_desktop(home: Path, *, browser=True, tray=True, smoke_seconds=None):
    import uvicorn

    from project_watch.studio.app import create_studio_app
    from project_watch.studio.host import instance_lock
    from project_watch.studio.settings import _atomic_write

    home = home.resolve()
    session = home/'desktop-session'
    marker = session/'endpoint.json'
    lock = instance_lock(session)
    try:
        lock.__enter__()
    except RuntimeError:
        # A second click may arrive while the first process is still starting.
        for _ in range(40):
            url = existing_url(marker)
            if url:
                if browser:
                    webbrowser.open(url)
                return 'reopened'
            time.sleep(.25)
        raise RuntimeError('ProjectWatch 正在启动或退出，请稍后再打开。') from None

    server = thread = sock = icon = timer = None
    identity = uuid.uuid4().hex
    try:
        check_installed_version(home)
        public_executor = configure_runtime(home)
        app = create_studio_app(home, start_worker=smoke_seconds is None)

        @app.get('/api/desktop/identity', include_in_schema=False)
        def identity_view():
            return {'instance': identity, 'public_executor': public_executor}

        # Reserve an OS-assigned socket; no fixed-port collision or probing race.
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
            log_config=None, log_level='warning', access_log=False))
        stop = ExitRequest(server)
        thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, name='ProjectWatch-server')
        thread.start()
        wait_ready(server, thread)
        _atomic_write(marker, json.dumps({'instance': identity, 'port': port}))
        url = f'http://127.0.0.1:{port}/'+start_page(app)
        if browser:
            webbrowser.open(url)
        if smoke_seconds is not None:
            timer = threading.Timer(smoke_seconds, stop.request)
            timer.start()
        if tray:
            import pystray

            def exit_app(icon, _item):
                if stop.request():
                    icon.title = 'ProjectWatch · 正在安全退出，请等待任务结束'
                    icon.update_menu()

            icon = pystray.Icon('ProjectWatch', tray_image(), 'ProjectWatch · 监控运行中', menu=pystray.Menu(
                pystray.MenuItem('打开 ProjectWatch', lambda: webbrowser.open(url), default=True),
                pystray.MenuItem('打开资料目录', lambda: open_data_folder(home)),
                pystray.MenuItem('退出（等待任务结束）', exit_app, enabled=lambda _: not stop.requested.is_set()),
            ))

            def ready(icon):
                icon.visible = True
                thread.join()
                icon.stop()

            icon.run(setup=ready)
        else:
            thread.join()
        return 'stopped'
    finally:
        if timer:
            timer.cancel()
        if server:
            server.should_exit = True
        if thread:
            # No hard timeout/kill of an in-flight paid operation.
            thread.join()
        if sock:
            sock.close()
        if icon:
            icon.stop()
        with contextlib.suppress(OSError, ValueError, TypeError, AttributeError):
            if json.loads(marker.read_text(encoding='utf-8')).get('instance') == identity:
                marker.unlink()
        lock.__exit__(None, None, None)


def main():
    parser = argparse.ArgumentParser(description='ProjectWatch desktop')
    parser.add_argument('--home', type=Path)
    parser.add_argument('--smoke-seconds', type=float, help='Isolated validation: no workers, automatic graceful exit')
    parser.add_argument('--headless', action='store_true', help='Only valid with smoke mode')
    args = parser.parse_args()
    if args.headless and args.smoke_seconds is None:
        parser.error('headless requires smoke mode')
    if args.smoke_seconds is not None and (args.home is None or not 0 < args.smoke_seconds <= 300):
        parser.error('smoke mode requires an explicit home and duration between 0 and 300 seconds')
    # pythonw has no stdout/stderr: libraries must still see streams, never a console.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, 'w', encoding='utf-8')  # noqa: SIM115 -- process-lifetime standard stream
    if sys.stderr is None:
        sys.stderr = open(os.devnull, 'w', encoding='utf-8')  # noqa: SIM115 -- process-lifetime standard stream
    try:
        run_desktop(args.home or desktop_home(), browser=not args.headless, tray=not args.headless,
                    smoke_seconds=args.smoke_seconds)
        return 0
    except Exception as exc:
        # Do not expose exception text: it can contain config or private paths.
        if not args.headless:
            message('ProjectWatch 无法启动或尚未退出。请稍后重试；若持续出现，请联系维护者。\n'
                    '已有资料保留，不要删除数据目录。错误类型：'+type(exc).__name__)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
