import base64
import io
import json
import subprocess
import urllib.error
import urllib.request

import pytest

from project_watch import host_network as host
from project_watch import host_network_worker as worker
from project_watch.feeds import FeedError, GuardedHTTPS, PublicResponse
from project_watch.model_transport import PublicModelOpener
from project_watch.public_article import ArticleReader

EXE = '/mnt/d/pilot/host-venv/Scripts/python.exe'


def test_default_route_unchanged_and_opt_in_is_explicit(monkeypatch):
    monkeypatch.delenv(host.ENV_NAME, raising=False)
    assert isinstance(host.configured_model_opener(), PublicModelOpener)
    assert isinstance(host.configured_article_transport(), GuardedHTTPS)
    monkeypatch.setenv(host.ENV_NAME, EXE)
    monkeypatch.setattr(host.os.path, 'isfile', lambda value: value == EXE)
    assert isinstance(host.configured_model_opener(), host.HostModelOpener)
    assert isinstance(ArticleReader().transport, host.HostArticleTransport)


@pytest.mark.parametrize('value', ['', 'python.exe', 'https://host/python.exe',
                                  '/mnt/d/x/../python.exe', EXE+' -c attack',
                                  '/mnt/d/x/python.exe\n', '//server/python.exe'])
def test_invalid_executable_fails_closed(monkeypatch, value):
    monkeypatch.setenv(host.ENV_NAME, value)
    monkeypatch.setattr(host.os.path, 'isfile', lambda _: True)
    with pytest.raises(ValueError):
        host.configured_model_opener()


def test_payload_and_key_only_in_pipe_no_shell_no_inherited_secret(monkeypatch):
    monkeypatch.setenv('GLM_API_KEY', 'inherited-secret')
    monkeypatch.setenv('HTTPS_PROXY', 'http://attacker.invalid')
    observed = []

    def run(command, **options):
        observed.append((command, options))
        return subprocess.CompletedProcess(command, 0, json.dumps({'version': 1, 'body': 'e30='}).encode())

    monkeypatch.setattr(host.subprocess, 'run', run)
    request = urllib.request.Request('https://model.example/v1/messages', data=b'{"prompt":"synthetic"}',
                                     headers={'Authorization': 'Bearer pipe-secret'}, method='POST')
    assert host.HostModelOpener(EXE).open(request).read() == b'{}'
    command, options = observed[0]
    assert command == [EXE, '-I', '-m', 'project_watch.host_network_worker']
    assert 'shell' not in options
    assert b'pipe-secret' in options['input']
    assert 'GLM_API_KEY' not in options['env'] and 'HTTPS_PROXY' not in options['env']
    assert 'synthetic' not in str(command) and 'pipe-secret' not in str(command)


@pytest.mark.parametrize('failure', ['timeout', 'invalid', 'oversized', 'nonzero', 'unknown_error'])
def test_bridge_failure_never_replays_or_falls_back(monkeypatch, failure):
    calls = []

    def run(command, **options):
        calls.append(command)
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(command, 85)
        body = {'invalid': b'not json', 'oversized': b' '*(host.MAX_OUTPUT+1),
                'nonzero': b'', 'unknown_error': b'{"version":1,"error":"secret-provider-body"}'}[failure]
        return subprocess.CompletedProcess(command, 1 if failure == 'nonzero' else 0, body)

    monkeypatch.setattr(host.subprocess, 'run', run)
    with pytest.raises(urllib.error.URLError, match='host model network unavailable'):
        host.HostModelOpener(EXE).open(urllib.request.Request('https://model.example/v1/messages', data=b'{}'))
    assert len(calls) == 1


def test_article_response_uses_shared_pinned_guards_and_extraction(monkeypatch):
    seen = []
    html = ('<article><p>'+('Public synthetic article. '*15)+'</p></article>').encode()

    def get(self, url):
        seen.append((url, self.resolver, self.connector))
        return PublicResponse(url, {'content-type': 'text/html', 'set-cookie': 'do-not-copy'}, html)

    monkeypatch.setattr(worker.GuardedHTTPS, 'get_response', get)
    monkeypatch.setattr(host, 'exchange', lambda exe, payload: {'version': 1, **worker.dispatch(payload)})
    result = ArticleReader(host.HostArticleTransport(EXE)).read('https://news.example/item')
    assert result['status'] == 'article_body'
    assert seen[0][1] is worker.resolve_article
    assert result['text'].startswith('Public synthetic')


@pytest.mark.parametrize('url', ['http://news.example', 'https://127.0.0.1/',
                                'https://user:secret@news.example/', 'https://x.local/'])
