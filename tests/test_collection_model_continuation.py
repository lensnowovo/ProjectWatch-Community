"""Resume through the host entry, without replaying collection or a lost model call."""
import json
import os
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import test_collection_host as fixtures
from test_collection_host import response

from project_watch.studio import collection_host, dsh_runtime, recovery
from project_watch.studio.collection_ledger import STAGE
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import Http, HttpError, Response


@pytest.fixture
def setup(tmp_path):
    return fixtures.setup.__wrapped__(tmp_path)


def request(research, text):
    return {'model': 'synthetic', 'messages': [{'role': 'user', 'content': text}],
            'tools': [{'type': 'function', 'function': {'name': name}} for name in research.tools]}


def fail_round(setup, monkeypatch, mode):
    pipeline, execution = setup
    monkeypatch.setenv('PROJECT_WATCH_DSH_RUNTIME', 'synthetic-runtime')
    monkeypatch.setenv('PROJECT_WATCH_DSH_MONITOR', '1')
    execution.checkpoint('recovery_inputs', {'binding': recovery.input_binding(pipeline, execution.job['project_id'])})
    calls = []

    def send(_method, url, _headers, raw, _timeout):
        calls.append((urlsplit(url).hostname, raw))
        if urlsplit(url).hostname == 'model.example.invalid':
            raise HttpError('connection_failed', transport_phase='connect_or_headers', failure_kind='timeout')
        return response(url)

    pipeline.http_factory = lambda _: Http(send)

    def interrupted(research, gateway, *_args):
        status = research.tool('pw_status', {})
        sid = status['collected_sources']['sources'][0]['id']
        research.tool('pw_read', {'source_id': sid})
        research.tool('pw_record', {'kind': 'business', 'source_id': sid, 'quote': 'Synthetic abstract',
                                   'text': 'Synthetic abstract', 'label': 'Saved finding'})
        gateway.model_call(request(research, 'original lost request'))

    monkeypatch.setattr(dsh_runtime, 'run_dsh', interrupted)
    entry = dsh_runtime.run_bootstrap if mode == 'first' else dsh_runtime.run_monitor
    with pytest.raises(HttpError):
        entry(pipeline, execution, None)
    assert len(execution.read_checkpoint('dsh_state')['records']) == 1
    assert execution.usage()['model']['unknown'] == 1
    pipeline.store.finish(execution.job['id'], error='request_outcome_unknown', lease_token=execution.job['lease_token'])
    return pipeline, execution, calls, entry


def authorize(pipeline, execution):
    state = recovery.preview(pipeline, execution.job['project_id'], execution.job['id'])
    assert state['can_recover'], state['reason']
    args = dict(expected_version=state['version'], idempotency_key='continue-once', accept_duplicate_cost=True)
    first = recovery.authorize(pipeline, execution.job['project_id'], execution.job['id'], **args)
    assert recovery.authorize(pipeline, execution.job['project_id'], execution.job['id'], **args) == first
    resumed = JobExecution(pipeline.store, pipeline.store.claim())
    pipeline._execution_state.current = resumed
    return resumed


@pytest.mark.parametrize('mode', ['first', 'incremental'])
def test_full_entry_continues_saved_work_without_old_request_or_collection_replay(setup, monkeypatch, mode):
    pipeline, previous, calls, entry = fail_round(setup, monkeypatch, mode)
    state = deepcopy(previous.read_checkpoint('dsh_state'))
    manifest = deepcopy(previous.read_checkpoint(STAGE))
    old_count = len(calls)
    execution = authorize(pipeline, previous)

    def send(_method, url, _headers, raw, _timeout):
        assert urlsplit(url).hostname == 'model.example.invalid', 'must not recollect'
        assert 'original lost request' not in raw.decode()
        calls.append((urlsplit(url).hostname, raw))
        return Response(200, {}, json.dumps({'choices': [{'message': {'role': 'assistant', 'content': 'Continue'},
                                                        'finish_reason': 'stop'}]}).encode())

    pipeline.http_factory = lambda _: Http(send)

    def continued(research, gateway, _root, directory):
        assert directory.name.startswith('r-')
        assert research.state == state
        assert execution.read_checkpoint(STAGE) == manifest
        with pytest.raises(HttpError, match='request_outcome_unknown'):
            research.tool('pw_status', {})
        payload = request(research, 'continue from saved checkpoint')
        gateway.model_call(payload)
        gateway.model_call(payload)  # settled response reused, no second send
        assert execution.read_checkpoint('dsh_hold') == {}
        assert research.tool('pw_status', {})['collected_sources']['sources']

    monkeypatch.setattr(dsh_runtime, 'run_dsh', continued)
    entry(pipeline, execution, None)
    assert len(calls) == old_count + 1
    assert execution.read_checkpoint('dsh_state')['records'] == state['records']
    with closing(pipeline.store._connect()) as db:
        statuses = [r[0] for r in db.execute("SELECT status FROM studio_job_calls WHERE job_id=? AND kind='model'",
                                            (execution.job['id'],))]
    assert sorted(statuses) == ['completed', 'unknown']


