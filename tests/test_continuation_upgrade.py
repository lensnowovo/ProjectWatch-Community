"""Synthetic upgrade contracts; never connect to a provider or production store."""
from contextlib import closing

import pytest
from test_studio_runtime_health import age, row, setup

from project_watch.studio.execution import JobExecution
from project_watch.studio.http import Http, Response
from project_watch.studio.llm import LLM
from project_watch.studio.runtime_health import projection, tick
from project_watch.studio.settings import ModelConfig


def test_model_wait_has_no_elapsed_research_deadline(tmp_path):
    store, _, job = setup(tmp_path)
    execution = JobExecution(store, job)
    def transport():
        age(store, job)
        with closing(store._connect()) as db:
            assert db.execute('SELECT deadline_at FROM studio_job_calls').fetchone()[0] == ''
        assert tick(store, job) == 'alive'
        return Response(200, {}, b'{}')
    execution.call('model', 'long', transport, timeout=None)


@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
def test_llm_selects_patient_response_wait(protocol):
    def transport(method, url, headers, body, timeout):
        assert timeout is None
        return Response(200, {}, b'{"choices":[{"message":{"content":"ok"}}],"content":[{"type":"text","text":"ok"}]}')
    model = LLM(ModelConfig(protocol, 'https://synthetic.invalid', 'm', 'test'), Http(transport))
    assert model.complete('s', 'u') == 'ok'


def test_expired_fourth_lease_replays_results_and_fences_old_owner(tmp_path):
    store, _, job = setup(tmp_path)
    execution = JobExecution(store, job)
    execution.checkpoint('retained', {'ok': True})
    with closing(store._connect()) as db, db:
        db.execute("UPDATE studio_jobs SET attempts=4,lease_until='2000-01-01' WHERE id=?", (job['id'],))
    resumed = store.claim()
    assert resumed and resumed['id'] == job['id']
    assert resumed['lease_token'] != job['lease_token']
    assert JobExecution(store, resumed).read_checkpoint('retained') == {'ok': True}
    with pytest.raises(RuntimeError):
        execution.checkpoint('late', True)


def test_page_failure_projects_partial_without_unknown(tmp_path):
    store, _, job = setup(tmp_path)
    JobExecution(store, job).checkpoint('page-summary-v2:result', {'status': 'partial'})
    store.finish(job['id'], lease_token=job['lease_token'])
    with closing(store._connect()) as db:
        assert projection(db, row(store, job))['state'] == 'partial'


def test_independent_step_continues_without_resending_unknown(tmp_path):
    from project_watch.studio.http import HttpError
    from project_watch.studio.step_outcomes import attempt
    store, _, job = setup(tmp_path)
    execution = JobExecution(store, job)
    def lost():
        raise HttpError('connection_failed', transport_phase='response_body', failure_kind='transport_error')
    # The journal owns uncertainty; the step helper only classifies its typed result.
    with pytest.raises(HttpError):
        execution.call('model', 'old', lost, timeout=None)
    with closing(store._connect()) as db:
        before = [tuple(r) for r in db.execute('SELECT * FROM studio_job_calls')]
    assert attempt(execution, 'blocked', lambda: execution.call('model', 'old',
        lambda: pytest.fail('unknown resent')))[0] is False
    assert attempt(execution, 'sibling', lambda: execution.call('model', 'independent',
        lambda: Response(200, {}, b'{}')))[0] is True
    assert attempt(execution, 'blocked', lambda: pytest.fail('blocked action repeated'))[0] is False
    with closing(store._connect()) as db:
        assert tuple(db.execute('SELECT * FROM studio_job_calls ORDER BY rowid').fetchone()) == before[0]


@pytest.mark.parametrize('code', ['project_paused', 'model_rate_limited', 'provider_backoff', 'model_balance_exhausted'])
def test_isolation_does_not_swallow_recovery_or_permission_errors(tmp_path, code):
    from project_watch.studio.llm import ModelError
    from project_watch.studio.step_outcomes import CHECKPOINT, attempt
    store, _, job = setup(tmp_path)
    execution = JobExecution(store, job)
    def failure():
        raise ModelError('synthetic', code=code)
    with pytest.raises(ModelError):
        attempt(execution, 'step', failure)
    assert execution.read_checkpoint(CHECKPOINT) is None


def test_patient_connection_bounds_setup_but_not_inference(monkeypatch):
    from unittest.mock import Mock

    from project_watch.studio.http import PatientHTTPSConnection
    sock = Mock()
    def connect(connection):
        assert connection.timeout == 30
        connection.sock = sock
    monkeypatch.setattr('http.client.HTTPSConnection.connect', connect)
    connection = PatientHTTPSConnection('synthetic.invalid', timeout=None)
    connection.connect()
    sock.settimeout.assert_called_once_with(None)


