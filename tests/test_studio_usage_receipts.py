"""Usage accounting from synthetic dispatch receipts, never provider invoices."""
import json
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from project_watch.studio.app import create_studio_app
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import Http, HttpError, Response
from project_watch.studio.llm import LLM, ModelError
from project_watch.studio.settings import ModelConfig


def configured(tmp_path, response):
    app = create_studio_app(tmp_path, start_worker=False)
    pipeline = app.state.studio_pipeline
    pipeline.settings_store.update({'model': {'protocol': 'openai',
        'base_url': 'https://model.example/v1', 'model': 'synthetic', 'api_key': 'PRIVATE'}})
    calls = []
    def transport(*_args):
        calls.append(1)
        if isinstance(response, BaseException):
            raise response
        return response
    pipeline.http_factory = lambda _: Http(transport)
    return app, pipeline, calls


def reply(usage=None, *, status=200, text='ok'):
    data = {'choices': [{'message': {'content': text}}]}
    if usage is not None:
        data['usage'] = usage
    return Response(status, {}, json.dumps(data).encode())


def model_call(pipeline, response, *, leased=True):
    pid = pipeline.repository.create_project('Synthetic Usage').id
    pipeline.store.upsert_project(pid, {}, consent=True)
    pipeline.run_external = lambda _: pipeline.tools()[2].complete('s', 'u')
    pipeline.store.enqueue(pid, 'external')
    job = pipeline.store.claim()
    if leased:
        pipeline.run(job)
    else:
        pipeline.tools()[2].complete('s', 'u')
    return pid, job


@pytest.mark.parametrize('leased', [True, False])
@pytest.mark.parametrize('usage,known,input_value,output_value', [
    ({'prompt_tokens': 10, 'completion_tokens': 4}, 1, 10, 4),
    ({'prompt_tokens': 0, 'completion_tokens': 0}, 1, 0, 0),
    (None, 0, 0, 0),
    ({'prompt_tokens': 7}, 0, 7, 0),
    ({'prompt_tokens': -1, 'completion_tokens': True}, 0, 0, 0),
    ({'prompt_tokens': '12', 'completion_tokens': 1.5}, 0, 0, 0),
])
def test_usage_retains_missing_and_partial_fields(tmp_path, leased, usage, known, input_value, output_value):
    app, pipeline, calls = configured(tmp_path, reply(usage))
    model_call(pipeline, reply(usage), leased=leased)
    total = TestClient(app).get('/api/studio/usage').json()['total']
    assert len(calls) == total['calls'] == 1
    assert total['known_calls'] == known
    assert total['unknown_calls'] == 1 - known
    assert total['input_tokens'] == input_value and total['output_tokens'] == output_value
    assert total['legacy_calls'] == 0
    assert 'PRIVATE' not in json.dumps(total)


def test_usage_is_saved_before_response_parsing_and_survives_crash(tmp_path):
    app, pipeline, _calls = configured(tmp_path, reply({'prompt_tokens': 7, 'completion_tokens': 3}))
    pid = pipeline.repository.create_project('Synthetic Crash').id
    pipeline.store.upsert_project(pid, {}, consent=True)
    pipeline.store.enqueue(pid, 'external')
    job = pipeline.store.claim()
    execution = JobExecution(pipeline.store, job)
    execution.call('model', 'lost-after-response', lambda: reply({'prompt_tokens': 7, 'completion_tokens': 3}))
    # No LLM callback was reached; usage is part of the durable HTTP receipt.
    execution.call('model', 'lost-after-response', lambda: pytest.fail('must replay'))
    total = TestClient(app).get('/api/studio/usage').json()['total']
    assert total['calls'] == total['known_calls'] == 1
    assert (total['input_tokens'], total['output_tokens']) == (7, 3)
    assert execution.usage()['model']['cache_hits'] == 1


def test_invalid_json_and_http_denial_still_count_requests(tmp_path):
    app, pipeline, _ = configured(tmp_path, reply())
    pid = pipeline.repository.create_project('Synthetic Errors').id
    pipeline.store.upsert_project(pid, {}, consent=True)
    pipeline.store.enqueue(pid, 'external')
    job = pipeline.store.claim()
    execution = JobExecution(pipeline.store, job)
    execution.call('model', 'malformed', lambda: Response(200, {}, b'not JSON'))
    execution.call('model', 'denied', lambda: reply(status=401))
    total = TestClient(app).get('/api/studio/usage').json()['total']
    assert total['calls'] == total['unknown_calls'] == 2
    assert total['known_calls'] == 0


