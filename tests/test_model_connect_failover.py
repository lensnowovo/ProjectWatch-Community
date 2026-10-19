import io
import ssl
import urllib.request

import pytest

from project_watch import model_transport as transport


class Socket:
    def __init__(self):
        self.sent = []
        self.closed = False

    def sendall(self, data):
        self.sent.append(data)

    def makefile(self, *args, **kwargs):
        return io.BytesIO(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")

    def settimeout(self, value):
        pass

    def close(self):
        self.closed = True


def setup(monkeypatch, wrap=None):
    monkeypatch.setattr(transport, "_resolve_public", lambda _: ["93.184.216.34", "1.1.1.1"])

    class Context:
        def wrap_socket(self, raw, **kwargs):
            return wrap(raw) if wrap else raw

    monkeypatch.setattr(transport.ssl, "create_default_context", Context)


def test_connect_timeout_tries_next_public_ip_before_single_post(monkeypatch):
    setup(monkeypatch)
    sock = Socket()
    calls = []

    def connect(address, **kwargs):
        calls.append(address)
        if len(calls) == 1:
            raise TimeoutError()
        return sock

    monkeypatch.setattr(transport.socket, "create_connection", connect)
    assert (
        transport.PublicModelOpener()
        .open(urllib.request.Request("https://example.org/model", data=b"body"))
        .read()
        == b"{}"
    )
    assert len(calls) == 2 and sum(b"POST " in b for b in sock.sent) == 1
    assert sum(b == b"body" for b in sock.sent) == 1 and sock.closed


def test_no_failover_after_request_sent_or_certificate_failure(monkeypatch):
    setup(monkeypatch)
    sock = Socket()
    calls = []

    def connect(*args, **kwargs):
        calls.append(1)
        return sock

    monkeypatch.setattr(transport.socket, "create_connection", connect)

    def fail_read(*args, **kwargs):
        raise TimeoutError()

    monkeypatch.setattr(transport.http.client.HTTPSConnection, "getresponse", fail_read)
    with pytest.raises(TimeoutError):
        transport.PublicModelOpener().open(
            urllib.request.Request("https://example.org/model", data=b"body")
        )
    assert len(calls) == 1
    calls.clear()

    def fail_cert(raw):
        raise ssl.SSLCertVerificationError()

    setup(monkeypatch, fail_cert)
    with pytest.raises(ssl.SSLCertVerificationError):
        transport.PublicModelOpener().open(
            urllib.request.Request("https://example.org/model", data=b"body")
        )
    assert len(calls) == 1 and sock.closed
