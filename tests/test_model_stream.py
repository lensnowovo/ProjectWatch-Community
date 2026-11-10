"""Offline transport faults, journal replay, and frozen legacy wire contracts."""
import io
import json
from contextlib import closing
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import ClassVar

import pytest
from test_studio_runtime_health import setup

from project_watch.studio.execution import JobExecution
from project_watch.studio.http import Http, HttpError, Response, safe_diagnostics, urllib_transport
from project_watch.studio.llm import LLM, ModelError, job_structured_options
from project_watch.studio.model_stream import IncompleteStream, read_completion
from project_watch.studio.settings import ModelConfig


def event(delta=None, finish=None, **extra):
    return ('data: ' + json.dumps({'choices': [{'index': 0, 'delta': delta or {},
            'finish_reason': finish}], **extra}, ensure_ascii=False) + '\n\n').encode()


def reply():
    return (b': heartbeat\n\n' + event({'reasoning_content': 'checking'})
            + event({'content': '{"answer":'}) + event({'content': '"合成"}'}, 'stop')
            + b'data: {"choices":[],"usage":{"prompt_tokens":12,"completion_tokens":8}}\n\n'
            + b'data: [DONE]\n\n')


class Stream(io.BytesIO):
    status = 200
    headers: ClassVar[dict] = {'content-type': 'text/event-stream', 'x-request-id': 'synthetic-123'}


def fake_network(monkeypatch, data, on_read=None, content_type=None):
    opened = []
    class NetworkStream(Stream):
        def readline(self, size=-1):
            if on_read:
                on_read()
            return super().readline(size)
    def open_(request, timeout):
        opened.append((json.loads(request.data), timeout))
        response = NetworkStream(data)
        if content_type:
            response.headers = {'content-type': content_type}
        return response
    monkeypatch.setattr('urllib.request.build_opener', lambda *a: SimpleNamespace(open=open_))
    return opened


def config():
    return ModelConfig('openai', 'https://open.bigmodel.cn/api/coding/paas/v4', 'glm-5.3-flash', 'synthetic')


def test_stream_normalizes_only_complete_reply_and_usage():
    observations = []
    data = json.loads(read_completion(Stream(reply()), 10000, observations.append))
    assert data['choices'][0]['message']['content'] == '{"answer":"合成"}'
    assert data['choices'][0]['message']['reasoning_content'] == 'checking'
    assert data['usage'] == {'prompt_tokens': 12, 'completion_tokens': 8}
    assert sum(observations) == len(reply())


def test_provider_id_is_observable_before_a_stream_disconnect():
    identities = []
    with pytest.raises(IncompleteStream):
        read_completion(Stream(event({'content': '{'}, id='provider-123')), 10000,
                        lambda n: None, identities.append)
    assert identities == ['provider-123']


@pytest.mark.parametrize('data', [
    event({'content': '{"a":'}),
    event({'content': '{}'}, 'stop'),  # Finish without DONE is not conclusive.
    event({'content': '{}'}) + b'data: [DONE]\n\n',
    b'data: {"error":{"message":"PRIVATE"}}\n\n',
    b'data: broken\n\n',
    b'data: []\n\n',
    event({'content': 'x'}, 'stop') + event({'content': 'y'}) + b'data: [DONE]\n\n',
])
def test_incomplete_or_malformed_stream_never_yields_partial_result(data):
    with pytest.raises(IncompleteStream):
        read_completion(Stream(data), 10000, lambda n: None)


def test_stream_safety_size_is_not_a_time_limit():
    with pytest.raises(IncompleteStream, match='oversized'):
        read_completion(Stream(reply()), 20, lambda n: None)


def test_sse_envelopes_and_heartbeats_do_not_consume_output_capacity():
    data = (b': heartbeat\n\n' * 100 + event({'content': 'x'}) * 100
            + event({}, 'stop') + b'data: [DONE]\n\n')
    result = json.loads(read_completion(Stream(data), 256, lambda n: None))
    assert result['choices'][0]['message']['content'] == 'x' * 100


def test_normalized_response_size_does_not_fall_into_generic_retry(monkeypatch):
    monkeypatch.setattr('project_watch.studio.http.MAX_RESPONSE_BYTES', 256)
    data = event({'content': '\x01' * 15}) * 8 + event({}, 'stop') + b'data: [DONE]\n\n'
    opened = fake_network(monkeypatch, data)
    with pytest.raises(ModelError) as error:
        LLM(config(), Http(urllib_transport())).json('s', 'u')
    assert error.value.code == 'request_outcome_unknown'
    assert len(opened) == 1


