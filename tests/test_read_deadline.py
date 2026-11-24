"""A timed read that trickles in ends at the transport, before the watchdog holds the job."""
from types import SimpleNamespace

import pytest

from project_watch.studio import http as transport_module
from project_watch.studio.http import HttpError, urllib_transport


def trickling(monkeypatch, chunks, clock):
    class Reply:
        status, headers, length, chunked = 200, {}, None, False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read1(self, limit):
            clock[0] += 10  # each small chunk arrives within the socket timeout
            return chunks.pop(0) if chunks else b''

        def read(self, limit):
            # A plain read waits until it has the whole chunk: a small trickling body never returns early.
            data = b''
            while len(data) < limit and chunks:
                data += self.read1(limit)
            return data
    monkeypatch.setattr('urllib.request.build_opener', lambda *a: SimpleNamespace(open=lambda *a, **k: Reply()))
    monkeypatch.setattr(transport_module.time, 'monotonic', lambda: clock[0])


def test_a_trickling_body_past_its_total_time_is_a_timed_out_read(monkeypatch):
    clock = [0.0]
    trickling(monkeypatch, [b'x'] * 100, clock)  # a 100-byte body, far under one 64 KiB read
    with pytest.raises(HttpError) as raised:
        urllib_transport()('GET', 'https://synthetic.invalid/page', {}, None, 20)
    assert raised.value.code == 'connection_failed'
    assert raised.value.transport_phase == 'response_body' and raised.value.failure_kind == 'timeout'
    assert clock[0] <= 20 + transport_module.TOTAL_GRACE_SECONDS + 10 < 20 + 60  # before the watchdog's grace


def test_a_body_that_arrives_in_time_is_read_whole(monkeypatch):
    clock = [0.0]
    trickling(monkeypatch, [b'{"a":', b' 1}'], clock)
    assert urllib_transport()('GET', 'https://synthetic.invalid/page', {}, None, 20).body == b'{"a": 1}'
