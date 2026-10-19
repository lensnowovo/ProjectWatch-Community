import importlib.util
from pathlib import Path

from project_watch.feeds import FixedFeedProxyTransport


def test_probe_uses_single_exact_request_restart_and_original_dates(tmp_path, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location('live_feed_probe', Path(__file__).parents[1] / 'scripts/live_feed_probe.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    requests = []
    from datetime import UTC, datetime
    from email.utils import format_datetime
    xml = f'''<rss><channel><item><guid>synthetic</guid><title>Synthetic optics news</title>
        <link>https://example.org/item</link><description>synthetic public notice</description>
        <pubDate>{format_datetime(datetime.now(UTC))}</pubDate></item></channel></rss>'''.encode()
    monkeypatch.setattr(FixedFeedProxyTransport, 'get', lambda self, url: requests.append(url) or xml)
    target = 'https://example.org/rss'
    module.exercise(tmp_path, term='Synthetic optics', target=target, proxy_url='http://127.0.0.1:17891')
    assert requests == [target]
    assert '"visible_items": 1' in capsys.readouterr().out


def test_local_factory_uses_operator_config_only(tmp_path, monkeypatch):
    import project_watch.api as api
    from project_watch.api import create_local_app
    seen = {}
    monkeypatch.setenv('PROJECT_WATCH_DB_PATH', str(tmp_path/'watch.sqlite3'))
    monkeypatch.setenv('PROJECT_WATCH_FIXED_FEED_PROXY_URL', 'http://127.0.0.1:17891')
    monkeypatch.setenv('PROJECT_WATCH_FIXED_FEED_TARGETS', '["https://example.org/rss"]')
    monkeypatch.setattr(api, 'create_app', lambda *args, **kwargs: seen.update(kwargs))
    create_local_app()
    assert seen['fixed_feed_targets'] == ('https://example.org/rss',)
    assert seen['fixed_feed_proxy_url'] == 'http://127.0.0.1:17891'
