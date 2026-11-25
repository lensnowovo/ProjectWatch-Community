"""A stand-in for the Codex CLI: records its arguments and plays one scripted outcome."""
import json
import os
import sys
import time

args = sys.argv[1:]
if args[:1] == ['--version']:
    print('codex-cli 0.0.0-test')
    sys.exit(0)
if args[:2] == ['login', 'status']:
    sys.exit(0 if os.environ.get('FAKE_CODEX_LOGGED_IN', '1') == '1' else 1)
record = os.environ.get('FAKE_CODEX_RECORD')
if record:
    with open(record, 'w', encoding='utf-8') as stream:
        json.dump({'argv': args, 'config': os.environ.get('PROJECT_WATCH_AGENT_CONFIG')}, stream, ensure_ascii=False)
mode = os.environ.get('FAKE_CODEX_MODE', 'ok')
if mode == 'hang':
    time.sleep(60)
if mode == 'fail':
    print(json.dumps({'type': 'error', 'message': 'not logged in'}), flush=True)
    sys.exit(2)
print(json.dumps({'type': 'thread.started'}), flush=True)
print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 1200, 'output_tokens': 34}}), flush=True)
with open(args[args.index('-o') + 1], 'w', encoding='utf-8') as stream:
    stream.write('写入 2 条新闻。')