@pytest.mark.parametrize('stop', ['explicit', 'lost_owner'])
def test_silent_socket_can_be_cancelled_without_a_retry(tmp_path, monkeypatch, stop):
    import socket
    import threading

    from project_watch.studio.http import HttpError, cancel_patient_requests, urllib_transport
    store, _, job = setup(tmp_path)
    execution = JobExecution(store, job)
    client, peer = socket.socketpair()
    peer.settimeout(5)
    def connect(connection):
        connection.sock = client
    monkeypatch.setattr('http.client.HTTPSConnection.connect', connect)
    errors = []
    def run():
        try:
            execution.call('model', 'silent', lambda: urllib_transport()(
                'POST', 'https://synthetic.invalid', {}, b'{}', None), timeout=None)
        except HttpError as exc:
            errors.append(exc.code)
    thread = threading.Thread(target=run)
    thread.start()
    try:
        data = b''
        while b'\r\n\r\n' not in data:
            data += peer.recv(65536)
        if stop == 'explicit':
            assert cancel_patient_requests() == 1
        else:
            # The job was taken away (the watchdog held it). An expired lease alone
            # no longer abandons a sent request: it may still answer after sleep.
            with closing(store._connect()) as db, db:
                db.execute("UPDATE studio_jobs SET status='failed',lease_token='',lease_until=NULL WHERE id=?",
                           (job['id'],))
        thread.join(5)
        assert not thread.is_alive()
        assert errors == ['connection_failed']
        if stop == 'lost_owner':
            with closing(store._connect()) as db, db:
                db.execute("UPDATE studio_jobs SET status='queued',retry_after=NULL WHERE id=?", (job['id'],))
            execution = JobExecution(store, store.claim())
        with pytest.raises(HttpError, match='unknown_not_resent'):
            execution.call('model', 'silent', lambda: pytest.fail('resent cancelled request'), timeout=None)
        with closing(store._connect()) as db:
            assert [r[0] for r in db.execute('SELECT status FROM studio_job_calls')] == ['unknown']
    finally:
        cancel_patient_requests()
        peer.close()
        client.close()
        thread.join(5)


@pytest.mark.parametrize('kind', ['business', 'details'])
def test_blocked_same_input_cannot_be_automatically_recreated(tmp_path, kind):
    from project_watch.studio.step_outcomes import CHECKPOINT
    store, pid, old = setup(tmp_path)
    store.finish(old['id'], lease_token=old['lease_token'])
    job = store.enqueue(pid, kind, input_version='same-material')
    job = store.claim()
    JobExecution(store, job).checkpoint_chunked(CHECKPOINT, {'batch-1': {'code': 'unknown_not_resent'}})
    store.finish(job['id'], lease_token=job['lease_token'])
    assert store.enqueue(pid, kind, input_version='same-material', automatic=True)['id'] == job['id']
    assert store.enqueue(pid, kind, input_version='new-material', automatic=True)['id'] != job['id']


def test_stop_after_complete_response_preserves_usage_and_replay(tmp_path):
    from project_watch.studio.http import HttpError
    store, _, job = setup(tmp_path)
    stopped = False
    execution = JobExecution(store, job, cancel_requested=lambda: stopped)
    response = Response(200, {}, b'{"usage":{"prompt_tokens":17,"completion_tokens":5}}')
    def transport():
        nonlocal stopped
        stopped = True
        return response
    with pytest.raises(HttpError, match='request_cancelled'):
        execution.call('model', 'complete', transport, timeout=None)
    with closing(store._connect()) as db:
        record = db.execute('SELECT status,response_body,input_tokens,output_tokens FROM studio_job_calls').fetchone()
        assert tuple(record) == ('completed', response.body, 17, 5)
    with pytest.raises(HttpError, match='request_cancelled'):
        execution.call('model', 'new', lambda: pytest.fail('new request after stop'))
    stopped = False
    assert execution.call('model', 'complete', lambda: pytest.fail('completed request resent')) == response


@pytest.mark.parametrize('complete', [True, False])
def test_http_stop_race_preserves_only_framed_complete_body(monkeypatch, complete):
    from project_watch.studio.http import HttpError, cancel_patient_requests, urllib_transport
    class Reply:
        status, headers, length, chunked = 200, {}, 2, False
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, limit):
            cancel_patient_requests()
            return b'{}' if complete else b'{'
    from types import SimpleNamespace
    monkeypatch.setattr('urllib.request.build_opener', lambda *a: SimpleNamespace(open=lambda *a, **k: Reply()))
    def call():
        return urllib_transport()('POST', 'https://synthetic.invalid', {}, b'{}', None)
    if complete:
        assert call().body == b'{}'
    else:
        with pytest.raises(HttpError, match='connection_failed'):
            call()


def test_independent_extraction_keeps_siblings_without_consuming_partial_profile(tmp_path):
    from types import SimpleNamespace

    from project_watch.studio.llm import ModelError
    from project_watch.studio.pipeline import Pipeline
    store, pid, job = setup(tmp_path)
    execution = JobExecution(store, job)
    calls = []
    inputs = {'input_version': 'stable', 'binding': 'allowed',
              'parts': [{'text': str(i)} for i in range(25)]}
    def extract(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise ModelError('synthetic', code='request_outcome_unknown')
        return {'retained_batch': len(calls)}
    pipeline = SimpleNamespace(_execution_state=SimpleNamespace(current=execution),
        store=SimpleNamespace(get_project=lambda _: {'activated': True, 'status': 'active'}),
        model_binding=lambda: 'allowed')
    def run():
        return Pipeline._extract_all_parts(pipeline, pid, inputs, SimpleNamespace(json=extract),
            'system', lambda batch: batch, 'business', lambda: inputs)
    assert run() is None  # Caller must not replace/consume the complete profile.
    assert execution.read_checkpoint('business_proposal_0') == {'retained_batch': 1}
    assert execution.read_checkpoint('business_proposal_2') == {'retained_batch': 3}
    assert run() is None
    assert len(calls) == 3
