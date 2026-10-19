"""Own a local parser tree; progress is completed work, not a timer heartbeat."""

import os
import signal
import subprocess
import threading
import time
from contextlib import suppress

from project_watch.extractors import ExtractionError


def run_parser(command, *, env, progress_path, cancel=None, on_progress=None,
               idle_seconds=900, poll_seconds=.2):
    if idle_seconds <= 0 or poll_seconds <= 0:
        raise ValueError('watchdog intervals must be positive')
    cancel = cancel or threading.Event()
    if cancel.is_set():
        raise ExtractionError('parser_cancelled')
    child = None
    try:
        with open(os.devnull, 'wb') as log:
            if os.name == 'nt':
                from project_watch.studio.windows_child import WindowsChild
                child = WindowsChild(command, env=env, log=log)
            else:
                child = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                                         stdout=log, stderr=log, start_new_session=True)
            last, signature = time.monotonic(), None
            while child.poll() is None:
                if cancel.is_set():
                    raise ExtractionError('parser_cancelled')
                try:
                    current = progress_path.stat().st_mtime_ns
                except FileNotFoundError:
                    current = None
                if current is not None and current != signature:
                    signature, last = current, time.monotonic()
                    if on_progress:
                        on_progress()
                if time.monotonic() - last >= idle_seconds:
                    raise ExtractionError('parser_stalled')
                cancel.wait(poll_seconds)
            if child.poll() != 0:
                raise ExtractionError('parser_crashed')
    except OSError as exc:
        raise ExtractionError('parser_crashed') from exc
    finally:
        if child is not None:
            # Always reap the owned tree, including descendants surviving a root
            # exit. Never discover/kill processes by executable name.
            if os.name == 'nt':
                try:
                    child.terminate()
                    child.wait(timeout=10)
                finally:
                    child.close()
            else:
                with suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=10)