def test_direct_connection_failure_not_implicitly_resent_and_counted_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr('project_watch.studio.llm.time.sleep', lambda _: None)
    app, pipeline, calls = configured(tmp_path, HttpError('connection_failed', detail='PRIVATE lost connection'))
    with pytest.raises(ModelError):
        pipeline.tools()[2].complete('s', 'u')
    assert len(calls) == 1
    total = TestClient(app).get('/api/studio/usage').json()['total']
    assert total['calls'] == total['unknown_calls'] == 1


def test_anthropic_partial_usage_and_cache_input_are_not_double_added(tmp_path):
    app, pipeline, _ = configured(tmp_path, reply())
    pipeline.settings_store.update({'model': {'protocol': 'anthropic', 'api_key': 'PRIVATE'}})
    pipeline.http_factory = lambda _: Http(lambda *_: Response(200, {}, json.dumps({
        'content': [{'type': 'text', 'text': 'ok'}], 'usage': {'input_tokens': 2,
            'output_tokens': 5, 'cache_read_input_tokens': 20, 'cache_creation_input_tokens': 3}}).encode()))
    pipeline.tools()[2].complete('s', 'u')
    total = TestClient(app).get('/api/studio/usage').json()['total']
    assert total['input_tokens'] == 2 and total['output_tokens'] == 5
    assert total['cache_read_tokens'] == 20 and total['cache_creation_tokens'] == 3


def test_legacy_daily_aggregate_is_preserved_without_claiming_coverage(tmp_path):
    app, pipeline, _ = configured(tmp_path, reply({'prompt_tokens': 7, 'completion_tokens': 3}))
    pipeline.store.record_usage(100, 50)
    pipeline.tools()[2].complete('s', 'u')
    total = TestClient(app).get('/api/studio/usage').json()['total']
    assert total['calls'] == 2 and total['legacy_calls'] == 1
    assert total['known_calls'] == 1 and total['unknown_calls'] == 0
    assert (total['input_tokens'], total['output_tokens']) == (107, 53)
    # Reads have no model calls and disclose neither headers nor provider bodies.
    TestClient(app).get('/api/studio/usage')
    with closing(pipeline.store._connect()) as db:
        assert db.execute('SELECT count(*) FROM studio_direct_model_calls').fetchone()[0] == 1


def test_callback_missing_usage_not_fabricated_zero():
    seen = []
    llm = LLM(ModelConfig('openai', 'https://model.example', 'm', 'synthetic'),
        Http(lambda *_: reply()), on_usage=lambda i, o: seen.append((i, o)))
    assert llm.complete('s', 'u') == 'ok'
    assert seen == [(None, None)]


def test_reopen_legacy_journal_and_daily_totals_not_counted_twice(tmp_path):
    from project_watch.studio.store import StudioStore

    _app, pipeline, _ = configured(tmp_path, reply({'prompt_tokens': 7, 'completion_tokens': 3}))
    pid = pipeline.repository.create_project('Synthetic Upgrade').id
    pipeline.store.upsert_project(pid, {}, consent=True)
    pipeline.store.enqueue(pid, 'external')
    job = pipeline.store.claim()
    with closing(pipeline.store._connect()) as db, db:
        db.execute("INSERT INTO studio_job_calls(job_id,call_key,kind,provider,status,created_at) "
                   "VALUES (?, 'old', 'model', 'legacy', 'completed', datetime('now'))", (job['id'],))
    pipeline.store.record_usage(100, 50)
    pipeline.store = StudioStore(pipeline.store.db_path)
    pipeline.tools()[2].complete('s', 'u')
    total = pipeline.store.usage()[0]
    assert total['calls'] == 2 and total['legacy_calls'] == 1
    assert total['known_calls'] == 1


def test_json_repair_records_both_attempts_with_known_counts(tmp_path):
    app, pipeline, _ = configured(tmp_path, reply())
    responses = iter([reply({'prompt_tokens': 7, 'completion_tokens': 3}, text='bad'),
                      reply({'prompt_tokens': 9, 'completion_tokens': 4}, text='{"ok":true}')])
    pipeline.http_factory = lambda _: Http(lambda *_: next(responses))
    assert pipeline.tools()[2].json('s', 'u') == {'ok': True}
    total = TestClient(app).get('/api/studio/usage').json()['total']
    assert total['calls'] == total['known_calls'] == 2
    assert (total['input_tokens'], total['output_tokens']) == (16, 7)


def test_ceiling_negotiation_records_rejection_in_denominator(tmp_path):
    app, pipeline, _ = configured(tmp_path, reply())
    responses = iter([Response(400, {}, b'{"error":{"message":"max_tokens range [1, 8192]"}}'),
                      reply({'prompt_tokens': 9, 'completion_tokens': 4})])
    pipeline.settings_store.update({'model': {'model': 'usage-ceiling-fixture'}})
    pipeline.http_factory = lambda _: Http(lambda *_: next(responses))
    assert pipeline.tools()[2].complete('s', 'u') == 'ok'
    total = TestClient(app).get('/api/studio/usage').json()['total']
    assert total['calls'] == 2 and total['known_calls'] == total['unknown_calls'] == 1


