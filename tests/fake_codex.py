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
        json.dump({'argv': args, 'project_watch_env': sorted(k for k in os.environ if k.upper().startswith('PROJECT_WATCH_'))}, stream, ensure_ascii=False)
mode = os.environ.get('FAKE_CODEX_MODE', 'ok')
if mode == 'hang':
    time.sleep(60)
if mode == 'fail':
    print(json.dumps({'type': 'error', 'message': 'not logged in'}), flush=True)
    sys.exit(2)
print(json.dumps({'type': 'thread.started'}), flush=True)
if mode == 'unconfirmed':
    print(json.dumps({'type': 'item.started', 'item': {'type': 'mcp_tool_call', 'tool': 'submit_monitoring_results'}}), flush=True)
    print(json.dumps({'type': 'item.completed', 'item': {'type': 'mcp_tool_call', 'tool': 'submit_monitoring_results', 'status': 'failed'}}), flush=True)
elif mode != 'nowrite':
    print(json.dumps({'type': 'item.completed', 'item': {'type': 'mcp_tool_call', 'tool': 'submit_monitoring_results', 'status': 'completed'}}), flush=True)
print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 1200, 'output_tokens': 34}}), flush=True)
with open(args[args.index('-o') + 1], 'w', encoding='utf-8') as stream:
    stream.write('写入 2 条新闻。')