def test_worker_rejects_invalid_destinations_before_io(url):
    with pytest.raises(FeedError):
        worker.dispatch({'version': 1, 'operation': 'article', 'url': url})


def test_worker_private_mixed_dns_rejected_without_network(monkeypatch):
    monkeypatch.setattr(worker, 'resolve_article', lambda _: ['8.8.8.8', '127.0.0.1'])
    with pytest.raises(FeedError, match='disallowed_address'):
        worker.dispatch({'version': 1, 'operation': 'article', 'url': 'https://news.example/item'})


def test_worker_model_uses_direct_guard_not_recursive_factory(monkeypatch):
    requests = []
    monkeypatch.setenv(host.ENV_NAME, EXE)

    def open(self, request, timeout):
        requests.append(request)
        return io.BytesIO(b'data: {}')

    monkeypatch.setattr(worker.PublicModelOpener, 'open', open)
    result = worker.dispatch({'version': 1, 'operation': 'model', 'url': 'https://model.example/v1/messages',
                              'headers': {'Authorization': 'Bearer synthetic'}, 'body': 'e30='})
    assert len(requests) == 1 and requests[0].get_method() == 'POST'
    assert base64.b64decode(result['body']) == b'data: {}'


@pytest.mark.parametrize('headers', [{'Host': '127.0.0.1'}, {'Authorization': 'secret\r\nInjected: yes'}, []])
def test_worker_rejects_header_override_and_injection(headers):
    with pytest.raises(ValueError):
        worker.dispatch({'version': 1, 'operation': 'model', 'url': 'https://model.example/v1/messages',
                         'headers': headers, 'body': 'e30='})


def test_worker_never_returns_exception_or_payload(monkeypatch):
    def fail(_):
        raise RuntimeError('sensitive provider response')
    monkeypatch.setattr(worker, 'dispatch', fail)
    output = io.BytesIO()
    worker.run(io.BytesIO(b'{}'), output)
    assert json.loads(output.getvalue()) == {'version': 1, 'error': 'host_network_unavailable'}


def test_worker_rejects_oversized_input_and_arbitrary_operations(monkeypatch):
    with pytest.raises(ValueError):
        worker.dispatch({'version': 1, 'operation': 'exec', 'url': 'https://example.org'})
    output = io.BytesIO()
    worker.run(io.BytesIO(b' '*(host.MAX_INPUT+1)), output)
    assert json.loads(output.getvalue())['error'] == 'host_network_unavailable'


def test_bing_worker_uses_exact_source_contract(monkeypatch):
    from project_watch.bing_news import BingNewsAdapter, bing_news_target
    monkeypatch.setenv(host.ENV_NAME, EXE)
    monkeypatch.setattr(host.os.path, 'isfile', lambda _: True)
    calls = []
    xml = b'<rss><channel><item><title>Public example</title><link>https://news.example/a</link></item></channel></rss>'

    def exchange(executable, payload):
        calls.append(payload)
        return {'version': 1, 'body': base64.b64encode(xml).decode()}

    monkeypatch.setattr(host, 'exchange', exchange)
    result = BingNewsAdapter().fetch(bing_news_target('public company', recent=True))
    assert len(result.items) == 1
    assert calls[0]['operation'] == 'bing'
    assert BingNewsAdapter().fetch('https://attacker.example/').unavailable_error == 'disallowed_url'
    assert len(calls) == 1
    with pytest.raises(FeedError):
        worker.dispatch({'version': 1, 'operation': 'bing', 'url': 'https://attacker.example/'})


def test_bing_worker_preserves_no_redirect_and_retry_after(monkeypatch):
    from project_watch import bing_news, feeds
    monkeypatch.setattr(bing_news, 'resolve_bing', lambda _: ['8.8.8.8'])
    monkeypatch.setattr(feeds, '_pinned_request', lambda *a: (302, {'location': 'https://example.org'}, b''))
    payload = {'version': 1, 'operation': 'bing', 'url': bing_news.bing_news_target('synthetic')}
    with pytest.raises(FeedError, match='redirect_not_allowed'):
        worker.dispatch(payload)
    monkeypatch.setattr(feeds, '_pinned_request', lambda *a: (429, {'retry-after': '120'}, b''))
    output = io.BytesIO()
    worker.run(io.BytesIO(json.dumps(payload).encode()), output)
    monkeypatch.setattr(host.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a, 0, output.getvalue()))
    with pytest.raises(FeedError, match='rate_limited') as caught:
        host.exchange(EXE, payload)
    assert caught.value.retry_after == 120
