"""Recovery presentation must not mistake a healthy transport for uncertainty."""
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
import test_studio_dsh_monitor as fixtures

from project_watch.studio import recovery
from project_watch.studio.http import HttpError, Response


@pytest.fixture
def loop(tmp_path):
    return fixtures.loop.__wrapped__(tmp_path)


def started(loop, kind='model'):
    with closing(loop.store._connect()) as db, db:
        db.execute("INSERT INTO studio_job_calls(job_id,call_key,kind,provider,status,created_at,deadline_at) "
                   "VALUES (?,'synthetic-inflight',?,'synthetic','started',?,?)",
                   (loop.execution.job['id'], kind, datetime.now(UTC).isoformat(),
                    (datetime.now(UTC) + timedelta(seconds=360)).isoformat()))


@pytest.mark.parametrize('kind', ['model', 'search', 'site', 'page', 'open_data'])
def test_inflight_request_is_not_a_confirmation_but_stays_fenced(loop, kind):
    def send():
        assert recovery.project_preview(loop.pipeline, loop.project_id)['items'] == []
        preview = recovery.preview(loop.pipeline, loop.project_id, loop.execution.job['id'])
        assert not preview['can_recover'] and preview['requests'] == {}
        assert '等待响应' in preview['reason']
        # Display filtering must not weaken the journal's protection or allow a second send.
        assert loop.execution.usage()[kind]['unknown'] == 1
        with pytest.raises(HttpError, match='request_outcome_unknown'):
            loop.execution.call(kind, 'synthetic', lambda: pytest.fail('duplicate dispatched'))
        with pytest.raises(ValueError):
            recovery.authorize(loop.pipeline, loop.project_id, loop.execution.job['id'],
                expected_version=preview['version'], idempotency_key='synthetic', accept_duplicate_cost=True)
        return Response(200, {}, b'{}')
    loop.execution.call(kind, 'synthetic', send)
    assert recovery.project_preview(loop.pipeline, loop.project_id)['items'] == []


@pytest.mark.parametrize('boundary', ['lease_expired', 'lease_missing', 'lease_token_missing',
                                      'deadline_expired', 'queued', 'failed', 'done'])
def test_abandoned_or_overdue_started_receipt_is_not_hidden(loop, boundary):
    started(loop)
    with closing(loop.store._connect()) as db, db:
        if boundary == 'lease_expired':
            db.execute("UPDATE studio_jobs SET lease_until='2000-01-01' WHERE id=?", (loop.execution.job['id'],))
        elif boundary == 'lease_missing':
            db.execute('UPDATE studio_jobs SET lease_until=NULL WHERE id=?', (loop.execution.job['id'],))
        elif boundary == 'lease_token_missing':
            db.execute("UPDATE studio_jobs SET lease_token='' WHERE id=?", (loop.execution.job['id'],))
        elif boundary == 'deadline_expired':
            db.execute("UPDATE studio_job_calls SET deadline_at='2000-01-01' WHERE job_id=?", (loop.execution.job['id'],))
        else:
            db.execute('UPDATE studio_jobs SET status=? WHERE id=?', (boundary, loop.execution.job['id']))
    items = recovery.project_preview(loop.pipeline, loop.project_id)['items']
    assert len(items) == 1 and items[0]['requests'] == {'model': 1}
    assert not items[0]['can_recover']  # No input binding or terminal authorization.
    if boundary not in ('failed', 'done'):
        assert '收尾' in items[0]['reason']


@pytest.mark.parametrize('failure', ['unknown', 'received_500', 'unknown_query'])
def test_real_failure_is_visible_even_with_healthy_running_request(loop, failure):
    started(loop)
    if failure == 'unknown_query':
        with closing(loop.store._connect()) as db, db:
            db.execute("INSERT INTO studio_query_uses VALUES ('synthetic-use',?,'synthetic-query','synthetic',?,'unknown',?)",
                       (loop.project_id, loop.execution.job['id'], datetime.now(UTC).isoformat()))
    else:
        with closing(loop.store._connect()) as db, db:
            db.execute("INSERT INTO studio_job_calls(job_id,call_key,kind,provider,status,response_status,created_at) "
                       "VALUES (?,'synthetic-failure','search','synthetic',?,?,?)",
                       (loop.execution.job['id'], 'unknown' if failure == 'unknown' else 'completed',
                        None if failure == 'unknown' else 500, datetime.now(UTC).isoformat()))
            if failure == 'received_500':
                db.execute("UPDATE studio_job_calls SET kind='model' WHERE call_key='synthetic-failure'")
    items = recovery.project_preview(loop.pipeline, loop.project_id)['items']
    assert len(items) == 1
    assert items[0]['requests'] == {'model' if failure == 'received_500' else 'search': 1}
    assert not items[0]['can_recover']


def test_inflight_to_unknown_transition_remains_visible_without_retry(loop):
    def lost():
        assert recovery.project_preview(loop.pipeline, loop.project_id)['items'] == []
        raise HttpError('connection_failed', detail='synthetic timeout')
    with pytest.raises(HttpError):
        loop.execution.call('model', 'timeout', lost)
    assert recovery.project_preview(loop.pipeline, loop.project_id)['items'][0]['requests'] == {'model': 1}
    loop.store.finish(loop.execution.job['id'], error='request_outcome_unknown', lease_token=loop.execution.job['lease_token'])
    assert recovery.project_preview(loop.pipeline, loop.project_id)['items'][0]['job_status'] == 'failed'
    with closing(loop.store._connect()) as db:
        assert db.execute('SELECT count(*) FROM studio_call_recoveries').fetchone()[0] == 0
