"""Real admission/journal transactions with synthetic providers, never live keys."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from project_watch.studio import acceptance_batch as batch
from project_watch.studio import collection_host, event_insight_work
from project_watch.studio import research_budget as cost
from project_watch.studio.app import create_studio_app
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import Http, HttpError, Response
from project_watch.studio.store import StudioStore

URL = 'https://model.example.invalid/v1/chat/completions'


def contract(ceiling=10000):
    return dict(version=1, currency='CNY', basis='reference_tariff', ceiling=ceiling,
        valid_until=(datetime.now(UTC) + timedelta(hours=2)).isoformat(),
        phase_reserves=dict.fromkeys(cost.PHASES, 0), public_reads_included=True,
        tariffs=[dict(kind='model', endpoint=URL, model='synthetic',
            input_per_token=2, output_per_token=4, context_tokens=1000,
            output_tokens=100, wire_bytes=65536, source='https://example.invalid/pricing')])


def setup(tmp_path, policy=None, n=80):
    app = create_studio_app(tmp_path, start_worker=False)
    pipeline = app.state.studio_pipeline
    pid = pipeline.repository.create_project('Budget Synthetic').id
    pipeline.store.upsert_project(pid, {'legal_name': 'Budget Synthetic', 'website': ''}, consent=True)
    job = pipeline.store.enqueue(pid, 'external')
    batch.create(pipeline.store, 'cost-window', deadline=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        caps=dict.fromkeys(batch.KINDS, n), journeys=[dict(project_id=pid,
        caps=dict.fromkeys(batch.KINDS, n), roots=['first', 'next'])], plan_sha256='a' * 64,
        cost_policy=policy or contract())
    batch.bind_root(pipeline.store, 'cost-window', job['id'], 'first')
    return pipeline, JobExecution(pipeline.store, pipeline.store.claim())


def send(execution, key='one', *, transport=None, url=URL, output=100, model='synthetic'):
    body = json.dumps(dict(model=model, messages=[dict(role='user', content=key)],
                           stream=False, max_tokens=output)).encode()
    http = execution.http(Http(transport or (lambda *args: Response(200, {}, json.dumps(
        dict(usage=dict(prompt_tokens=10, completion_tokens=10))).encode()))), lambda url: ('model', 'synthetic'))
    return http.transport('POST', url, {}, body, 30)


def journal(store):
    with closing(store._connect()) as db:
        return [dict(r) for r in db.execute('SELECT * FROM studio_job_calls ORDER BY rowid')]


def test_normal_http_clamps_before_hash_settles_and_replays_after_expiry(tmp_path):
    pipeline, execution = setup(tmp_path)
    bodies = []
    def provider(method, url, headers, body, timeout):
        bodies.append(json.loads(body))
        return Response(200, {}, b'{"usage":{"prompt_tokens":10,"completion_tokens":10}}')
    send(execution, output=1000000, transport=provider)
    assert bodies[0]['max_tokens'] == 100
    summary = cost.summary(pipeline.store, 'cost-window')
    assert summary['committed'] == 60 and summary['retained'] == 0
    assert summary['basis'] == 'reference_tariff'
    with closing(pipeline.store._connect()) as db, db:
        db.execute("UPDATE studio_acceptance_batches SET deadline='2000-01-01T00:00:00+00:00'")
    send(execution, output=1000000, transport=lambda *a: pytest.fail('must replay exact body'))
    assert len(journal(pipeline.store)) == 1
    with pytest.raises(HttpError, match='acceptance_expired'):
        send(execution, 'different')
    assert len(journal(pipeline.store)) == 1


@pytest.mark.parametrize('payload,status', [(b'{}', 200), (b'{"usage":{"prompt_tokens":10}}', 200),
    (b'{"usage":{"prompt_tokens":true,"completion_tokens":10}}', 200),
    (b'{"usage":{"prompt_tokens":10,"completion_tokens":10}}', 500),
    (b'{"usage":{"prompt_tokens":10,"completion_tokens":10,"cache_read_input_tokens":5}}', 200)])
def test_missing_uncertain_or_error_usage_retains_entire_reservation(tmp_path, payload, status):
    pipeline, execution = setup(tmp_path, contract(2400))
    send(execution, transport=lambda *a: Response(status, {}, payload))
    assert cost.summary(pipeline.store, 'cost-window')['retained'] == 2400
    before = journal(pipeline.store)
    send(execution, 'next')
    assert len(journal(pipeline.store)) == 2
    assert journal(pipeline.store)[:1] == before
    assert cost.summary(pipeline.store, 'cost-window')['retained'] == 2400


def test_unknown_is_preserved_and_cannot_be_replayed_or_refunded(tmp_path):
    pipeline, execution = setup(tmp_path)
    def lost(*a):
        raise TimeoutError('synthetic')
    with pytest.raises(TimeoutError):
        send(execution, transport=lost)
    before = journal(pipeline.store)
    assert before[0]['status'] == 'unknown'
    assert cost.summary(pipeline.store, 'cost-window')['retained'] == 2400
    with pytest.raises(HttpError, match='request_outcome_unknown'):
        send(execution, transport=lambda *a: pytest.fail('unknown cannot send'))
    assert journal(pipeline.store) == before


def test_concurrent_children_keep_all_money_reservations_without_ceiling(tmp_path):
    pipeline, root = setup(tmp_path, contract(2400))
    pipeline.store.enqueue(root.job['project_id'], 'scope', parent_job_id=root.job['id'])
    child = pipeline.store.claim()
    barrier = threading.Barrier(2)
    calls = []
    def work(job):
        execution = JobExecution(StudioStore(pipeline.store.db_path), job)
        barrier.wait(timeout=15)
        def provider(*a):
            calls.append(job['id'])
            return Response(200, {}, b'{}')
        try:
            send(execution, job['id'], transport=provider)
            return 'sent'
        except HttpError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(work, [root.job, child]))
    assert sorted(results) == ['sent', 'sent']
    assert len(calls) == len(journal(pipeline.store)) == 2


def test_phase_reserves_do_not_block_collection_or_review(tmp_path):
    policy = contract(5000)
    policy['phase_reserves']['review'] = 3000
    pipeline, execution = setup(tmp_path, policy)
    send(execution, 'collection')
    with execution.research_budget_phase('review'):
        send(execution, 'review')
    assert cost.summary(pipeline.store, 'cost-window')['committed'] == 120


def test_report_and_new_cycle_keep_same_cost_account(tmp_path):
    pipeline, root = setup(tmp_path, contract(2400))
    send(root, transport=lambda *a: Response(200, {}, b'{}'))
    pipeline.store.finish(root.job['id'], lease_token=root.job['lease_token'])
    pipeline.store.enqueue(root.job['project_id'], 'brief', parent_job_id=root.job['id'])
    report = JobExecution(pipeline.store, pipeline.store.claim())
    send(report)
    pipeline.store.finish(report.job['id'], lease_token=report.job['lease_token'])
    next_job = pipeline.store.enqueue(root.job['project_id'], 'external', automatic=True)
    batch.bind_root(pipeline.store, 'cost-window', next_job['id'], 'next')
    other = JobExecution(pipeline.store, pipeline.store.claim())
    send(other)
    assert len(journal(pipeline.store)) == 3
    assert cost.summary(pipeline.store, 'cost-window')['committed'] == 2520


@pytest.mark.parametrize('changes,error', [({'url': 'https://other.invalid/v1/chat/completions'}, 'research_price_missing'),
    ({'model': 'other'}, 'research_price_missing'), ({'output': -1}, 'research_request_unbounded')])
def test_changed_route_or_invalid_output_never_reaches_provider(tmp_path, changes, error):
    pipeline, execution = setup(tmp_path)
    with pytest.raises(HttpError, match=error):
        send(execution, transport=lambda *a: pytest.fail('unpriced must not send'), **changes)
    assert journal(pipeline.store) == []


def test_reported_usage_exceeding_verified_cap_stops_further_dispatch(tmp_path):
    pipeline, execution = setup(tmp_path)
    send(execution, transport=lambda *a: Response(200, {}, b'{"usage":{"prompt_tokens":1001,"completion_tokens":10}}'))
    assert cost.summary(pipeline.store, 'cost-window')['price_mismatches'] == 1
    with pytest.raises(HttpError, match='research_price_mismatch'):
        send(execution, 'next')


def test_legacy_local_and_global_caps_do_not_block_new_calls(tmp_path):
    pipeline, execution = setup(tmp_path)
    collection_host.limit_budgets(execution, 'refresh')
    frozen = event_insight_work.begin(execution)
    assert frozen['origin_job_id'] == execution.job['id']
    for i in range(17):
        send(execution, str(i))
    assert len(journal(pipeline.store)) == 17
    execution.budgets['model'] = 17
    send(execution, 'beyond historical limit')
    assert len(journal(pipeline.store)) == 18


def test_only_explicit_free_public_read_capability_is_zero(tmp_path):
    pipeline, execution = setup(tmp_path)
    execution.call('page', 'official', lambda: Response(200, {}, b'public'), read_only=True, public_read=True)
    with pytest.raises(HttpError, match='research_price_missing'):
        execution.call('page', 'unknown page', lambda: pytest.fail('unpriced'), read_only=True)
    assert cost.summary(pipeline.store, 'cost-window')['committed'] == 0
    assert len(journal(pipeline.store)) == 1


def test_unpriced_historical_receipt_cannot_be_imported_as_zero(tmp_path):
    pipeline, execution = setup(tmp_path)
    # Synthetic legacy receipt from a different old job, untouched by admission.
    with closing(pipeline.store._connect()) as db, db:
        db.execute("INSERT INTO studio_jobs(id,project_id,kind,status,created_at) VALUES ('old',?,'external','done','2000')",
                   (execution.job['project_id'],))
        db.execute("INSERT INTO studio_job_calls(job_id,call_key,kind,provider,status,created_at) VALUES ('old','legacy','model','','completed','2000')")
        db.execute('INSERT INTO studio_job_budget_links VALUES (?,?,?)', (execution.job['id'], 'old', 'legacy'))
    with pytest.raises(HttpError, match='research_history_unpriced'):
        send(execution)
    assert len(journal(pipeline.store)) == 1


@pytest.mark.parametrize('field,value', [('ceiling', 0), ('ceiling', True), ('basis', 'invoice_guessed'),
    ('valid_until', '2000-01-01T00:00:00+00:00')])
def test_invalid_or_expired_price_contract_refused(field, value):
    spec = contract()
    spec[field] = value
    with pytest.raises(ValueError):
        cost.validate(spec)


@pytest.mark.parametrize('failure', ['http', 'public_read'])
def test_once_window_failure_stops_other_paid_routes_without_retry(tmp_path, failure):
    spec = contract()
    spec['stop_on_failure'] = True
    pipeline, execution = setup(tmp_path, spec)
    if failure == 'http':
        send(execution, transport=lambda *a: Response(503, {}, b'{}'))
    else:
        def unavailable():
            raise TimeoutError('synthetic unavailable source')
        with pytest.raises(HttpError, match='public_read_failed'):
            execution.call('open_data', 'fixed-public-read', unavailable, read_only=True, public_read=True)
    before = journal(pipeline.store)
    if failure == 'public_read':
        send(execution, 'independent-request')
        assert len(journal(pipeline.store)) == len(before) + 1
        assert journal(pipeline.store)[:len(before)] == before
        return
    with pytest.raises(HttpError, match='research_window_failed'):
        send(execution, 'different-request', transport=lambda *a: pytest.fail('closed window must not send'))
    assert journal(pipeline.store) == before


def test_received_page_404_is_replayed_without_resend_and_other_work_continues(tmp_path):
    spec = contract()
    spec['stop_on_failure'] = True
    pipeline, execution = setup(tmp_path, spec)
    def response():
        return Response(404, {}, b'unavailable')
    execution.call('page', 'missing', response, read_only=True, public_read=True)
    before = journal(pipeline.store)
    execution.call('page', 'missing', lambda: pytest.fail('must not resend'), read_only=True, public_read=True)
    send(execution, 'independent')
    assert journal(pipeline.store)[:1] == before
    assert len(journal(pipeline.store)) == 2


def test_index_annotations_are_variants_not_new_identities():
    names = ['示例公司', 'Example Biotherapeutics, Inc.（示例公司，境外注册主体）',
             'Acme (UK) Limited', 'Example（国际）有限公司', 'Example LLC（示例）']
    result = collection_host.index_names(names)
    assert result[0] == dict(query='Example Biotherapeutics, Inc.', confirmed_name=names[1], transform='terminal_annotation')
    assert next(r for r in result if r['confirmed_name'] == names[2])['query'] == names[2]
    assert next(r for r in result if r['confirmed_name'] == names[3])['query'] == names[3]
    assert set(r['confirmed_name'] for r in result) == set(names)


def read_failure_fixture(tmp_path):
    spec = contract()
    spec['stop_on_failure'] = True
    pipeline, execution = setup(tmp_path, spec)
    execution.call('page', 'missing', lambda: Response(404, {}, b'unavailable'), read_only=True, public_read=True)
    execution.checkpoint('dsh_hold', {'code': 'research_window_failed'})
    execution.checkpoint('bootstrap_collection_v1', dict(actions={
        'blocked': dict(status='complete', outcome='research_window_failed'),
        'saved': dict(status='complete', outcome='observed')}, pending=[], phase='prepare'))
    pipeline.store.finish(execution.job['id'], error='research_window_failed', lease_token=execution.job['lease_token'])
    batch.close(pipeline.store, 'cost-window')
    return pipeline, execution


def test_read_continuation_preserves_account_deadline_receipts_and_is_one_time(tmp_path):
    pipeline, old = read_failure_fixture(tmp_path)
    before = journal(pipeline.store)
    with closing(pipeline.store._connect()) as db:
        contract_before = tuple(db.execute('SELECT specification,deadline FROM studio_acceptance_batches').fetchone())
    batch.continue_read_failure(pipeline.store, 'cost-window', old.job['id'], authorization='synthetic explicit consent')
    execution = JobExecution(pipeline.store, pipeline.store.claim())
    batch.prepare_continuation(execution)
    assert execution.job['id'] == old.job['id']
    assert execution.read_checkpoint('dsh_hold') == {}
    assert set(execution.read_checkpoint('bootstrap_collection_v1')['actions']) == {'saved'}
    execution.call('page', 'missing', lambda: pytest.fail('never resend'), read_only=True, public_read=True)
    send(execution, 'first-model')
    assert journal(pipeline.store)[:1] == before
    with closing(pipeline.store._connect()) as db:
        assert tuple(db.execute('SELECT specification,deadline FROM studio_acceptance_batches').fetchone()) == contract_before
        saved = json.loads(db.execute('SELECT snapshot FROM studio_acceptance_continuations').fetchone()[0])
        assert saved['usage']['page'] == 1 and saved['checkpoints']
    pipeline.store.finish(execution.job['id'], error='research_window_failed', lease_token=execution.job['lease_token'])
    batch.close(pipeline.store, 'cost-window')
    with pytest.raises(HttpError, match='consumed'):
        batch.continue_read_failure(pipeline.store, 'cost-window', old.job['id'], authorization='again')


@pytest.mark.parametrize('change,error', [('unknown', 'ambiguous'), ('expired', 'expired'), ('paused', 'paused')])
def test_read_continuation_does_not_clear_unknown_or_extend_deadline_or_unpause(tmp_path, change, error):
    pipeline, execution = read_failure_fixture(tmp_path)
    with closing(pipeline.store._connect()) as db, db:
        if change == 'unknown':
            db.execute("UPDATE studio_job_calls SET status='unknown'")
        elif change == 'expired':
            db.execute("UPDATE studio_acceptance_batches SET deadline='2000-01-01T00:00:00+00:00'")
        else:
            db.execute("UPDATE studio_projects SET status='paused'")
    before = journal(pipeline.store)
    with pytest.raises(HttpError, match=error):
        batch.continue_read_failure(pipeline.store, 'cost-window', execution.job['id'], authorization='synthetic')
    assert journal(pipeline.store) == before


def test_unknown_still_fences_other_calls_without_resetting_cost(tmp_path):
    spec = contract()
    spec['stop_on_failure'] = True
    pipeline, execution = setup(tmp_path, spec)
    send(execution)
    with closing(pipeline.store._connect()) as db, db:
        db.execute("UPDATE studio_job_calls SET status='unknown'")
    before = cost.summary(pipeline.store, 'cost-window')
    with pytest.raises(HttpError):
        send(execution, 'independent', transport=lambda *a: pytest.fail('unknown must block'))
    assert cost.summary(pipeline.store, 'cost-window') == before
