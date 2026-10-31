"""Explicit settled-content correction does not release ambiguous requests."""
import json
from contextlib import closing

import pytest
from test_event_insight import install, next_insight, source
from test_studio_dsh_monitor import synthetic_review

from project_watch.studio import event_insight, event_insight_work, update_handoff, update_run
from project_watch.studio.dsh_bootstrap import ToolValidationError
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import HttpError
from project_watch.studio.pipeline import StudioWorker


def rejected_role(tmp_path, monkeypatch, *, legacy=True):
    pipeline, root, seen = install(tmp_path, monkeypatch)
    source(pipeline, root)
    child = next_insight(pipeline, root)
    child.budgets['model'] = 100  # Direct role fixture bypasses run()'s cap setup.
    manifest = child.read_checkpoint(event_insight_work.INPUT)
    scoped = event_insight.RoleExecution(child, manifest['objects'][0]['fingerprint'])
    _, _, _, collectors = pipeline.tools(execution=scoped)
    role = event_insight.ObjectInsight(pipeline, scoped, collectors, manifest['objects'][0], root.job['id'])
    role.complete_review_feedback = not legacy
    ctx = role.pw_context({})
    calls = []

    def reject(payload):
        calls.append(payload)
        result = synthetic_review(payload)
        if payload['phase'] == 'fields':
            result['checks'][0].update(status='insufficient', reason='Synthetic: not established by evidence')
        return result

    role.judgment_reviewer = reject
    text = next(iter(ctx['numbered'].values()))['context']
    for headline in ('Synthetic draft A', 'Synthetic draft B'):
        with pytest.raises(ToolValidationError):
            role.tool('pw_judge', {'analysis_json': json.dumps(dict(headline=headline,
                operations={'summary': text, 'for': [{'text': text, 'refs': ['N1']}]}))})
    return pipeline, root, child, role, calls, seen


def test_new_policy_preserves_rejections_and_stops_without_third_review(tmp_path, monkeypatch):
    pipeline, _root, child, role, calls, _ = rejected_role(tmp_path, monkeypatch, legacy=False)
    saved = role.execution.read_checkpoint('object_insight_draft')
    count = len(calls)
    role.tool('pw_judge', {'analysis_json': json.dumps({'headline': 'Synthetic third draft',
        'operations': {'for': [{'text': 'Synthetic', 'refs': ['N1']}]}})})
    assert len(calls) == count
    assert role.state['finished']['reason'] == 'judgment_revision_exhausted'
    assert role.execution.read_checkpoint('object_insight_draft') == saved
    assert role.execution.read_checkpoint('object_insight_unreviewed_draft')
    assert not child.read_checkpoint('dsh_hold')
    with closing(pipeline.store._connect()) as db:
        assert not db.execute('SELECT 1 FROM studio_insight_versions').fetchone()


@pytest.mark.parametrize('failure', ['settled', 'unknown', 'invalid'])
def test_explicit_content_handoff_keeps_old_hold_and_requires_new_full_review(tmp_path, monkeypatch, failure):
    pipeline, root, child, role, _calls, seen = rejected_role(tmp_path, monkeypatch)
    role.hold('judgment_review_limit' if failure != 'invalid' else 'judgment_review_invalid')
    assert pipeline.store.finish(child.job['id'], error='judgment_review_limit', lease_token=child.job['lease_token'])
    pipeline.schedule_insight(root.job['project_id'])
    if failure == 'unknown':
        with closing(pipeline.store._connect()) as db, db:
            db.execute("UPDATE studio_job_calls SET status='unknown' WHERE job_id=? AND kind='model'", (root.job['id'],))
    before = child.read_checkpoint('dsh_hold')
    with pytest.raises(HttpError):
        update_handoff.admit(pipeline, root.job['id'], authorization='synthetic', resume='analysis')
    if failure != 'settled':
        with pytest.raises(HttpError):
            update_handoff.admit(pipeline, root.job['id'], authorization='synthetic repair', resume='analysis', repair_content=True)
        assert not root.read_checkpoint(update_handoff.SUCCESSOR)
        return
    job = update_handoff.admit(pipeline, root.job['id'], authorization='synthetic repair', resume='analysis', repair_content=True)
    worker = StudioWorker(pipeline)
    monkeypatch.setattr(worker, 'schedule', lambda: None)
    assert worker.step()
    current = JobExecution(pipeline.store, job)
    state = current.read_checkpoint(update_run.STATE)
    assert state['phases']['analysis']['status'] == 'complete', state
    assert state['phases']['reports']['status'] == 'complete'
    assert seen['reviews']
    assert any('received_rejection_repair' in json.dumps(b['messages']) for b in seen['insight'])
    assert child.read_checkpoint('dsh_hold') == before
    assert current.read_checkpoint('update_content_review_handoff_v1')


