"""A local, rotating log for the windowed desktop app, which has no console.

Without it, warnings and failure tracebacks were discarded: a live job that failed
with "内部错误" left nothing to diagnose. The file stays in the data directory, is
never uploaded, and credential-looking values are masked before they are written.
"""
from __future__ import annotations

import logging
import re
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

MAX_BYTES = 5 * 1024 ** 2
BACKUPS = 3
_SECRET = re.compile(
    r"(?i)((?<![A-Za-z0-9_])(?:api[_-]?key|apikey|access[_-]?token|token|secret|password|authorization|key)['\"]?\s*[=:]\s*['\"]?"
    r"(?:bearer\s+)?)[^\s&'\",;}]+"
    r"|(bearer\s+)[A-Za-z0-9._~+/=-]+"
    r"|\b(sk-)[A-Za-z0-9_-]{8,}")


def redact(text: str) -> str:
    return _SECRET.sub(lambda m: (m[1] or m[2] or m[3]) + '***', text)


class RedactingFormatter(logging.Formatter):
    def format(self, record):
        return redact(super().format(record))


def log_path(home: Path) -> Path:
    return home / 'logs' / 'studio.log'


def configure(home: Path) -> Path:
    """Send warnings, errors and failure tracebacks of this process to the data directory."""
    path = log_path(home)
    root = logging.getLogger()
    current = [h for h in root.handlers if getattr(h, 'projectwatch_desktop', False)]
    if not any(h.baseFilename == str(path.resolve()) for h in current):
        for stale in current:  # one data directory per process: follow the current one
            root.removeHandler(stale)
            stale.close()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding='utf-8', delay=True)
        except OSError:
            return path  # a log that cannot be opened must not stop the app
        handler.projectwatch_desktop = True
        handler.setLevel(logging.WARNING)
        handler.setFormatter(RedactingFormatter('%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s'))
        root.addHandler(handler)
        logging.captureWarnings(True)
    if root.level == logging.NOTSET or root.level > logging.WARNING:
        root.setLevel(logging.WARNING)
    if threading.excepthook is threading.__excepthook__:
        threading.excepthook = _thread_exception
    return path


def _thread_exception(args):
    if args.exc_type is SystemExit:
        return
    logging.getLogger('project_watch.thread').error(
        'unhandled exception in thread %s', getattr(args.thread, 'name', ''),
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
