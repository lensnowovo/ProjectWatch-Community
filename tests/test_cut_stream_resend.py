"""A provider stream that broke off is resent once, under a new journal key. Offline, synthetic."""
import json
from types import SimpleNamespace

import pytest
import test_dispatch_rule as fixtures
from test_dispatch_rule import job, ok

from project_watch.studio import dispatch
from project_watch.studio.http import HttpError, Response
from project_watch.studio.llm import LLM, ModelConfig, ModelError
from project_watch.studio.store import now


@pytest.fixture
def env(tmp_path, monkeypatch):
    return fixtures.env.__wrapped__(tmp_path, monkeypatch)


def cut(stream_failure='missing_end', headers=True, **extra):
    def send():
        diagnostics = {'route': 'direct', 'stage': 'response_body', 'exception': 'IncompleteStream',
                       'stream_failure': stream_failure, 'connected_ms': 5, 'sent_ms': 5, **extra}
        if headers:
            diagnostics.update(headers_ms=40, first_chunk_ms=40, last_chunk_ms=900)
        raise HttpError('connection_failed', transport_phase='response_body', failure_kind='transport_error',
                        diagnostics=diagnostics)
    return send


def calls(store):
    with store._connect() as db:
        return [(r['call_key'][64:], r['status'], r['retryable']) for r in
                db.execute('SELECT call_key,status,retryable FROM studio_job_calls ORDER BY rowid')]


@pytest.mark.parametrize('failure,extra', [('missing_end', {}), ('invalid_finish', {'unexpected_finish': 'repeat'})])
def test_a_cut_model_stream_is_resent_once_and_then_replayed(env, failure, extra):
    store, pid = env
    work, sent = job(store, pid, 'analyze'), []
    with pytest.raises(HttpError, match='stream_cut_resend'):
        work.call('model', 'same', cut(failure, **extra))
    work.call('model', 'same', ok(sent))
    work.call('model', 'same', ok(sent))  # the resent answer is replayed, not paid again
    assert sent == [1]
    assert calls(store) == [('', dispatch.RESENT, dispatch.CUT_RESEND), (':429:1', 'completed', 0)]
    usage = work.usage()['model']
    assert usage['unknown'] == 0 and usage['requests'] == 2  # spent twice, nothing unresolved


def test_a_second_cut_of_the_same_request_holds_for_the_owner(env):
    store, pid = env
    work, sent = job(store, pid, 'analyze'), []
    with pytest.raises(HttpError, match='stream_cut_resend'):
        work.call('model', 'same', cut())
    with pytest.raises(HttpError, match='connection_failed'):
        work.call('model', 'same', cut())
    with pytest.raises(dispatch.NotSent, match='unknown_not_resent'):
        work.call('model', 'same', ok(sent))
    assert not sent
    assert [c[1:] for c in calls(store)] == [('unknown', dispatch.CUT_RESEND), ('unknown', 0)]


def test_a_failure_before_any_response_header_still_holds(env):
    store, pid = env
    work, sent = job(store, pid, 'analyze'), []
    with pytest.raises(HttpError, match='connection_failed'):
        work.call('model', 'same', cut(headers=False))
    with pytest.raises(dispatch.NotSent):
        work.call('model', 'same', ok(sent))
    assert not sent


def test_only_model_requests_are_resent(env):
    store, pid = env
    work, sent = job(store, pid, 'collect'), []
    with pytest.raises(HttpError, match='connection_failed'):
        work.call('search', 'same', cut())
    with pytest.raises(dispatch.NotSent):
        work.call('search', 'same', ok(sent))
    assert not sent


def analysis(store, pid, input_version):
    store.enqueue(pid, 'analyze', input_version=input_version)
    claimed = store.claim()
    from project_watch.studio.execution import JobExecution
    return JobExecution(store, claimed)


def test_a_sibling_analysis_of_the_same_batch_shares_the_single_resend(env):
    store, pid = env
    first = analysis(store, pid, 'collect:b1')
    with pytest.raises(HttpError, match='stream_cut_resend'):
        first.call('model', 'same', cut())
    with store._connect() as db:
        db.execute("UPDATE studio_jobs SET status='failed',lease_token='' WHERE id=?", (first.job['id'],))
    retry, sent = analysis(store, pid, 'collect:b1'), []
    with pytest.raises(HttpError, match='connection_failed'):  # no second automatic resend
        retry.call('model', 'same', cut())
    with pytest.raises(dispatch.NotSent):
        retry.call('model', 'same', ok(sent))
    assert not sent


