import json
import ssl
import urllib.error

import pytest

from project_watch import model_transport as transport


def setup(monkeypatch, failures=(), payload=None, status=200):
    calls, closed, requests = [], [], []
    payload = payload if payload is not None else {"Status": 0, "Answer": [{"type": 1, "data": "8.8.8.8"}]}

    class Connection:
        def __init__(self, host, timeout):
            self.host = host
            calls.append(host)

        def request(self, method, path, headers):
            requests.append((method, path, headers))
            if len(calls) <= len(failures):
                raise failures[len(calls)-1]

        def getresponse(self):
            return type("Response", (), {"status": status, "read": lambda self, limit: json.dumps(payload).encode()})()

        def close(self):
            closed.append(self.host)

    monkeypatch.setattr(transport.http.client, "HTTPSConnection", Connection)
    return calls, closed, requests


def test_dns_switches_only_between_fixed_public_resolvers(monkeypatch):
    calls, closed, requests = setup(monkeypatch, failures=[TimeoutError()])
    assert transport._public_dns("example.org") == ["8.8.8.8"]
    assert calls == closed == ["1.1.1.1", "1.0.0.1"]
    assert all(r == ("GET", "/dns-query?name=example.org&type=A", {"Accept": "application/dns-json"}) for r in requests)


def test_dns_failover_is_bounded(monkeypatch):
    calls, closed, _ = setup(monkeypatch, failures=[TimeoutError(), TimeoutError()])
    with pytest.raises(urllib.error.URLError):
        transport._public_dns("example.org")
    assert calls == closed == ["1.1.1.1", "1.0.0.1"]


@pytest.mark.parametrize("payload", [
    {"Status": 0, "Answer": [{"type": 1, "data": "127.0.0.1"}]},
    {"Status": 0, "Answer": [{"type": 1, "data": "8.8.8.8"}, {"type": 1, "data": "10.0.0.1"}]},
    {"Status": 0, "Answer": [{"type": 1, "data": "not-an-ip"}]},
    {"Status": 3}, [],
])
def test_invalid_or_private_dns_response_not_retried(monkeypatch, payload):
    calls, closed, _ = setup(monkeypatch, payload=payload)
    with pytest.raises(urllib.error.URLError):
        transport._public_dns("example.org")
    assert calls == closed == ["1.1.1.1"]


def test_bad_certificate_not_retried(monkeypatch):
    calls, closed, _ = setup(monkeypatch, failures=[ssl.SSLCertVerificationError()])
    with pytest.raises(ssl.SSLCertVerificationError):
        transport._public_dns("example.org")
    assert calls == closed == ["1.1.1.1"]


def test_http_rejection_not_retried(monkeypatch):
    calls, closed, _ = setup(monkeypatch, status=429)
    with pytest.raises(urllib.error.URLError):
        transport._public_dns("example.org")
    assert calls == closed == ["1.1.1.1"]
