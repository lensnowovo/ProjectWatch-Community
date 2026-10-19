import io
import ssl

import pytest

from project_watch import feeds


class Socket:
    def __init__(self, response=b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"):
        self.sent = []
        self.closed = False
        self.response = response

    def sendall(self, data):
        self.sent.append(data)

    def makefile(self, *args, **kwargs):
        return io.BytesIO(self.response)

    def close(self):
        self.closed = True


def transport(monkeypatch, connect, wrap=None, addresses=None):
    class Context:
        def wrap_socket(self, raw, **kwargs):
            assert kwargs["server_hostname"] == "example.org"
            return wrap(raw) if wrap else raw

    monkeypatch.setattr(feeds.ssl, "create_default_context", Context)
    monkeypatch.setattr(feeds.socket, "create_connection", connect)
    return feeds.GuardedHTTPS(resolver=lambda _: addresses or ["1.1.1.1", "8.8.8.8"])


def test_tcp_timeout_tries_next_ip_before_get(monkeypatch):
    calls, sock = [], Socket()

    def connect(address, **kwargs):
        calls.append(address)
        if len(calls) == 1:
            raise TimeoutError()
        return sock

    assert transport(monkeypatch, connect).get("https://example.org/article") == b"ok"
    assert len(calls) == 2
    assert len(sock.sent) == 1 and b"GET /article" in sock.sent[0] and sock.closed


def test_tls_timeout_closes_raw_socket_and_tries_next_ip(monkeypatch):
    sockets = [Socket(), Socket()]
    calls = []

    def connect(*args, **kwargs):
        calls.append(1)
        return sockets[len(calls)-1]

    def wrap(raw):
        if raw is sockets[0]:
            raise TimeoutError()
        return raw

    assert transport(monkeypatch, connect, wrap).get("https://example.org/") == b"ok"
    assert sockets[0].closed and not sockets[0].sent
    assert sockets[1].closed and len(sockets[1].sent) == 1


@pytest.mark.parametrize("mode", ["certificate", "read", "rate_limit", "forbidden"])
def test_no_replay_after_request_or_security_failure(monkeypatch, mode):
    calls, sock = [], Socket()
    if mode in {"rate_limit", "forbidden"}:
        sock.response = f"HTTP/1.1 {429 if mode == 'rate_limit' else 403} Failure\r\nContent-Length: 0\r\n\r\n".encode()

    def connect(*args, **kwargs):
        calls.append(1)
        return sock

    def wrap(raw):
        if mode == "certificate":
            raise ssl.SSLCertVerificationError()
        return raw

    client = transport(monkeypatch, connect, wrap)
    if mode == "read":
        def fail(*args, **kwargs):
            raise TimeoutError()
        monkeypatch.setattr(feeds.http.client.HTTPSConnection, "getresponse", fail)
    with pytest.raises(feeds.FeedError):
        client.get("https://example.org/")
    assert len(calls) == 1 and sock.closed


def test_all_addresses_validated_before_connect_and_attempts_bounded(monkeypatch):
    calls = []

    def connect(*args, **kwargs):
        calls.append(1)
        raise TimeoutError()

    client = transport(monkeypatch, connect, addresses=["1.1.1.1", "127.0.0.1"])
    with pytest.raises(feeds.FeedError, match="disallowed_address"):
        client.get("https://example.org/")
    assert not calls
    client = transport(monkeypatch, connect, addresses=["1.1.1.1", "8.8.8.8", "8.8.4.4", "9.9.9.9"])
    with pytest.raises(feeds.FeedError, match="connection_failed"):
        client.get("https://example.org/")
    assert len(calls) == 3


def test_redirect_resolves_and_validates_new_destination(monkeypatch):
    sock = Socket(b"HTTP/1.1 302 Found\r\nLocation: https://internal.example.org/\r\nContent-Length: 0\r\n\r\n")
    client = transport(monkeypatch, lambda *args, **kwargs: sock)
    client.resolver = lambda host: ["1.1.1.1"] if host == "example.org" else ["127.0.0.1"]
    with pytest.raises(feeds.FeedError, match="disallowed_address"):
        client.get("https://example.org/")
    assert len(sock.sent) == 1