def test_a_stopped_job_does_not_resend(env, monkeypatch):
    store, pid = env
    work, stopped = job(store, pid, 'analyze'), []
    monkeypatch.setattr(work, 'cancel_requested', lambda: bool(stopped))
    def stop_then_cut():
        stopped.append(True)  # the owner stops the job while the stream is open
        cut()()
    with pytest.raises(HttpError, match='connection_failed'):
        work.call('model', 'same', stop_then_cut)
    assert [c[1:] for c in calls(store)] == [('unknown', 0)]


def test_the_model_client_resends_a_cut_stream_and_returns_the_answer(env):
    store, pid = env
    work, attempts = job(store, pid, 'analyze'), []
    answer = json.dumps({'choices': [{'message': {'content': '合成回答'}, 'finish_reason': 'stop'}],
                         'usage': {'prompt_tokens': 3, 'completion_tokens': 2}}).encode()

    def transport(method, url, headers, body, timeout):
        attempts.append(body)
        if len(attempts) == 1:
            cut()()
        return Response(200, {'content-type': 'application/json'}, answer)
    http = work.http(SimpleNamespace(transport=transport), lambda url: ('model', 'synthetic-provider'))
    model = LLM(ModelConfig(base_url='https://synthetic.invalid', model='synthetic', api_key='fake'), http)
    assert model.complete('system', 'user', stream=False) == '合成回答'
    assert len(attempts) == 2 and attempts[0] == attempts[1]  # the same request, sent once more
    assert [c[1] for c in calls(store)] == [dispatch.RESENT, 'completed']


def test_the_model_client_reports_a_second_cut_as_unknown(env):
    store, pid = env
    work = job(store, pid, 'analyze')

    def transport(method, url, headers, body, timeout):
        cut()()
    http = work.http(SimpleNamespace(transport=transport), lambda url: ('model', 'synthetic-provider'))
    model = LLM(ModelConfig(base_url='https://synthetic.invalid', model='synthetic', api_key='fake'), http)
    with pytest.raises(ModelError) as caught:
        model.complete('system', 'user', stream=False)
    assert caught.value.code == 'request_outcome_unknown'
    assert [c[1:] for c in calls(store)] == [('unknown', dispatch.CUT_RESEND), ('unknown', 0)]
    assert now()  # journal rows are the only effect


@pytest.mark.parametrize('failure,extra', [
    ('invalid_finish', {}),  # a second end event: protocol behaviour, would repeat
    ('missing_finish', {}),  # [DONE] without a finish value: would repeat
    ('missing_end', {'cancel_reason': 'owner_lost'}),  # this side closed the stream
    ('missing_end', {'cancel_reason': 'explicit_cancel'}),
])
def test_streams_that_would_end_the_same_way_or_were_closed_here_are_not_resent(env, failure, extra):
    store, pid = env
    work, sent = job(store, pid, 'analyze'), []
    with pytest.raises(HttpError, match='connection_failed'):
        work.call('model', 'same', cut(failure, **extra))
    with pytest.raises(dispatch.NotSent):
        work.call('model', 'same', ok(sent))
    assert not sent


def test_the_resend_is_made_even_after_ordinary_attempts_were_used(monkeypatch):
    from project_watch.studio import llm as llm_module
    from project_watch.studio.http import Http

    monkeypatch.setattr(llm_module.time, 'sleep', lambda seconds: None)
    answer = json.dumps({'choices': [{'message': {'content': '合成回答'}, 'finish_reason': 'stop'}]}).encode()
    attempts = []

    def transport(method, url, headers, body, timeout):
        attempts.append(1)
        if len(attempts) <= 2:
            raise HttpError('synthetic_transient')  # uses up two ordinary attempts
        if len(attempts) == 3:
            raise HttpError('stream_cut_resend')  # the journal allowed one resend
        return Response(200, {'content-type': 'application/json'}, answer)
    model = LLM(ModelConfig(base_url='https://synthetic.invalid', model='synthetic', api_key='fake'), Http(transport))
    assert model.complete('system', 'user', stream=False) == '合成回答'
    assert len(attempts) == 4


def test_each_request_of_a_resend_keeps_its_own_diagnostic(env):
    store, pid = env
    work = job(store, pid, 'analyze')
    with pytest.raises(HttpError, match='stream_cut_resend'):
        work.call('model', 'same', cut())
    with pytest.raises(HttpError, match='connection_failed'):
        work.call('model', 'same', cut())
    with store._connect() as db:
        saved = {r[0][len('call_error:'):][40:]: json.loads(r[1])['auto_resend']
                 for r in db.execute("SELECT stage,data FROM studio_job_checkpoints WHERE stage LIKE 'call_error:%'")}
    assert saved == {'': True, ':429:1': False}
