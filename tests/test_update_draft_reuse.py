"""A compatible settled draft continues review, without paying to regenerate it."""
import json
from contextlib import closing

import pytest
from test_event_insight import install, next_insight, source
from test_monitor_completion_journey import call_rows

from project_watch.studio import event_insight, event_insight_work, update_handoff, update_run
from project_watch.studio.execution import JobExecution
from project_watch.studio.pipeline import StudioWorker


@pytest.mark.parametrize('resume', ['sources', 'analysis'])
def test_handoff_reuses_exact_draft_and_preserves_old_role_receipts(tmp_path, monkeypatch, resume):
    pipeline, root, seen = install(tmp_path, monkeypatch)
    source(pipeline, root)
    child = next_insight(pipeline, root)
    original = event_insight.ObjectInsight.review_candidate

    def save_pending(role, analysis, ctx):
        role.execution.checkpoint_chunked('object_insight_draft',
                                         {'analysis': analysis, 'fingerprint': ctx['fingerprint']})
        role.defer('semantic_review_budget')
        return None

    monkeypatch.setattr(event_insight.ObjectInsight, 'review_candidate', save_pending)
    pipeline.run(child.job)
    assert child.read_checkpoint(event_insight_work.RESULT)['objects'][0]['reason'] == 'semantic_review_budget'
    assert pipeline.store.finish(child.job['id'], lease_token=child.job['lease_token'])
    pipeline.schedule_insight(root.job['project_id'])
    monkeypatch.setattr(event_insight.ObjectInsight, 'review_candidate', original)
    before_calls = call_rows(pipeline.store, child.job['id'])
    with closing(pipeline.store._connect()) as db:
        before_state = [tuple(r) for r in db.execute('SELECT * FROM studio_job_checkpoints WHERE job_id=? ORDER BY stage', (child.job['id'],))]
    generated = len(seen['insight'])
    seen['baseline_events'] = pipeline.store.list_events(project_id=root.job['project_id'])
    seen['baseline_watches'] = pipeline.monitoring_plan.items(root.job['project_id'])
    seen['baseline_material_count'] = len(seen['materials'])
    job = update_handoff.admit(pipeline, root.job['id'], authorization='synthetic draft handoff', resume=resume)
    worker = StudioWorker(pipeline)
    monkeypatch.setattr(worker, 'schedule', lambda: None)
    assert worker.step()
    current = JobExecution(pipeline.store, {'id': job['id']})
    state = current.read_checkpoint(update_run.STATE)
    assert state['phases']['analysis']['status'] == 'complete', state
    assert len(seen['insight']) == generated and seen['reviews']
    receipt = current.read_checkpoint('update_analysis_reuse_v1')
    assert len(receipt['roles']) == 1 and receipt['roles'][0]['review_only']
    assert receipt['roles'][0]['origin_job_id'] == child.job['id']
    assert call_rows(pipeline.store, child.job['id']) == before_calls
    with closing(pipeline.store._connect()) as db:
        assert [tuple(r) for r in db.execute('SELECT * FROM studio_job_checkpoints WHERE job_id=? ORDER BY stage', (child.job['id'],))] == before_state


def test_changed_fingerprint_never_imports_old_role(tmp_path, monkeypatch):
    from test_update_handoff import finish_old

    pipeline, root, _ = install(tmp_path, monkeypatch)
    finish_old(pipeline, root)
    job = update_handoff.admit(pipeline, root.job['id'], authorization='synthetic handoff')
    job = pipeline.store.claim()
    current = JobExecution(pipeline.store, job)
    binding = current.read_checkpoint(update_handoff.STAGE)['settings_binding']
    update_handoff.reuse_analysis(current, {'settings_binding': binding,
                                          'objects': [{'fingerprint': 'f' * 64}]})
    assert current.read_checkpoint('update_analysis_reuse_v1') == {'roles': []}
    with closing(pipeline.store._connect()) as db:
        assert not db.execute("SELECT 1 FROM studio_job_checkpoints WHERE job_id=? AND stage LIKE 'ir:%'", (job['id'],)).fetchone()


