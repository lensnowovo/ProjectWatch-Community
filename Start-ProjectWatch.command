#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
if [[ "$(uname -s)" != "Darwin" ]]; then
  printf '%s\n' '此启动器用于 macOS。Windows 请使用 Windows 启动器。' >&2
  exit 1
fi
# Finder may omit Homebrew from PATH. Add known tool locations, never install them.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
if [[ -x .venv-macos/bin/python ]]; then
  exec .venv-macos/bin/python -I -B scripts/macos_developer.py "$@"
fi
for candidate in python3.12 python3.13; do
  if command -v "$candidate" >/dev/null 2>&1; then
    exec "$candidate" -I -B scripts/macos_developer.py "$@"
  fi
done
printf '%s\n' '请先安装 Python 3.12/3.13 与 Node.js 22+，再参考 docs/macos-developer.md。' >&2
exit 1
