"""Actual worker entry, frozen object work and synthetic transport only."""
import json
from contextlib import closing

import pytest
from test_bootstrap_collection import install as collection_install

from project_watch.studio import dsh_runtime, event_insight, event_insight_work, insight
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import Http, HttpError, Response
from project_watch.studio.monitoring_contracts import ObjectInput
from project_watch.studio.pipeline import StudioWorker


def install(tmp_path, monkeypatch, *, monitor=True, two=False, native=False):
    pipeline, execution, seen = collection_install(tmp_path, monkeypatch, mode='scope_missing', native=native)
    if monitor:
        pipeline.store.touch(execution.job['project_id'], 'last_external_at')
    monkeypatch.setenv('PROJECT_WATCH_DSH_MONITOR', '1' if monitor else '0')
    seen['insight'], seen['reviews'] = [], []
    seen['hook'] = None
    source_send = pipeline.http_factory(None).transport
    if two:
        pipeline.objects.save(execution.job['project_id'], ObjectInput(kind='company', name='Second Host',
            purpose='related', visibility='public'), actor='user')

    def send(method, url, headers, raw, timeout):
        body = json.loads(raw) if raw and 'model.example.invalid' in url else {}
        tools = {t['function']['name'] for t in body.get('tools', [])}
        if 'pw_judge' in tools:
            assert tools == set(event_insight.TOOLS)
            seen['insight'].append(body)
            contexts = [value for m in body['messages'] if m['role'] == 'tool'
                        for value in [json.loads(m['content'])] if isinstance(value, dict) and 'numbered' in value]
            if not contexts:
                name, args = 'pw_context', {}
            else:
                ctx = contexts[-1]
                text = next(iter(ctx['numbered'].values()))['context']
                analysis = {'headline': text,
                    'operations': {'verdict': 'improving', 'summary': text, 'for': [{'text': text, 'refs': ['N1']}]}}
                if seen.get('wide_draft'):
                    point = {'text': text, 'refs': ['N1']}
                    analysis.update(position={'verdict': 'unclear', 'summary': text, 'for': [point], 'against': [point]},
                        chain={'verdict': 'unclear', 'summary': text, 'for': [point], 'against': [point]},
                        technology={'summary': text, 'academic': [point], 'industry': [point], 'risks': [point]},
                        industry={'verdict': 'unclear', 'summary': text, 'drivers': [point], 'headwinds': [point]})
                name, args = 'pw_judge', {'analysis_json': json.dumps(analysis)}
            message = {'role': 'assistant', 'tool_calls': [{'id': 'insight-' + str(len(seen['insight'])),
                'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}
            return Response(200, {}, json.dumps({'choices': [{'message': message, 'finish_reason': 'tool_calls'}],
                'usage': {'prompt_tokens': 10, 'completion_tokens': 5}}).encode())
        if body and not tools:
            review = json.loads(body['messages'][-1]['content'])
            if 'target_object' in review:
                seen['reviews'].append(body)
                if seen['hook']:
                    seen['hook']()
                    seen['hook'] = None
        response = source_send(method, url, headers, raw, timeout)
        if two and tools == {'pw_submit_records'}:
            data = response.json()
            function = data['choices'][0]['message']['tool_calls'][0]['function']
            args = json.loads(function['arguments'])
            args['observations'] += [{**r, 'subject': 'Second Host'} for r in args['observations']]
            function['arguments'] = json.dumps(args)
            response = Response(200, {}, json.dumps(data).encode())
        return response

    pipeline.http_factory = lambda _: Http(send)
    return pipeline, execution, seen


def source(pipeline, execution):
    pipeline.run(execution.job)
    return execution.read_checkpoint(event_insight_work.ROUND)


def next_insight(pipeline, execution):
    pipeline.store.finish(execution.job['id'], lease_token=execution.job['lease_token'])
    job = pipeline.store.claim()
    assert job and job['kind'] == 'insight'
    return JobExecution(pipeline.store, job)


@pytest.mark.parametrize('monitor', [False, True])
def test_actual_pipeline_freezes_fresh_material_entry_before_recovery_checkpoint(tmp_path, monkeypatch, monitor):
    pipeline, execution, seen = install(tmp_path, monkeypatch, monitor=monitor)
    source(pipeline, execution)
    owner = execution.read_checkpoint('material_pipeline_v1')
    assert owner['owner'] == 'materials' and owner['entry'] == ('monitor' if monitor else 'bootstrap')
    assert execution.read_checkpoint('recovery_inputs') and seen['materials']
    assert pipeline.store.list_events(project_id=execution.job['project_id'])
    before = len(seen['models'])
    pipeline.run(execution.job)
    assert len(seen['models']) == before


@pytest.mark.parametrize('monitor', [False, True])
def test_old_recovery_entry_remains_legacy(tmp_path, monkeypatch, monitor):
    from project_watch.studio.recovery import input_binding
    pipeline, execution, _seen = install(tmp_path, monkeypatch, monitor=monitor)
    execution.checkpoint('recovery_inputs', {'binding': input_binding(pipeline, execution.job['project_id'])})
    # Stop after actual dispatch selects the legacy role, before synthetic I/O.
    monkeypatch.setattr(dsh_runtime, 'run_dsh', lambda research, *args, **kwargs: {'tools': research.tools})
    source(pipeline, execution)
    assert execution.read_checkpoint('material_pipeline_v1')['owner'] == 'legacy'


@pytest.mark.parametrize('native', [False, True])
def test_worker_delivers_events_then_fresh_fixed_insight_with_no_source_tools(tmp_path, monkeypatch, native):
    pipeline, execution, seen = install(tmp_path, monkeypatch, native=native)
    handoff = source(pipeline, execution)
    assert handoff['phase'] == 'scheduled' and len(handoff['objects']) == 1
    assert not seen['insight'] and not insight.load(pipeline.store, execution.job['project_id'])
    assert pipeline.store.claim() is None  # Source stage must be terminal first.
    pipeline.store.finish(execution.job['id'], lease_token=execution.job['lease_token'])
    source_counts = {k: len(seen[k]) for k in ('pages', 'materials', 'clinical')}
    worker = StudioWorker(pipeline)
    monkeypatch.setattr(worker, 'schedule', lambda: None)
    assert worker.step()
    with closing(pipeline.store._connect()) as db:
        job = dict(db.execute('SELECT * FROM studio_jobs WHERE id=?', (handoff['insight_job_id'],)).fetchone())
    assert job['status'] == 'done', job['error']
    result = JobExecution(pipeline.store, job).read_checkpoint(event_insight_work.RESULT)
    assert result['status'] == 'complete' and result['model_used'] <= handoff['cap']
    assert source_counts == {k: len(seen[k]) for k in source_counts}
    assert seen['reviews'] and seen['insight']
    assert all(len(r['messages']) == 2 and 'tools' not in r for r in seen['reviews'])
    record = insight.load(pipeline.store, execution.job['project_id'])
    assert record['status'] == 'current' and record['input']['event_versions']
    assert record['input']['object_versions'] and record['review']['passed']


def test_two_objects_share_one_job_budget_and_replay_never_recollects(tmp_path, monkeypatch):
    pipeline, execution, seen = install(tmp_path, monkeypatch, two=True)
    handoff = source(pipeline, execution)
    assert len(handoff['objects']) == 2
    child = next_insight(pipeline, execution)
    pipeline.run(child.job)
    result = child.read_checkpoint(event_insight_work.RESULT)
    assert len(result['objects']) == 2 and result['status'] == 'complete'
    assert child.budget_used('model') <= handoff['cap']
    with closing(pipeline.store._connect()) as db:
        assert db.execute('SELECT count(*) FROM studio_insight_versions').fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM studio_jobs WHERE kind='insight'").fetchone()[0] == 1
    counts = len(seen['models']), len(seen['insight'])
    pipeline.run(child.job)
    pipeline.schedule_insight(execution.job['project_id'])
    assert counts == (len(seen['models']), len(seen['insight']))


def test_version_change_during_fresh_review_rejects_current_pointer(tmp_path, monkeypatch):
    pipeline, execution, seen = install(tmp_path, monkeypatch)
    handoff = source(pipeline, execution)
    child = next_insight(pipeline, execution)
    obj = handoff['objects'][0]['object']
    def change():
        pipeline.objects.save(execution.job['project_id'], ObjectInput(kind='company', name=obj['name'],
            purpose='subject', visibility='public', website='https://changed.example.invalid'), actor='user',
            object_id=obj['id'], expected_version=obj['version'])
    seen['hook'] = change
    pipeline.run(child.job)
    assert not insight.load(pipeline.store, execution.job['project_id'])
    assert child.read_checkpoint(event_insight_work.RESULT)['objects'][0]['reason'] == 'input_version_changed'
    assert pipeline.store.list_events(project_id=execution.job['project_id'])


def revise_event(pipeline, pid, value='corrected'):
    from project_watch.studio.event_revisions import record
    with closing(pipeline.store._connect()) as db, db:
        eid = db.execute('SELECT id FROM studio_events WHERE project_id=? LIMIT 1', (pid,)).fetchone()[0]
        record(db, pid, eid)
        db.execute('UPDATE studio_events SET impact=? WHERE id=?', (value, eid))
        record(db, pid, eid)


def test_event_correction_coalesces_rebuild_and_preserves_full_immutable_history(tmp_path, monkeypatch):
    pipeline, execution, seen = install(tmp_path, monkeypatch)
    pid = execution.job['project_id']
    source(pipeline, execution)
    child = next_insight(pipeline, execution)
    pipeline.run(child.job)
    pipeline.store.finish(child.job['id'], lease_token=child.job['lease_token'])
    old = insight.load(pipeline.store, pid)
    revise_event(pipeline, pid)
    assert insight.load(pipeline.store, pid)['status'] == 'stale'
    worker = StudioWorker(pipeline)
    worker.dispatch_rebuilds(model_ready=True)
    worker.dispatch_rebuilds(model_ready=True)
    with closing(pipeline.store._connect()) as db:
        assert db.execute("SELECT count(*) FROM studio_jobs WHERE kind='insight'").fetchone()[0] == 2
    rebuild = JobExecution(pipeline.store, pipeline.store.claim())
    assert rebuild.read_checkpoint(event_insight_work.INPUT)['origin_job_id'] == child.job['id']
    pipeline.run(rebuild.job)
    fresh = insight.load(pipeline.store, pid)
    assert fresh['status'] == 'current' and fresh['id'] != old['id'] and fresh['parent_version_id'] == old['id']
    assert len(insight.history(pipeline.store, pid)) == 2
    assert not pipeline.evidence.dependencies.pending(pid)
    with closing(pipeline.store._connect()) as db:
        saved = json.loads(db.execute('SELECT record FROM studio_insight_versions WHERE id=?', (old['id'],)).fetchone()[0])
        assert saved['analysis'] == old['analysis'] and saved['numbered'] == old['numbered'] and saved['review'] == old['review']
        with pytest.raises(Exception, match='immutable'):
            db.execute('UPDATE studio_insight_versions SET record=? WHERE id=?', ('{}', old['id']))
    # The previous judgment is comparison text only, never a numbered source.
    tasks = [m['content'] for r in seen['insight'] for m in r['messages'] if m['role'] == 'user']
    assert any('comparison_only_not_evidence' in t for t in tasks)
    assert all('previous_judgment' not in str(r['context']) for r in fresh['numbered'].values())
    counts = len(seen['models']), len(seen['insight'])
    pipeline.run(rebuild.job)
    assert counts == (len(seen['models']), len(seen['insight']))


def test_failed_rebuild_keeps_stale_previous_and_no_second_queue(tmp_path, monkeypatch):
    pipeline, execution, _seen = install(tmp_path, monkeypatch)
    pid = execution.job['project_id']
    source(pipeline, execution)
    child = next_insight(pipeline, execution)
    pipeline.run(child.job)
    pipeline.store.finish(child.job['id'], lease_token=child.job['lease_token'])
    old = insight.load(pipeline.store, pid)
    revise_event(pipeline, pid)
    worker = StudioWorker(pipeline)
    worker.dispatch_rebuilds(model_ready=True)
    job = pipeline.store.claim()
    pipeline.store.finish(job['id'], error='synthetic analysis failure', lease_token=job['lease_token'])
    worker.dispatch_rebuilds(model_ready=True)
    worker.dispatch_rebuilds(model_ready=True)
    current = insight.load(pipeline.store, pid)
    assert current['status'] == 'stale' and current['id'] == old['id'] and current['analysis'] == old['analysis']
    assert pipeline.evidence.dependencies.pending(pid)[0]['status'] == 'blocked_failed'
    assert pipeline.store.claim() is None


def test_publication_crash_keeps_event_and_recovers_unique_outbox_without_paid_replay(tmp_path, monkeypatch):
    pipeline, execution, seen = install(tmp_path, monkeypatch)
    original = pipeline.store.put_compiled_profile
    def fail(*args, **kwargs):
        raise RuntimeError('after event publication')
    monkeypatch.setattr(pipeline.store, 'put_compiled_profile', fail)
    with pytest.raises(RuntimeError, match='after event publication'):
        source(pipeline, execution)
    assert pipeline.store.list_events(project_id=execution.job['project_id'])
    assert execution.read_checkpoint(event_insight_work.ROUND)['publication_sources']
    counts = len(seen['models']), len(seen['materials'])
    pipeline.store.finish(execution.job['id'], error='synthetic crash', lease_token=execution.job['lease_token'])
    monkeypatch.setattr(pipeline.store, 'put_compiled_profile', original)
    first = pipeline.schedule_insight(execution.job['project_id'])
    assert first and pipeline.schedule_insight(execution.job['project_id']) is None
    assert counts == (len(seen['models']), len(seen['materials']))
    child = JobExecution(pipeline.store, pipeline.store.claim())
    pipeline.run(child.job)
    assert child.read_checkpoint(event_insight_work.RESULT)['status'] == 'complete'


def test_small_shared_cap_retains_unreviewed_draft_without_budget_hold(tmp_path, monkeypatch):
    from project_watch.studio import progressive_bootstrap
    pipeline, execution, seen = install(tmp_path, monkeypatch)
    progressive_bootstrap.freeze(execution, 'monitor', materials=True)
    execution.budgets['model'] = 5
    seen['wide_draft'] = True
    event_insight_work.begin(execution)
    handoff = source(pipeline, execution)
    child = next_insight(pipeline, execution)
    pipeline.run(child.job)
    result = child.read_checkpoint(event_insight_work.RESULT)
    assert result['status'] == 'pending' and result['model_used'] <= 5
    assert result['objects'][0]['reason'] == 'semantic_review_budget'
    scoped = event_insight.RoleExecution(child, handoff['objects'][0]['fingerprint'])
    assert scoped.read_checkpoint('object_insight_draft')
    assert not child.read_checkpoint('dsh_hold') and not seen['reviews']
    assert pipeline.store.list_events(project_id=execution.job['project_id'])
    before = len(seen['insight'])
    pipeline.run(child.job)
    assert len(seen['insight']) == before


def test_insight_unknown_remains_project_hold_and_never_retries(tmp_path, monkeypatch):
    pipeline, execution, seen = install(tmp_path, monkeypatch)
    source(pipeline, execution)
    child = next_insight(pipeline, execution)
    original = pipeline.http_factory(None).transport
    calls = []
    def unknown(method, url, headers, raw, timeout):
        if 'model.example.invalid' in url:
            calls.append(url)
            raise HttpError('connection_failed')
        return original(method, url, headers, raw, timeout)
    pipeline.http_factory = lambda _: Http(unknown)
    for _ in range(2):
        with pytest.raises(HttpError, match=r'request_outcome_unknown|connection_failed'):
            pipeline.run(child.job)
    assert len(calls) == 1 and not seen['reviews']
    assert child.read_checkpoint('dsh_hold') and pipeline.store.list_events(project_id=execution.job['project_id'])


def test_two_object_rebuilds_share_latest_round_predecessor_and_cannot_respend_balance(tmp_path, monkeypatch):
    from project_watch.studio import progressive_bootstrap
    pipeline, execution, _seen = install(tmp_path, monkeypatch, two=True)
    pid = execution.job['project_id']
    progressive_bootstrap.freeze(execution, 'monitor', materials=True)
    execution.budgets['model'] = 12
    event_insight_work.begin(execution)
    source(pipeline, execution)
    child = next_insight(pipeline, execution)
    pipeline.run(child.job)
    assert child.read_checkpoint(event_insight_work.RESULT)['status'] == 'complete'
    pipeline.store.finish(child.job['id'], lease_token=child.job['lease_token'])
    revise_event(pipeline, pid)
    worker = StudioWorker(pipeline)
    predecessor = child.job['id']
    outcomes = []
    for _ in range(2):
        worker.dispatch_rebuilds(model_ready=True)
        current = JobExecution(pipeline.store, pipeline.store.claim())
        manifest = current.read_checkpoint(event_insight_work.INPUT)
        assert manifest['round_root_job_id'] == execution.job['id']
        assert manifest['predecessor_job_id'] == predecessor
        pipeline.run(current.job)
        outcomes.append(current.read_checkpoint(event_insight_work.RESULT))
        pipeline.store.finish(current.job['id'], lease_token=current.job['lease_token'])
        predecessor = current.job['id']
    with closing(pipeline.store._connect()) as db:
        paid = db.execute("SELECT count(*) FROM studio_job_calls WHERE kind='model'").fetchone()[0]
    assert paid == current.budget_used('model') <= 12
    assert outcomes[-1]['status'] == 'pending'


@pytest.mark.parametrize('change', ['event', 'model'])
def test_manual_refresh_api_uses_fixed_worker_admission_and_legacy_cannot_hide_stale(tmp_path, monkeypatch, change):
    import test_collection_host as fixtures
    from fastapi.testclient import TestClient
    original = fixtures.create_studio_app
    apps = []
    def capture(*args, **kwargs):
        app = original(*args, **kwargs)
        apps.append(app)
        return app
    monkeypatch.setattr(fixtures, 'create_studio_app', capture)
    pipeline, execution, _seen = install(tmp_path, monkeypatch)
    pid = execution.job['project_id']
    source(pipeline, execution)
    child = next_insight(pipeline, execution)
    pipeline.run(child.job)
    pipeline.store.finish(child.job['id'], lease_token=child.job['lease_token'])
    old = insight.load(pipeline.store, pid)
    if change == 'event':
        revise_event(pipeline, pid)
    else:
        settings = pipeline.settings_store.load()
        settings.model.model = 'synthetic-v2'
        pipeline.settings_store.save(settings)
    with pytest.raises(ValueError, match='fixed-input publication'):
        insight.save(pipeline.store, pid, old['analysis'], {'fingerprint': 'legacy', 'numbered': old['numbered']})
    response = TestClient(apps[0]).post(f'/api/studio/projects/{pid}/refresh', json={'kind': 'insight'})
    assert response.status_code == 202, response.text
    manual = JobExecution(pipeline.store, pipeline.store.claim())
    pipeline.run(manual.job)
    manifest = manual.read_checkpoint(event_insight_work.INPUT)
    assert manifest['budget_basis'] == 'explicit_new_analysis_round' and manifest['round_root_job_id'] == manual.job['id']
    assert manual.read_checkpoint(event_insight_work.RESULT)['status'] == 'complete'
    fresh = insight.load(pipeline.store, pid)
    assert fresh['status'] == 'current' and fresh['id'] != old['id']


def test_event_revision_during_rebuild_review_preserves_stale_current(tmp_path, monkeypatch):
    pipeline, execution, seen = install(tmp_path, monkeypatch)
    pid = execution.job['project_id']
    source(pipeline, execution)
    child = next_insight(pipeline, execution)
    pipeline.run(child.job)
    pipeline.store.finish(child.job['id'], lease_token=child.job['lease_token'])
    old = insight.load(pipeline.store, pid)
    revise_event(pipeline, pid)
    StudioWorker(pipeline).dispatch_rebuilds(model_ready=True)
    rebuild = JobExecution(pipeline.store, pipeline.store.claim())
    seen['hook'] = lambda: revise_event(pipeline, pid, 'changed while reviewer was running')
    pipeline.run(rebuild.job)
    assert rebuild.read_checkpoint(event_insight_work.RESULT)['objects'][0]['reason'] == 'input_version_changed'
    current = insight.load(pipeline.store, pid)
    assert current['id'] == old['id'] and current['analysis'] == old['analysis'] and current['status'] == 'stale'


def test_relation_only_evidence_withdrawal_invalidates_published_insight(tmp_path, monkeypatch):
    from test_studio_bilateral_competition import reviewer
    from test_studio_evidence_versions import sample

    from project_watch.studio.competition import assess_relation
    from project_watch.studio.monitoring_contracts import FactInput, LocatedRef
    pipeline, execution, _seen = install(tmp_path, monkeypatch)
    pid = execution.job['project_id']
    primary = pipeline.objects.ensure_subject(pid)
    peer = pipeline.objects.save(pid, ObjectInput(kind='company', name='Peer', visibility='public'), actor='user')
    products, evidence_ids = [], []
    for company in (primary, peer):
        product_name = company['name'] + ' software'
        identity = company['name'] + ' builds ' + product_name + '.'
        identity_ev = pipeline.evidence.save(sample(pid, identity).model_copy(update={
            'source_key': 'identity:' + company['id'], 'visibility': 'public'}))
        identity_ref = LocatedRef(evidence_version_id=identity_ev['id'], part_index=0, start=0, end=len(identity), quote=identity)
        pipeline.objects.save(pid, ObjectInput(kind='company', name=company['name'], visibility='public', refs=(identity_ref,)),
            actor='user', object_id=company['id'], expected_version=company['version'])
        text = company['name'] + ' supplies ' + product_name + ' for drug screening; buyers are pharma; delivery is software.'
        ev = pipeline.evidence.save(sample(pid, text).model_copy(update={'source_key': company['id'], 'visibility': 'public'}))
        evidence_ids.append(ev['id'])
        ref = LocatedRef(evidence_version_id=ev['id'], part_index=0, start=0, end=len(text), quote=text)
        product = pipeline.objects.save(pid, ObjectInput(kind='product', name=product_name,
            parent_id=company['id'], visibility='public', refs=(identity_ref,)), actor='user')
        products.append(product)
        for predicate, value in [('use_case', 'drug screening'), ('buyer_type', 'pharma'), ('delivery', 'software')]:
            pipeline.objects.save_fact(pid, FactInput(subject_id=product['id'], predicate=predicate, value=value,
                text=value, visibility='public', basis='actual', subject_attribution='explicit', refs=(ref,)))
    scope = dict(subject_id=primary['id'], peer_id=peer['id'], subject_product_id=products[0]['id'], peer_product_id=products[1]['id'])
    checked = assess_relation(pipeline, pid, scope, 'direct', review=reviewer)
    assert checked['support'] == 'supported', checked['missing']
    pipeline.relationships.save_assessment(pid, checked)
    source(pipeline, execution)
    child = next_insight(pipeline, execution)
    pipeline.run(child.job)
    current = insight.load(pipeline.store, pid)
    assert current['status'] == 'current' and set(evidence_ids) <= set(current['input']['evidence_ids'])
    pipeline.evidence.dependencies.withdraw(pid, evidence_ids[0], 'synthetic relation correction', actor='test')
    assert insight.load(pipeline.store, pid)['status'] == 'stale'
    pending = [r for r in pipeline.evidence.dependencies.pending(pid) if r['artifact_key'] == current['artifact_key']]
    assert len(pending) == 1 and pending[0]['status'] == 'queued'


def test_incremental_followup_uses_real_pipeline_material_entry(tmp_path, monkeypatch):
    pipeline, parent, seen = install(tmp_path, monkeypatch)
    pipeline.store.finish(parent.job['id'], lease_token=parent.job['lease_token'])
    pipeline.store.enqueue(parent.job['project_id'], 'external_followup', automatic=True, input_version=parent.job['id'])
    execution = JobExecution(pipeline.store, pipeline.store.claim())
    handoff = source(pipeline, execution)
    assert execution.read_checkpoint('collection_admission_v1')['mode'] == 'incremental'
    assert execution.read_checkpoint('material_pipeline_v1')['owner'] == 'materials'
    assert handoff['phase'] == 'scheduled' and seen['materials'] and not seen['insight']


def test_version_commit_before_role_checkpoint_replays_without_fee_or_duplicate_history(tmp_path, monkeypatch):
    from project_watch.studio import insight_versions
    pipeline, execution, seen = install(tmp_path, monkeypatch)
    source(pipeline, execution)
    child = next_insight(pipeline, execution)
    original = insight_versions.save
    def interrupted(*args):
        original(*args)
        raise RuntimeError('after immutable version commit')
    monkeypatch.setattr(insight_versions, 'save', interrupted)
    with pytest.raises(RuntimeError, match='after immutable version commit'):
        pipeline.run(child.job)
    saved = insight.load(pipeline.store, execution.job['project_id'])
    paid = child.budget_used('model')
    seen_before = len(seen['models']), len(seen['insight'])
    monkeypatch.setattr(insight_versions, 'save', original)
    pipeline.run(child.job)
    assert child.read_checkpoint(event_insight_work.RESULT)['status'] == 'complete'
    assert paid == child.budget_used('model') and seen_before == (len(seen['models']), len(seen['insight']))
    assert insight.load(pipeline.store, execution.job['project_id'])['id'] == saved['id']
    with closing(pipeline.store._connect()) as db:
        assert db.execute('SELECT count(*) FROM studio_insight_versions').fetchone()[0] == 1