def test_transport_waits_past_fifteen_minutes_and_journals_complete_usage(tmp_path, monkeypatch):
    store, _, job = setup(tmp_path)
    execution = JobExecution(store, job)
    from project_watch.studio.runtime_health import expired_reason
    def during_read():
        with closing(store._connect()) as db:
            current = db.execute('SELECT * FROM studio_jobs WHERE id=?', (job['id'],)).fetchone()
            assert expired_reason(db, current, datetime.now(UTC) + timedelta(hours=2)) == ''
    opened = fake_network(monkeypatch, reply(), during_read)
    http = execution.http(Http(urllib_transport()), lambda u: ('model', 'synthetic'))
    options = job_structured_options(execution, config())
    llm = LLM(config(), http, json_options=options)
    assert llm.json('synthetic', 'synthetic') == {'answer': '合成'}
    assert opened == [(dict(model='glm-5.3-flash', max_tokens=131072, temperature=.2,
        messages=[{'role': 'system', 'content': 'synthetic\n\n只输出 JSON，不要输出任何解释或 Markdown。'},
                  {'role': 'user', 'content': 'synthetic'}], response_format={'type': 'json_object'},
        reasoning_effort='low', stream=True), None)]
    # A second call is local replay, not a second stream.
    assert llm.json('synthetic', 'synthetic') == {'answer': '合成'}
    assert len(opened) == 1
    with closing(store._connect()) as db:
        row = db.execute('SELECT * FROM studio_job_calls').fetchone()
        assert (row['status'], row['input_tokens'], row['output_tokens']) == ('completed', 12, 8)
        diagnostics = json.loads(db.execute("SELECT data FROM studio_job_checkpoints WHERE stage LIKE 'call_transport:%'").fetchone()[0])
        assert diagnostics['provider_request_id'] == 'synthetic-123'
        assert diagnostics['received_bytes'] == len(reply())


def test_disconnect_is_unknown_not_resent_after_restart(tmp_path, monkeypatch):
    store, _, job = setup(tmp_path)
    execution = JobExecution(store, job)
    execution.checkpoint('already-saved', {'kept': True})
    opened = fake_network(monkeypatch, event({'content': '{"a":'}))
    options = job_structured_options(execution, config())
    for _ in range(2):
        http = execution.http(Http(urllib_transport()), lambda u: ('model', 'synthetic'))
        with pytest.raises(ModelError) as error:
            LLM(config(), http, json_options=options).json('s', 'u')
        assert error.value.code in ('request_outcome_unknown', 'unknown_not_resent')
    assert len(opened) == 1
    assert execution.read_checkpoint('already-saved') == {'kept': True}
    with closing(store._connect()) as db:
        row = db.execute('SELECT * FROM studio_job_calls').fetchone()
        assert row['status'] == 'unknown' and row['input_tokens'] is None
        diagnostic = json.loads(db.execute("SELECT data FROM studio_job_checkpoints WHERE stage LIKE 'call_error:%'").fetchone()[0])
        assert diagnostic['transport']['stream_failure'] == 'missing_end'


def test_saved_legacy_options_do_not_change_request_or_cap(tmp_path):
    store, _, job = setup(tmp_path)
    execution = JobExecution(store, job)
    import hashlib
    cfg = config()
    binding = hashlib.sha256(json.dumps([cfg.protocol, cfg.base_url, cfg.model]).encode()).hexdigest()[:32]
    execution.checkpoint('model-json-options-v1:' + binding, {'reasoning_effort': 'low', 'json_object': True})
    options = job_structured_options(execution, cfg)
    assert 'stream' not in options
    from project_watch.studio.llm import _CEILINGS
    _CEILINGS.pop((cfg.protocol, cfg.base_url, cfg.model), None)
    bodies = []
    def send(*args):
        bodies.append(json.loads(args[3]))
        raise HttpError('connection_failed')
    http = execution.http(Http(send), lambda u: ('model', 'synthetic'))
    for _ in range(2):
        with pytest.raises(ModelError):
            LLM(cfg, http, json_options=job_structured_options(execution, cfg)).json('s', 'u')
    assert len(bodies) == 1
    assert 'stream' not in bodies[0] and bodies[0]['max_tokens'] == 1000000


def test_no_blanket_cap_on_compatible_gateway():
    cfg = config()
    cfg.base_url = 'https://gateway.invalid/v1'
    bodies = []
    def send(*args):
        bodies.append(json.loads(args[3]))
        return Response(200, {}, b'{"choices":[{"message":{"content":"{}"}}]}')
    LLM(cfg, Http(send)).json('s', 'u')
    assert bodies[0]['max_tokens'] == 1000000 and 'stream' not in bodies[0]