@pytest.mark.parametrize('failure', ['evidence_shape', 'field_coverage', 'unknown', 'provider_error'])
def test_explicit_settled_evidence_shape_repair_requires_full_new_review(tmp_path, monkeypatch, failure):
    pipeline, root, child, role, _calls, seen = rejected_role(tmp_path, monkeypatch)
    bindings = role.execution.read_checkpoint('dsh_semantic_drafts')
    binding = bindings[-1]
    invalid = 'review_field_coverage_invalid' if failure == 'field_coverage' else 'review_evidence_invalid'
    role.execution.checkpoint('dsh_semantic_' + binding[:40],
                              {'passed': False, 'invalid': invalid, 'binding': binding})
    role.hold('judgment_review_invalid')
    assert pipeline.store.finish(child.job['id'], error='judgment_review_invalid', lease_token=child.job['lease_token'])
    pipeline.schedule_insight(root.job['project_id'])
    with closing(pipeline.store._connect()) as db, db:
        if failure == 'unknown':
            db.execute("UPDATE studio_job_calls SET status='unknown' WHERE job_id=? AND kind='model'", (root.job['id'],))
        elif failure == 'provider_error':
            db.execute("UPDATE studio_job_calls SET response_status=500 WHERE job_id=? AND kind='model'", (root.job['id'],))
        old_calls = [tuple(r) for r in db.execute('SELECT * FROM studio_job_calls ORDER BY job_id,call_key')]
    old_review = role.execution.read_checkpoint('dsh_semantic_' + binding[:40])
    with pytest.raises(HttpError):
        update_handoff.admit(pipeline, root.job['id'], authorization='no repair', resume='analysis')
    if failure != 'evidence_shape':
        with pytest.raises(HttpError):
            update_handoff.admit(pipeline, root.job['id'], authorization='explicit repair', resume='analysis', repair_content=True)
        assert not root.read_checkpoint(update_handoff.SUCCESSOR)
        return
    job = update_handoff.admit(pipeline, root.job['id'], authorization='explicit repair', resume='analysis', repair_content=True)
    worker = StudioWorker(pipeline)
    monkeypatch.setattr(worker, 'schedule', lambda: None)
    assert worker.step()
    current = JobExecution(pipeline.store, job)
    assert current.read_checkpoint(update_run.STATE)['phases']['analysis']['status'] == 'complete'
    assert seen['reviews']
    assert current.read_checkpoint(update_handoff.STAGE)['content_repairs'][0]['full_new_review_required']
    assert child.read_checkpoint('dsh_hold') == {'code': 'judgment_review_invalid'}
    assert role.execution.read_checkpoint('dsh_semantic_' + binding[:40]) == old_review
    with closing(pipeline.store._connect()) as db:
        old_ids = {row[0] for row in old_calls}
        assert [tuple(r) for r in db.execute('SELECT * FROM studio_job_calls ORDER BY job_id,call_key')
                if r['job_id'] in old_ids] == old_calls

@pytest.mark.parametrize('damage', [None, 'receipt', 'publication', 'unknown'])
def test_inline_failed_analysis_resumes_only_durable_publication(tmp_path, monkeypatch, damage):
    from test_monitor_completion_journey import call_rows

    from project_watch.studio import dsh_runtime

    p, root, seen = install(tmp_path, monkeypatch, monitor=False, user_entry=True)
    monkeypatch.setenv('PROJECT_WATCH_DSH_MONITOR', '1')
    worker = StudioWorker(p)
    monkeypatch.setattr(worker, 'schedule', lambda: None)
    original = dsh_runtime.Gateway.review_judgment

    def malformed(gateway, payload):
        response = original(gateway, payload)
        if payload.get('target_object') and payload.get('phase') == 'fields':
            response['checks'][0]['evidence'] = [{'ref': 'N1', 'quote': 'Synthetic invented quotation'}]
        return response

    monkeypatch.setattr(dsh_runtime.Gateway, 'review_judgment', malformed)
    assert worker.step()
    before_state = root.read_checkpoint(update_run.STATE)
    assert before_state['phases']['analysis']['status'] == 'blocked'
    assert not before_state.get('finished_at')
    assert before_state['phases']['events']['output']['publication_complete']
    assert root.read_checkpoint('dsh_hold') == {'code': 'judgment_review_invalid'}
    monkeypatch.setattr(dsh_runtime.Gateway, 'review_judgment', original)
    counts = {k: len(seen[k]) for k in ('pages', 'materials', 'clinical')}
    old_events = {e['id'] for e in p.store.list_events(project_id=root.job['project_id'])}
    with closing(p.store._connect()) as db, db:
        if damage in ('receipt', 'publication'):
            from project_watch.studio.dsh_bootstrap import digest
            phase = before_state['phases']['events']
            if damage == 'receipt':
                phase['output_fingerprint'] = 'damaged'
            else:
                phase['output']['publication_complete'] = False
                phase['output_fingerprint'] = digest(phase['output'])
            event_insight_work._put(db, root.job['id'], update_run.STATE, before_state)
        if damage == 'unknown':
            db.execute("UPDATE studio_job_calls SET status='unknown' WHERE job_id=? AND kind='model'", (root.job['id'],))
    calls = call_rows(p.store, root.job['id'])
    if damage:
        with pytest.raises((ValueError, HttpError)):
            update_handoff.admit(p, root.job['id'], authorization='synthetic repair', resume='analysis', repair_content=True)
        assert root.read_checkpoint(update_handoff.SUCCESSOR) is None
        return
    job = update_handoff.admit(p, root.job['id'], authorization='synthetic repair', resume='analysis', repair_content=True)
    assert worker.step()
    current = JobExecution(p.store, job)
    state = current.read_checkpoint(update_run.STATE)
    assert state['phases']['analysis']['status'] == 'complete', state
    assert state['phases']['reports']['status'] == 'complete'
    assert current.read_checkpoint(update_handoff.STAGE)['content_repairs']
    assert old_events <= {e['id'] for e in p.store.list_events(project_id=job['project_id'])}
    assert counts == {k: len(seen[k]) for k in counts}
    assert root.read_checkpoint(update_run.STATE) == before_state
    assert root.read_checkpoint('dsh_hold') == {'code': 'judgment_review_invalid'}
    assert call_rows(p.store, root.job['id']) == calls

