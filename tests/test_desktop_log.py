"""The windowed desktop keeps a local log of warnings and failures, with credentials masked."""
import logging
import threading

import pytest

from project_watch.studio import desktop_log


@pytest.fixture
def clean_root():
    root = logging.getLogger()
    handlers, level, hook = list(root.handlers), root.level, threading.excepthook
    yield root
    for handler in root.handlers:
        if handler not in handlers:
            handler.close()
    root.handlers[:] = handlers
    root.setLevel(level)
    threading.excepthook = hook


def flush(root):
    for handler in root.handlers:
        handler.flush()


def test_failures_and_tracebacks_reach_the_local_log(tmp_path, clean_root):
    path = desktop_log.configure(tmp_path)
    assert path == tmp_path / 'logs' / 'studio.log'
    log = logging.getLogger('project_watch.studio.pipeline')
    log.info('routine detail stays out')
    try:
        raise RuntimeError('job lease expired or replaced')
    except RuntimeError:
        log.exception('studio job failed')
    flush(clean_root)
    text = path.read_text(encoding='utf-8')
    assert 'studio job failed' in text and 'RuntimeError: job lease expired or replaced' in text
    assert 'Traceback' in text and 'routine detail' not in text


def test_credentials_are_masked_before_writing(tmp_path, clean_root):
    path = desktop_log.configure(tmp_path)
    logging.getLogger('project_watch').warning(
        'GET https://data.example/v1?q=a&api_key=SECRET123&x=1 Authorization: Bearer tok.en-456 sk-abcdefghijkl password=pw')
    flush(clean_root)
    text = path.read_text(encoding='utf-8')
    for secret in ('SECRET123', 'tok.en-456', 'abcdefghijkl', '=pw'):
        assert secret not in text
    assert 'api_key=***' in text and 'q=a' in text


def test_configuring_twice_adds_one_handler(tmp_path, clean_root):
    desktop_log.configure(tmp_path)
    desktop_log.configure(tmp_path)
    assert sum(getattr(h, 'projectwatch_desktop', False) for h in clean_root.handlers) == 1


def test_an_unhandled_thread_exception_is_logged(tmp_path, clean_root):
    threading.excepthook = threading.__excepthook__  # as in the desktop process (pytest installs its own)
    path = desktop_log.configure(tmp_path)

    def boom():
        raise ValueError('thread failure')
    thread = threading.Thread(target=boom, name='studio-worker-test')
    thread.start()
    thread.join()
    flush(clean_root)
    text = path.read_text(encoding='utf-8')
    assert 'studio-worker-test' in text and 'ValueError: thread failure' in text


@pytest.mark.parametrize('raw', ['{"api_key": "abc123.XYZ"}', "{'api_key': 'abc123.XYZ'}", '"password":"abc123.XYZ"',
                                 'Authorization: abc123.XYZ', "{'Authorization': 'Bearer abc123.XYZ'}", 'x-api-key: abc123.XYZ'])
def test_quoted_and_header_credentials_are_masked(raw):
    assert 'abc123.XYZ' not in desktop_log.redact(raw)


def test_request_hashes_stay_readable():
    assert desktop_log.redact('call_key=ab12 cache_key=cd34') == 'call_key=ab12 cache_key=cd34'


def test_a_log_that_cannot_be_opened_does_not_stop_the_app(tmp_path, clean_root):
    (tmp_path / 'logs').write_text('not a directory')
    desktop_log.configure(tmp_path)
    assert not any(getattr(h, 'projectwatch_desktop', False) for h in clean_root.handlers)


def test_a_failed_desktop_start_is_logged_with_its_traceback(tmp_path, clean_root, monkeypatch):
    from project_watch.studio import desktop

    def broken(_home):
        raise RuntimeError('migration failed')
    monkeypatch.setattr(desktop, 'check_installed_version', broken)
    with pytest.raises(RuntimeError):
        desktop.run_desktop(tmp_path, browser=False, tray=False, smoke_seconds=1)
    flush(clean_root)
    text = desktop_log.log_path(tmp_path).read_text(encoding='utf-8')
    assert 'desktop failed' in text and 'RuntimeError: migration failed' in text


def test_a_new_data_directory_replaces_the_previous_log(tmp_path, clean_root):
    desktop_log.configure(tmp_path / 'a')
    path = desktop_log.configure(tmp_path / 'b')
    logging.getLogger('project_watch').warning('second home')
    flush(clean_root)
    assert 'second home' in path.read_text(encoding='utf-8')
    assert sum(getattr(h, 'projectwatch_desktop', False) for h in clean_root.handlers) == 1
