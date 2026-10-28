"""Temporary browser fixture: installed product, fake providers, no background work."""
import argparse
import json
import runpy
import socket
import threading
from pathlib import Path

import uvicorn


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--home', type=Path, required=True)
    args = parser.parse_args()
    if args.home.exists():
        raise ValueError('New synthetic home required')
    helpers = runpy.run_path(str(Path(__file__).with_name('distribution_journey_support.py')))
    app, transport = helpers['make_app'](args.home)
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level='warning', access_log=False))
    url = f'http://127.0.0.1:{sock.getsockname()[1]}/setup'
    (args.home/'browser-fixture.json').write_text(json.dumps({'url': url}), encoding='utf-8')
    timer = threading.Timer(300, lambda: setattr(server, 'should_exit', True))
    timer.start()
    print(url, flush=True)
    try:
        server.run(sockets=[sock])
    finally:
        timer.cancel()
        sock.close()
        (args.home/'browser-result.json').write_text(json.dumps({'synthetic_calls': transport.calls,
            'live_calls': 0}), encoding='utf-8')


if __name__ == '__main__':
    main()
