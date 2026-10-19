"""Read-only disk health and bounded stdout/stderr capture for owned children."""

import logging
import os
import shutil
import threading
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler


def disk_health(home):
    try:
        usage = shutil.disk_usage(home)
        low = usage.free < 1024 ** 3 or usage.free / max(1, usage.total) < .05
        return {'state': 'low' if low else 'ok', 'free_bytes': usage.free,
                'label': '磁盘空间不足，请整理磁盘' if low else '磁盘空间正常'}
    except OSError:
        return {'state': 'unknown', 'free_bytes': None, 'label': '暂时无法读取磁盘空间'}


@contextmanager
def rotating_child_log(path, *, max_bytes=5 * 1024 ** 2, backups=3):
    """Drain even on log write failure, so disk-full cannot deadlock the child.

    Keeps only this named log's rotation set, never deletes legacy/user logs.
    Close the owned child tree before exiting this context to release its pipe.
    """
    handler = RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backups, encoding='utf-8')
    read_fd, write_fd = os.pipe()
    reader, writer = os.fdopen(read_fd, 'rb', buffering=0), os.fdopen(write_fd, 'wb', buffering=0)
    state = {'write_failed': False}

    def consume():
        with reader:
            while chunk := reader.read(4096):
                record = logging.LogRecord('studio.child', logging.WARNING, '', 0,
                                           chunk.decode('utf-8', errors='replace').rstrip('\n'), (), None)
                try:
                    # Call directly: do not echo arbitrary child text via logging's
                    # global error handler when storage is unavailable.
                    if handler.shouldRollover(record):
                        handler.doRollover()
                    handler.stream.write(handler.format(record) + '\n')
                    handler.flush()
                except OSError:
                    state['write_failed'] = True
        handler.close()

    thread = threading.Thread(target=consume, name='studio-log-drain', daemon=True)
    thread.start()
    try:
        yield writer, state
    finally:
        writer.close()
        thread.join(timeout=10)
        if thread.is_alive():
            raise RuntimeError('日志管道仍被子进程占用')