@pytest.mark.parametrize('mode', ['first', 'incremental'])
def test_full_entry_new_timeout_keeps_both_receipts_and_never_reuses_authorization(setup, monkeypatch, mode):
    pipeline, previous, calls, entry = fail_round(setup, monkeypatch, mode)
    execution = authorize(pipeline, previous)
    count = len(calls)
    state = deepcopy(execution.read_checkpoint('dsh_state'))

    def continued(research, gateway, *_args):
        gateway.model_call(request(research, 'continue from checkpoint'))

    monkeypatch.setattr(dsh_runtime, 'run_dsh', continued)
    for _ in range(2):
        with pytest.raises(HttpError):
            entry(pipeline, execution, None)
    assert len(calls) == count + 1
    assert execution.read_checkpoint('dsh_state') == state
    with closing(pipeline.store._connect()) as db:
        assert db.execute("SELECT count(*) FROM studio_job_calls WHERE job_id=? AND status='unknown'",
                          (execution.job['id'],)).fetchone()[0] == 2


@pytest.mark.parametrize('change', ['no_authorization', 'paused', 'config', 'scope', 'other_unknown', 'pending_collection'])
def test_continuation_preparation_keeps_boundaries(setup, monkeypatch, change):
    pipeline, previous, calls, entry = fail_round(setup, monkeypatch, 'first')
    execution = authorize(pipeline, previous) if change != 'no_authorization' else previous
    if change == 'paused':
        pipeline.store.set_status(previous.job['project_id'], 'paused')
    elif change == 'config':
        settings = pipeline.settings_store.load()
        settings.model.model = 'different'
        pipeline.settings_store.save(settings)
    elif change == 'scope':
        scope = execution.read_checkpoint(collection_host.SCOPE)
        scope['settings_binding'] = 'changed'
        execution.checkpoint_chunked(collection_host.SCOPE, scope)
    elif change == 'other_unknown':
        with pytest.raises(HttpError):
            execution.call('search', 'other-request', lambda: (_ for _ in ()).throw(HttpError('connection_failed')))
    elif change == 'pending_collection':
        manifest = execution.read_checkpoint(STAGE)
        manifest['receipts'] = []
        execution.checkpoint_chunked(STAGE, manifest)
    count = len(calls)
    monkeypatch.setattr(dsh_runtime, 'run_dsh', lambda *_: pytest.fail('must stop before model'))
    with pytest.raises((HttpError, ValueError, RuntimeError)):
        entry(pipeline, execution, None)
    assert len(calls) == count


@pytest.mark.skipif(not os.environ.get('PW_DSH_TEST_RUNTIME'), reason='requires pinned official DSH')
def test_native_dsh_continuation_finishes_with_saved_record_and_review(setup, monkeypatch):
    from test_studio_dsh_monitor import synthetic_review

    native_run = dsh_runtime.run_dsh
    pipeline, previous, calls, entry = fail_round(setup, monkeypatch, 'first')
    execution = authorize(pipeline, previous)
    records = deepcopy(execution.read_checkpoint('dsh_state')['records'])
    original_calls = len(calls)

    def send(_method, url, _headers, raw, _timeout):
        assert urlsplit(url).hostname == 'model.example.invalid'
        payload = json.loads(raw)
        assert 'original lost request' not in raw.decode()
        calls.append((urlsplit(url).hostname, raw))
        if 'tools' in payload:
            message = {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': 'resume-finish', 'type': 'function',
                 'function': {'name': 'pw_finish', 'arguments': '{"gaps":{}}'}}]}
            reason = 'tool_calls'
        else:
            review = json.loads(payload['messages'][-1]['content'])
            message = {'role': 'assistant', 'content': json.dumps(synthetic_review(review))}
            reason = 'stop'
        return Response(200, {}, json.dumps({'choices': [{'message': message, 'finish_reason': reason}]}).encode())

    pipeline.http_factory = lambda _: Http(send)
    monkeypatch.setattr(dsh_runtime, 'run_dsh', native_run)
    result = entry(pipeline, execution, Path(os.environ['PW_DSH_TEST_RUNTIME']))
    assert result['status'] == 'partial'  # original unconfigured sources remain disclosed
    assert execution.read_checkpoint('dsh_record_admission')['accepted'] == 1
    assert execution.read_checkpoint('dsh_state')['records'] == records
    assert len(pipeline.store.get_public_profile(execution.job['project_id'])['facts']) == 1
    assert len(calls) == original_calls + 2  # new continuation and mandatory record review
    with closing(pipeline.store._connect()) as db:
        assert db.execute("SELECT count(*) FROM studio_job_calls WHERE job_id=? AND status='unknown'",
                          (execution.job['id'],)).fetchone()[0] == 1