@pytest.mark.parametrize('damage', [None, 'unknown', 'field'])
def test_explicit_legacy_path_protocol_failure_requires_received_complete_response(tmp_path, monkeypatch, damage):
    from project_watch.studio import dsh_judgment_review as review
    from project_watch.studio.http import Http, Response

    p, root, _seen = install(tmp_path, monkeypatch, monitor=False, user_entry=True)
    monkeypatch.setenv('PROJECT_WATCH_DSH_MONITOR', '1')
    worker = StudioWorker(p)
    monkeypatch.setattr(worker, 'schedule', lambda: None)
    transport = p.http_factory(None).transport
    decode = review.ReviewFieldAliases.decode

    def send(method, url, headers, raw, timeout):
        reply = transport(method, url, headers, raw, timeout)
        body = json.loads(raw) if raw and 'model.example.invalid' in url else {}
        if body and not body.get('tools'):
            payload = json.loads(body['messages'][-1]['content'])
            if payload.get('field_paths') and payload.get('phase') == 'fields':
                value = reply.json()
                msg = value['choices'][0]['message']
                result = json.loads(msg['content'])
                for row in result['checks']:
                    row['field'] = payload['field_paths'][row['field']]
                if damage == 'field':
                    result['checks'][0]['field'] = 'invented.field'
                msg['content'] = json.dumps(result)
                return Response(200, {}, json.dumps(value).encode())
        return reply

    def old_decoder(aliases, value):
        if aliases.payload.get('field_paths'):
            raise ValueError('review_field_coverage_invalid')
        return decode(aliases, value)

    p.http_factory = lambda _: Http(send)
    monkeypatch.setattr(review.ReviewFieldAliases, 'decode', old_decoder)
    assert worker.step()
    assert root.read_checkpoint('dsh_hold') == {'code': 'judgment_review_invalid'}
    before = root.read_checkpoint(update_run.STATE)
    monkeypatch.setattr(review.ReviewFieldAliases, 'decode', decode)
    with closing(p.store._connect()) as db, db:
        if damage == 'unknown':
            db.execute("UPDATE studio_job_calls SET status='unknown' WHERE job_id=? AND kind='model'", (root.job['id'],))
        calls = [tuple(r) for r in db.execute('SELECT * FROM studio_job_calls WHERE job_id=? ORDER BY call_key', (root.job['id'],))]
    if damage == 'unknown':
        with pytest.raises(HttpError):
            update_handoff.admit(p, root.job['id'], authorization='explicit format repair', resume='analysis', repair_content=True)
        assert not root.read_checkpoint(update_handoff.SUCCESSOR)
        return
    job = update_handoff.admit(p, root.job['id'], authorization='explicit format repair', resume='analysis', repair_content=True)
    assert worker.step()
    current = JobExecution(p.store, job)
    if damage == 'field':
        # Authorization to try fresh work never admits a repeated bad reply.
        assert current.read_checkpoint(update_run.STATE)['phases']['analysis']['status'] == 'blocked'
        assert current.read_checkpoint('dsh_hold') == {'code': 'judgment_review_invalid'}
        with closing(p.store._connect()) as db:
            assert not db.execute('SELECT 1 FROM studio_insight_versions').fetchone()
            assert [tuple(r) for r in db.execute('SELECT * FROM studio_job_calls WHERE job_id=? ORDER BY call_key', (root.job['id'],))] == calls
        assert root.read_checkpoint(update_run.STATE) == before
        return
    assert current.read_checkpoint(update_run.STATE)['phases']['analysis']['status'] == 'complete', current.read_checkpoint(update_run.STATE)
    assert current.read_checkpoint(update_run.STATE)['phases']['reports']['status'] == 'complete'
    assert current.read_checkpoint(update_handoff.STAGE)['content_repairs'][0]['full_new_review_required']
    assert root.read_checkpoint('dsh_hold') == {'code': 'judgment_review_invalid'}
    assert root.read_checkpoint(update_run.STATE) == before
    with closing(p.store._connect()) as db:
        assert [tuple(r) for r in db.execute('SELECT * FROM studio_job_calls WHERE job_id=? ORDER BY call_key', (root.job['id'],))] == calls