@pytest.mark.parametrize('failure', ['rejected', 'unknown', 'invalid'])
def test_resumed_review_rejection_keeps_events_and_finishes_reports_without_regeneration(
        tmp_path, monkeypatch, failure):
    from project_watch.studio import dsh_judgment_review, insight
    from project_watch.studio.http import HttpError

    pipeline, root, seen = install(tmp_path, monkeypatch, monitor=False, user_entry=True)
    monkeypatch.setenv('PROJECT_WATCH_DSH_MONITOR', '1')
    original = event_insight.ObjectInsight.review_candidate

    def pending(role, analysis, ctx):
        role.execution.checkpoint_chunked('object_insight_draft',
            {'analysis': analysis, 'fingerprint': ctx['fingerprint']})
        role.defer('semantic_review_budget')
        return None

    monkeypatch.setattr(event_insight.ObjectInsight, 'review_candidate', pending)
    worker = StudioWorker(pipeline)
    monkeypatch.setattr(worker, 'schedule', lambda: None)
    assert worker.step()
    before = call_rows(pipeline.store, root.job['id'])
    events = pipeline.store.list_events(project_id=root.job['project_id'])
    generated = len(seen['insight'])
    monkeypatch.setattr(event_insight.ObjectInsight, 'review_candidate', original)

    def reject(response, payload):
        if failure != 'rejected':
            raise HttpError('request_outcome_unknown' if failure == 'unknown' else 'judgment_review_invalid')
        return [dict(field=next(iter(payload['fields'])), code='evidence_insufficient',
                     reason='The cited source does not support this comparison.', evidence=[])]

    monkeypatch.setattr(dsh_judgment_review, 'validate_review', reject)
    job = update_handoff.admit(pipeline, root.job['id'], authorization='synthetic review rejection', resume='analysis')
    assert worker.step()
    current = JobExecution(pipeline.store, job)
    state = current.read_checkpoint(update_run.STATE)
    assert not insight.load(pipeline.store, root.job['project_id'])
    assert pipeline.store.list_events(project_id=root.job['project_id']) == events
    assert call_rows(pipeline.store, root.job['id']) == before
    assert len(seen['insight']) == generated
    with closing(pipeline.store._connect()) as db:
        status = db.execute('SELECT status FROM studio_jobs WHERE id=?', (job['id'],)).fetchone()[0]
    if failure != 'rejected':
        assert status == 'failed'
        assert state['phases']['analysis']['status'] == 'blocked'
        assert 'reports' not in state['phases']
        return
    assert status == 'done' and state['status'] == 'partial'
    assert state['phases']['analysis']['status'] == 'partial'
    assert state['phases']['reports']['status'] == 'complete'
    work = current.read_checkpoint(event_insight_work.INPUT)['objects'][0]
    role = event_insight.RoleExecution(current, work['fingerprint'])
    receipt = role.read_checkpoint('object_insight_rejection')
    assert receipt['issues'][0]['code'] == 'evidence_insufficient'
    assert receipt['automatic_retry'] is False
    assert role.read_checkpoint('object_insight_draft')
    assert role.read_checkpoint('dsh_state')['finished']['reason'] == 'judgment_evidence_review'
    # New fixed-object policy collects all field feedback and consistency in
    # one pass, including after a previously deferred budget admission.
    assert [json.loads(r['messages'][-1]['content'])['phase']
            for r in seen['reviews']] == ['fields', 'fields', 'consistency']


def test_analysis_only_successor_never_recollects_or_reextracts(tmp_path, monkeypatch):
    from test_monitor_completion_journey import old_hold

    from project_watch.studio import public_research_admission

    pipeline, root, seen = install(tmp_path, monkeypatch, monitor=False, user_entry=True)
    monkeypatch.setenv('PROJECT_WATCH_DSH_MONITOR', '1')
    old_unknown = old_hold(pipeline.store, root.job['project_id'])
    unknown_before = call_rows(pipeline.store, old_unknown)
    original = event_insight.ObjectInsight.review_candidate

    def pending(role, analysis, ctx):
        role.execution.checkpoint_chunked('object_insight_draft', {'analysis': analysis, 'fingerprint': ctx['fingerprint']})
        role.defer('semantic_review_budget')
        return None

    monkeypatch.setattr(event_insight.ObjectInsight, 'review_candidate', pending)
    worker = StudioWorker(pipeline)
    monkeypatch.setattr(worker, 'schedule', lambda: None)
    assert worker.step()
    old = JobExecution(pipeline.store, root.job)
    state = old.read_checkpoint(update_run.STATE)
    assert state['phases']['analysis']['status'] == 'partial'
    before = call_rows(pipeline.store, root.job['id'])
    generated, materials = len(seen['insight']), len(seen['materials'])
    monkeypatch.setattr(event_insight.ObjectInsight, 'review_candidate', original)
    job = update_handoff.admit(pipeline, root.job['id'], authorization='synthetic review-only continuation', resume='analysis')
    assert worker.step()
    current = JobExecution(pipeline.store, job)
    after = current.read_checkpoint(update_run.STATE)
    with closing(pipeline.store._connect()) as db:
        status = dict(db.execute('SELECT status,error FROM studio_jobs WHERE id=?', (job['id'],)).fetchone())
    assert after, status
    assert after['phases']['analysis']['status'] == 'complete', after
    assert len(seen['insight']) == generated and len(seen['materials']) == materials
    assert call_rows(pipeline.store, root.job['id']) == before
    publication = current.read_checkpoint(event_insight_work.ROUND)
    assert publication['publication_complete']
    assert publication['event_versions'] == state['phases']['events']['output']['event_versions']
    grandchild = update_handoff.admit(pipeline, job['id'], authorization='synthetic second handoff', resume='analysis')
    assert worker.step()
    final = JobExecution(pipeline.store, grandchild)
    assert final.read_checkpoint(update_run.STATE)['phases']['analysis']['status'] == 'complete'
    with closing(pipeline.store._connect()) as db:
        assert public_research_admission.read(db, grandchild['id'])['root_job_id'] == root.job['id']
    assert call_rows(pipeline.store, old_unknown) == unknown_before
    assert not call_rows(pipeline.store, grandchild['id'])