def test_usage_read_30_calendar_days_not_30_nonempty_days(tmp_path):
    from datetime import UTC, datetime, timedelta

    app, pipeline, _ = configured(tmp_path, reply())
    old_day = (datetime.now(UTC).date() - timedelta(days=31)).isoformat()
    with closing(pipeline.store._connect()) as db, db:
        db.execute('INSERT INTO studio_usage VALUES (?,1,999,999)', (old_day,))
    assert TestClient(app).get('/api/studio/usage').json()['total']['calls'] == 0


def test_project_usage_is_scoped_stored_only_and_retains_limits_replays(tmp_path):
    app, pipeline, calls = configured(tmp_path, reply())
    pid = pipeline.repository.create_project('Synthetic Local Accounting').id
    pipeline.store.upsert_project(pid, {}, consent=True)
    pipeline.store.enqueue(pid, 'competition')
    job = pipeline.store.claim()
    execution = JobExecution(pipeline.store, job, budgets={'model': 32, 'search': 24})
    execution.call('model', 'synthetic', lambda: reply({'prompt_tokens': 7, 'completion_tokens': 3}))
    execution.call('model', 'synthetic', lambda: pytest.fail('must replay'))
    result = TestClient(app).get(f'/api/studio/projects/{pid}/usage').json()
    assert result['token_budget'] is None and len(result['jobs']) == 1
    observed = result['jobs'][0]
    assert observed['purpose'] == 'competition' and 'lease_token' not in observed
    kind = observed['kinds'][0]
    assert kind['requests'] == kind['known_usage'] == kind['replay_hits'] == 1
    assert kind['min_request_limit'] == kind['max_request_limit'] == 32
    assert kind['timed_requests'] == 1 and kind['elapsed_ms'] >= 0
    assert calls == []
    other = pipeline.repository.create_project('Synthetic Other Project').id
    pipeline.store.upsert_project(other, {}, consent=True)
    assert TestClient(app).get(f'/api/studio/projects/{other}/usage').json()['jobs'] == []
    assert TestClient(app).get('/api/studio/projects/missing/usage').status_code == 404


def test_evaluation_counter_captures_failure_and_partial_usage():
    from project_watch.studio.usage import UsageCounter

    counter = UsageCounter()
    responses = iter([reply({'prompt_tokens': 3, 'completion_tokens': 2}), reply({'prompt_tokens': 4})])
    observed = counter.http(Http(lambda *_: next(responses)))
    observed.post_json('https://model.example', {})
    observed.post_json('https://model.example', {})
    failing = counter.http(Http(lambda *_: (_ for _ in ()).throw(HttpError('connection_failed'))))
    with pytest.raises(HttpError):
        failing.post_json('https://model.example', {})
    assert counter.totals['provider_requests'] == 3
    assert counter.totals['known_calls'] == 1 and counter.totals['unknown_calls'] == 2
    assert counter.totals['input_tokens'] == 7 and counter.totals['output_tokens'] == 2


def test_old_journal_schema_upgrades_additively_without_rewriting_receipts(tmp_path):
    from project_watch.studio.store import StudioStore

    _app, pipeline, _ = configured(tmp_path, reply())
    pid = pipeline.repository.create_project('Synthetic Old Schema').id
    pipeline.store.upsert_project(pid, {}, consent=True)
    pipeline.store.enqueue(pid, 'external')
    job = pipeline.store.claim()
    with closing(pipeline.store._connect()) as db, db:
        # Disposable synthetic database only; this reproduces the old release DDL.
        db.execute('DROP TABLE studio_job_calls')
        db.execute('''CREATE TABLE studio_job_calls (
            job_id TEXT NOT NULL REFERENCES studio_jobs(id), call_key TEXT NOT NULL,
            kind TEXT NOT NULL, provider TEXT NOT NULL, status TEXT NOT NULL,
            response_status INTEGER, response_headers TEXT, response_body BLOB,
            created_at TEXT NOT NULL, PRIMARY KEY(job_id,call_key))''')
        db.execute("INSERT INTO studio_job_calls VALUES (?,'old','model','hashed','completed',200,'{}',?,datetime('now'))",
                   (job['id'], b'old-receipt'))
    upgraded = StudioStore(pipeline.store.db_path)
    with closing(upgraded._connect()) as db:
        row = dict(db.execute("SELECT * FROM studio_job_calls WHERE call_key='old'").fetchone())
        assert row['response_body'] == b'old-receipt' and row['usage_version'] == 0
        assert row['input_tokens'] is None and row['request_limit'] is None
    assert upgraded.usage() == []  # No inference or counting a second old authority.
