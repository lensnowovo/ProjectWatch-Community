"""Synthetic durable material-stage invariants; no external services."""
import json
from contextlib import closing

import pytest
import test_collection_view as fixtures
from test_collection_ingest import trial

from project_watch.studio import evidence_tasks
from project_watch.studio.http import HttpError, Response


@pytest.fixture
def owner(tmp_path):
    return fixtures.owner.__wrapped__(tmp_path)


def rows(owner):
    with closing(owner[0].store._connect()) as db:
        return [dict(row) for row in db.execute('SELECT * FROM studio_evidence_tasks ORDER BY rowid')]


def proposal(source, _execution):
    return {'observations': [{'text': 'Source assertion, not an approved fact', 'refs': [next(iter(source['refs']))]}]}


def two(owner):
    execution, unit, ingest, ref = fixtures.save(owner)
    second = trial(url='https://example.org/second')
    second['extra']['source_identity'] = 'clinical:NCT00000002'
    ingest(unit, [second])
    return execution, unit, ingest, ref


def test_ingest_atomically_admits_once_and_replay_does_not_repeat_processing(owner):
    execution, unit, ingest, _ = fixtures.save(owner)
    ingest(unit, [trial()])
    calls = []

    def extract(source, parent):
        assert parent.job is execution.job  # same lease, receipt namespace and budget
        calls.append(source['task_id'])
        return proposal(source, parent)

    assert evidence_tasks.run_pending(execution, extract) == 1
    ingest(unit, [trial()])
    assert evidence_tasks.run_pending(execution, extract) == 0
    assert len(rows(owner)) == len(calls) == 1
    output = json.loads(rows(owner)[0]['result'])
    assert output['review_status'] == 'pending' and output['input_status'] == 'current'
    ref = output['refs'][output['observations'][0]['refs'][0]]
    stored = owner[0].evidence.get(owner[2], ref['evidence_version_id'])
    assert ref['quote'] == stored['parts'][ref['part_index']]['text'][ref['start']:ref['end']]
    assert owner[0].store.list_events(project_id=owner[2]) == []  # extraction is not semantic admission


def test_bad_material_isolated_but_unknown_stops_new_dispatch(owner):
    execution, _, _, _ = two(owner)
    seen = []

    def extract(source, parent):
        seen.append(source['task_id'])
        return {'observations': [{'text': 'bad', 'refs': ['unbound']}]} if len(seen) == 1 else proposal(source, parent)

    assert evidence_tasks.run_pending(execution, extract) == 2
    assert [row['status'] for row in rows(owner)] == ['rejected', 'complete']


def test_unknown_keeps_pending_and_does_not_visit_next_material(owner):
    execution, _, _, _ = two(owner)
    seen = []

    def extract(source, parent):
        def transport():
            seen.append(source['task_id'])
            raise HttpError('connection_failed')
        return parent.call('model', 'event:' + source['task_id'], transport, provider='synthetic')

    # The journal preserves the first transport error while recording unknown;
    # replay returns the explicit outcome hold without dispatching transport.
    for code in ('connection_failed', 'request_outcome_unknown'):
        with pytest.raises(HttpError, match=code):
            evidence_tasks.run_pending(execution, extract)
    assert len(seen) == 1 and [row['status'] for row in rows(owner)] == ['pending', 'pending']
    assert execution.usage()['model']['unknown'] == 1


def test_receipt_replay_after_projection_crash_does_not_send_twice(owner, monkeypatch):
    execution, _, _, _ = fixtures.save(owner)
    sent = []

    def extract(source, parent):
        def transport():
            sent.append(source['task_id'])
            return Response(200, {}, json.dumps(proposal(source, parent)).encode())
        response = parent.call('model', 'event:' + source['task_id'], transport, provider='synthetic')
        return json.loads(response.body)

    save = evidence_tasks._save
    monkeypatch.setattr(evidence_tasks, '_save', lambda *_: (_ for _ in ()).throw(RuntimeError('synthetic crash')))
    with pytest.raises(RuntimeError, match='synthetic crash'):
        evidence_tasks.run_pending(execution, extract)
    monkeypatch.setattr(evidence_tasks, '_save', save)
    evidence_tasks.run_pending(execution, extract)
    assert len(sent) == 1 and rows(owner)[0]['status'] == 'complete'
    assert execution.usage()['model']['requests'] == 1


def test_expired_lease_cannot_commit_observation(owner):
    execution, _, _, _ = fixtures.save(owner)

    def extract(source, parent):
        with closing(parent.store._connect()) as db, db:
            db.execute("UPDATE studio_jobs SET lease_token='replacement' WHERE id=?", (parent.job['id'],))
        return proposal(source, parent)

    with pytest.raises(RuntimeError, match='lease expired'):
        evidence_tasks.run_pending(execution, extract)
    assert rows(owner)[0]['status'] == 'pending'


def test_withdrawal_skips_model_and_revision_invalidates_candidate(owner):
    execution, unit, ingest, ref = fixtures.save(owner)
    evidence_tasks.run_pending(execution, proposal)
    key = 'event-input:' + rows(owner)[0]['id']
    changed = trial()
    changed['snippet'] = 'Changed source assertion'
    ingest(unit, [changed])
    assert len(rows(owner)) == 2
    assert owner[0].evidence.dependencies.get(owner[2], key)['status'] == 'stale'
    owner[0].evidence.dependencies.withdraw(owner[2], rows(owner)[1]['evidence_version_id'], 'synthetic', actor='test')
    evidence_tasks.run_pending(execution, lambda *_: pytest.fail('withdrawn material must not invoke model'))
    assert rows(owner)[1]['status'] == 'rejected'
    result = json.loads(rows(owner)[0]['result'])
    assert result['refs'][result['observations'][0]['refs'][0]]['evidence_version_id'] == ref['evidence_version_id']


def test_ingest_failure_rolls_back_source_and_stage_together(owner, monkeypatch):
    _, unit, ingest, _ = fixtures.save(owner)
    before = rows(owner)
    monkeypatch.setattr(evidence_tasks, 'admit', lambda *_: (_ for _ in ()).throw(RuntimeError('synthetic insert error')))
    second = trial(url='https://example.org/uncommitted')
    second['extra']['source_identity'] = 'clinical:NCT00000002'
    with pytest.raises(RuntimeError, match='synthetic insert error'):
        ingest(unit, [second])
    assert rows(owner) == before
    with closing(owner[0].store._connect()) as db:
        assert db.execute('SELECT count(*) FROM studio_collection_sources').fetchone()[0] == 1


def test_materials_share_parent_call_budget(owner):
    execution, _, _, _ = two(owner)
    execution.budgets['model'] = 1
    sent = []

    def extract(source, parent):
        def transport():
            sent.append(source['task_id'])
            return Response(200, {}, json.dumps(proposal(source, parent)).encode())
        return json.loads(parent.call('model', 'event:' + source['task_id'], transport, provider='synthetic').body)

    with pytest.raises(HttpError, match='request_budget'):
        evidence_tasks.run_pending(execution, extract)
    assert len(sent) == 1
    assert [row['status'] for row in rows(owner)] == ['complete', 'pending']
