"""Start ProjectWatch locally and open the browser: ``python -m project_watch.studio``."""

from __future__ import annotations

import argparse
import socket
import threading
import webbrowser


def _free_port(preferred: int) -> int:
    for port in (preferred, *range(preferred + 1, preferred + 20)):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit("找不到可用端口")


def main() -> None:
    parser = argparse.ArgumentParser(description="ProjectWatch 本机启动器")
    parser.add_argument("--port", type=int, default=8796)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument('--strict-port', action='store_true', help='端口占用时退出，不自动换端口')
    args = parser.parse_args()

    import uvicorn

    from project_watch.studio.app import create_studio_app, default_data_dir

    port = args.port if args.strict_port else _free_port(args.port)
    url = f"http://127.0.0.1:{port}/"
    print(f"ProjectWatch 已启动：{url}")
    print(f"数据目录：{default_data_dir()}")
    print("关闭此窗口即可停止。")
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_studio_app(), host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