def test_mismatched_response_type_does_not_silently_fall_back(monkeypatch):
    opened = fake_network(monkeypatch, b'{}', content_type='application/json')
    with pytest.raises(ModelError):
        LLM(config(), Http(urllib_transport())).json('s', 'u')
    assert len(opened) == 1


def test_diagnostics_drop_text_urls_and_untrusted_fields():
    assert safe_diagnostics({'detail': 'PRIVATE', 'url': 'PRIVATE', 'errno': 10054,
        'stage': 'PRIVATE', 'exception': 'PRIVATE', 'provider_request_id': 'Bearer PRIVATE'}) == {'errno': 10054}


def test_socket_reset_records_typed_reason_without_resend(tmp_path, monkeypatch):
    store, _, job = setup(tmp_path)
    execution = JobExecution(store, job)
    def reset():
        raise ConnectionResetError(10054, 'PRIVATE key and URL')
    opened = fake_network(monkeypatch, reply(), reset)
    http = execution.http(Http(urllib_transport()), lambda u: ('model', 'synthetic'))
    with pytest.raises(ModelError):
        LLM(config(), http).json('s', 'u')
    assert len(opened) == 1
    with closing(store._connect()) as db:
        diagnostic = db.execute("SELECT data FROM studio_job_checkpoints WHERE stage LIKE 'call_error:%'").fetchone()[0]
        assert 'PRIVATE' not in diagnostic
        assert json.loads(diagnostic)['transport']['errno'] == 10054
        assert json.loads(diagnostic)['transport']['exception'] == 'ConnectionResetError'


def test_stream_cancel_race_preserves_only_a_complete_reply(monkeypatch):
    from project_watch.studio.http import cancel_patient_requests
    opened = fake_network(monkeypatch, reply(), cancel_patient_requests)
    # Buffered data already forms a complete reply; a stop must not lose it.
    assert LLM(config(), Http(urllib_transport())).json('s', 'u') == {'answer': '合成'}
    assert len(opened) == 1
    fake_network(monkeypatch, event({'content': '{'}), cancel_patient_requests)
    with pytest.raises(HttpError) as error:
        urllib_transport()('POST', 'https://synthetic.invalid', {}, b'{"stream":true}', None)
    assert error.value.diagnostics['cancel_reason'] == 'explicit_cancel'


def test_owner_loss_is_distinguished_from_network_failure():
    from project_watch.studio.http import TRANSPORT_OWNER, TransportTrace, _patient_control
    token = TRANSPORT_OWNER.set(lambda: False)
    try:
        with _patient_control(True) as control:
            control.trace = TransportTrace(False)
            assert control.cancelled.wait(3)
            assert control.trace.data['cancel_reason'] == 'owner_lost'
    finally:
        TRANSPORT_OWNER.reset(token)


def test_body_send_failure_is_not_labelled_waiting_for_headers(monkeypatch):
    import http.client

    from project_watch.studio.http import PatientHTTPSConnection, TransportTrace, _PatientControl
    control = _PatientControl()
    control.trace = TransportTrace(False)
    control.trace.data.update(connected_ms=1, sent_ms=2, stage='response_headers')
    def fail(*args):
        raise ConnectionResetError(10054, 'synthetic')
    monkeypatch.setattr(http.client.HTTPSConnection, 'send', fail)
    connection = PatientHTTPSConnection('synthetic.invalid', control=control)
    with pytest.raises(ConnectionResetError):
        connection.send(b'body')
    assert control.trace.data['stage'] == 'sending'


def test_rejected_new_stream_cap_replays_before_unknown_after_cache_change(tmp_path):
    from project_watch.studio.llm import _CEILINGS
    store, _, job = setup(tmp_path)
    execution = JobExecution(store, job)
    cfg = config()
    sent = []
    def transport(*args):
        budget = json.loads(args[3])['max_tokens']
        sent.append(budget)
        if budget == 131072:
            return Response(400, {}, b'{"error":{"message":"max_tokens range [1, 64000]"}}')
        raise HttpError('connection_failed')
    options = job_structured_options(execution, cfg)
    for _ in range(2):
        _CEILINGS[(cfg.protocol, cfg.base_url, cfg.model)] = 8192
        with pytest.raises(ModelError):
            LLM(cfg, execution.http(Http(transport), lambda u: ('model', 'synthetic')),
                json_options=options).json('s', 'u')
    _CEILINGS.pop((cfg.protocol, cfg.base_url, cfg.model), None)
    assert sent == [131072, 64000]
