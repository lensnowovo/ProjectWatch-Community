"""Real Pipeline/finish/schedule with temporary stores and scripted transports.

The deliberately small, unchanged review batch limit makes a real closeout:
the second proposal fits alone but not alongside the first. No real providers.
"""
import json
from contextlib import closing
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
import test_collection_host as fixtures
from test_collection_pages import work
from test_studio_dsh_monitor import synthetic_review

from project_watch.studio import (
    bootstrap_collection,
    collection_history,
    collection_host,
    dsh_judgment_review,
    dsh_runtime,
    event_insight_work,
    material_handoff,
    progressive_bootstrap,
    public_stage_work,
)
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import Http, HttpError, Response
from project_watch.studio.monitoring_contracts import EvidenceInput, EvidencePart, ObjectInput
from project_watch.studio.pipeline import StudioWorker


@pytest.fixture
def journey(tmp_path, monkeypatch):
    pipeline, initial = fixtures.setup.__wrapped__(tmp_path)
    monkeypatch.setenv('PROJECT_WATCH_DSH_RUNTIME', 'synthetic-runtime')
    monkeypatch.setenv('PROJECT_WATCH_DSH_MONITOR', '1')
    monkeypatch.delenv('PROJECT_WATCH_DSH_UNAVAILABLE', raising=False)
    monkeypatch.setattr(dsh_judgment_review, 'MAX_FIELDS', 1)
    monkeypatch.setattr(dsh_judgment_review, 'MAX_BATCHES', 1)
    state = dict(rows=[work(i) for i in (1, 2, 3)], model=[], reads=[], source=None)

    def send(_method, url, _headers, raw, _timeout):
        host = urlsplit(url).hostname
        if host == 'api.openalex.org':
            state['reads'].append(host)
            return Response(200, {}, json.dumps({'results': state['rows'], 'meta': {'count': len(state['rows'])}}).encode())
        if host == 'www.cninfo.com.cn':
            state['reads'].append(host)
            return Response(200, {}, b'{"announcements":[],"totalAnnouncement":0}')
        assert host == 'model.example.invalid'
        body = json.loads(raw)
        state['model'].append(body)
        if 'tools' in body:
            source = state['source']
            ref, bound = next(iter(source['refs'].items()))
            text = 'Synthetic abstract'
            start = bound['quote'].index(text)
            proposal = {'observations': [dict(kind='competitor', subject='Synthetic Peer', label='Synthetic evidence',
                text=text, refs=[ref], start=start, end=start + len(text))]}
            message = dict(role='assistant', content=None, tool_calls=[dict(id='candidate', type='function',
                function=dict(name='pw_submit_records', arguments=json.dumps(proposal)))])
        else:
            message = dict(role='assistant', content=json.dumps(synthetic_review(json.loads(body['messages'][-1]['content']))))
        return Response(200, {}, json.dumps({'choices': [{'message': message, 'finish_reason': 'stop'}]}).encode())

    pipeline.http_factory = lambda _: Http(send)
    monkeypatch.setattr(pipeline, 'reader', lambda: SimpleNamespace(read=lambda _url: dict(status='unavailable', text='', error_code='http_failure')))

    def turn(research, gateway, *_args, **_kwargs):
        if research.tools == bootstrap_collection.TOOLS:
            return research.tool('pw_finish', {'gaps': {'company': 'Synthetic fixture'}})
        assert research.tools == ('pw_submit_records',)
        state['source'] = json.loads(research.task)
        reply = gateway.model_call(dict(model=gateway.model.model, messages=[dict(role='user', content=research.task)],
                                        tools=[{'function': {'name': 'pw_submit_records'}}]))
        call = reply['choices'][0]['message']['tool_calls'][0]['function']
        research.tool(call['name'], json.loads(call['arguments']))
        return research.state['finished']

    monkeypatch.setattr(dsh_runtime, 'run_dsh', turn)
    # Leave actual due_external, enqueue, dedup, claim and research unchanged;
    # unrelated internal/profile/legacy analysis schedulers are outside this test.
    for name in ('unanalyzed_ready_projects', 'missing_public_profile_projects', 'due_landscape_projects'):
        monkeypatch.setattr(pipeline.store, name, lambda **_kw: [])
    for name in ('schedule_business', 'schedule_details', 'schedule_competition', 'schedule_insight'):
        monkeypatch.setattr(pipeline, name, lambda *_: None)
    return pipeline, initial, state


def finish(pipeline, execution):
    assert pipeline.store.finish(execution.job['id'], lease_token=execution.job['lease_token'])


def scheduled(pipeline, pid):
    with closing(pipeline.store._connect()) as db, db:
        db.execute("UPDATE studio_projects SET last_external_at='2000-01-01' WHERE project_id=?", (pid,))
    pipeline.store.set_state('external_attempt:' + pid, '2000-01-01')
    worker = StudioWorker(pipeline)
    worker.schedule()
    worker.schedule()  # duplicate scheduler ticks cannot create another owner
    job = pipeline.store.claim()
    assert job['kind'] in ('external', 'external_followup')
    return JobExecution(pipeline.store, job)


def old_round(journey, *, monitor=False):
    pipeline, execution, state = journey
    if monitor:
        pipeline.store.touch(execution.job['project_id'], 'last_external_at')
    pipeline.run(execution.job)
    pending = execution.read_checkpoint(progressive_bootstrap.CLOSEOUT)['pending_task_ids']
    assert len(pending) == 2 and len(state['model']) == 3
    finish(pipeline, execution)
    state['rows'] = []
    return pending


@pytest.mark.parametrize('monitor', [False, True])
def test_pending_advances_via_pipeline_finish_schedule_without_new_sources(journey, monitor):
    pipeline, old, state = journey
    pending = old_round(journey, monitor=monitor)
    with closing(pipeline.store._connect()) as db:
        old_calls = [tuple(r) for r in db.execute('SELECT call_key,status,response_body FROM studio_job_calls WHERE job_id=? ORDER BY call_key', (old.job['id'],))]
        identities = dict(db.execute('SELECT id,evidence_version_id FROM studio_evidence_tasks'))
    current = scheduled(pipeline, old.job['project_id'])
    pipeline.run(current.job)
    receipt = current.read_checkpoint(material_handoff.STAGE)
    assert receipt['transfers'] == [dict(origin=old.job['id'], tasks=pending)]
    assert len(state['model']) == 5  # saved second proposal: zero new extraction
    assert current.budget_used('model') == 5 and current.usage()['model']['requests'] == 2
    with closing(pipeline.store._connect()) as db:
        assert dict(db.execute('SELECT id,evidence_version_id FROM studio_evidence_tasks')) == identities
        assert old_calls == [tuple(r) for r in db.execute('SELECT call_key,status,response_body FROM studio_job_calls WHERE job_id=? ORDER BY call_key', (old.job['id'],))]
        assert dict(db.execute('SELECT id,status FROM studio_evidence_tasks'))[pending[0]] == 'complete'
        assert db.execute('SELECT count(*) FROM studio_material_transfers').fetchone()[0] == 2
    # Crash after durable dispatch completion, before finish; reclaim same job.
    with closing(pipeline.store._connect()) as db, db:
        db.execute("UPDATE studio_jobs SET lease_until='2000-01-01' WHERE id=?", (current.job['id'],))
    reclaimed = JobExecution(pipeline.store, pipeline.store.claim())
    assert reclaimed.job['id'] == current.job['id']
    pipeline.run(reclaimed.job)
    assert len(state['model']) == 5
    finish(pipeline, reclaimed)
    third = scheduled(pipeline, old.job['project_id'])
    pipeline.run(third.job)
    assert len(state['model']) == 6 and third.budget_used('model') == 6
    with closing(pipeline.store._connect()) as db:
        assert {r[0] for r in db.execute('SELECT status FROM studio_evidence_tasks')} == {'complete'}
        assert db.execute('SELECT count(*) FROM studio_items').fetchone()[0] == 3


def test_persisted_limits_enforced_at_journal_after_reconstruction(journey, monkeypatch):
    pipeline, old, state = journey
    limit = collection_host.limit_budgets
    def bounded(execution, mode):
        limit(execution, mode)
        if execution.job['id'] == old.job['id']:
            execution.budgets['model'] = 13  # 7 collection + 6 reserved analysis, one cumulative round.
    monkeypatch.setattr(collection_host, 'limit_budgets', bounded)
    old_round(journey)
    current = scheduled(pipeline, old.job['project_id'])
    pipeline.run(current.job)
    saved = current.read_checkpoint(material_handoff.STAGE)
    assert saved['caps']['model'] == 7
    assert current.read_checkpoint(event_insight_work.ROUND)['cap'] == 13
    # Actual admission must read the durable bound even when an executor has
    # default None, or analysis begin reuses a wider in-memory cap.
    fresh = JobExecution(pipeline.store, current.job)
    event_insight_work.begin(fresh)
    for i in range(2):
        fresh.call('model', 'bounded-extra-' + str(i), lambda: Response(200, {}, b'{}'), provider='synthetic')
    assert fresh.budget_used('model') == 7
    fresh = JobExecution(pipeline.store, current.job)
    count = len(state['model'])
    with pytest.raises(HttpError, match='request_budget'):
        fresh.call('model', 'must-not-send', lambda: pytest.fail('exhausted'), provider='synthetic')
    assert len(state['model']) == count
    assert fresh.usage()['model']['requests'] == current.usage()['model']['requests']
    assert fresh.call('model', 'bounded-extra-0', lambda: pytest.fail('cached reply at cap'), provider='synthetic').body == b'{}'


@pytest.mark.parametrize('boundary', ['pause', 'unknown', 'started'])
def test_protected_projects_cannot_transfer_or_send(journey, boundary):
    pipeline, old, state = journey
    pending = old_round(journey)
    current = scheduled(pipeline, old.job['project_id'])
    before = (len(state['reads']), len(state['model']))
    with closing(pipeline.store._connect()) as db, db:
        if boundary == 'pause':
            db.execute("UPDATE studio_projects SET status='paused' WHERE project_id=?", (old.job['project_id'],))
        else:
            db.execute('UPDATE studio_job_calls SET status=? WHERE job_id=? AND kind=\'model\'', (boundary, old.job['id']))
    with pytest.raises(HttpError, match='project_paused' if boundary == 'pause' else 'request_outcome_unknown'):
        pipeline.run(current.job)
    assert before == (len(state['reads']), len(state['model'])) and not current.usage()
    with closing(pipeline.store._connect()) as db:
        assert db.execute('SELECT count(*) FROM studio_material_transfers').fetchone()[0] == 0
        assert all(db.execute('SELECT job_id FROM studio_evidence_tasks WHERE id=?', (t,)).fetchone()[0] == old.job['id'] for t in pending)


@pytest.mark.parametrize('boundary', ['withdrawn', 'version', 'dismissed', 'scope', 'missing_cap', 'analysis_pending'])
def test_ineligible_materials_retain_reason_and_are_not_reprocessed(journey, boundary):
    pipeline, old, state = journey
    pending = old_round(journey, monitor=True)
    pid = old.job['project_id']
    with closing(pipeline.store._connect()) as db, db:
        if boundary == 'dismissed':
            db.execute("UPDATE studio_items SET status='dismissed'")
        elif boundary == 'scope':
            frozen = old.read_checkpoint(collection_host.SCOPE, db=db)
            frozen['settings_binding'] = 'different-original-provider'
            db.execute('UPDATE studio_job_checkpoints SET data=? WHERE job_id=? AND stage=?',
                       (json.dumps(frozen), old.job['id'], collection_host.SCOPE))
        elif boundary == 'missing_cap':
            db.execute('DELETE FROM studio_job_checkpoints WHERE job_id=? AND stage IN (?,?)',
                       (old.job['id'], material_handoff.CAPS, material_handoff.STAGE))
        elif boundary == 'analysis_pending':
            analysis = old.read_checkpoint(event_insight_work.ROUND, db=db)
            analysis['phase'] = 'awaiting_queue'
            db.execute('UPDATE studio_job_checkpoints SET data=? WHERE job_id=? AND stage=?',
                       (json.dumps(analysis), old.job['id'], event_insight_work.ROUND))
        versions = [db.execute('SELECT evidence_version_id FROM studio_evidence_tasks WHERE id=?', (t,)).fetchone()[0] for t in pending]
    for version in versions:
        if boundary == 'withdrawn':
            pipeline.evidence.dependencies.withdraw(pid, version, 'synthetic correction', actor='test')
        elif boundary == 'version':
            record = pipeline.evidence.get(pid, version)
            pipeline.evidence.save(EvidenceInput(project_id=pid, source_key=record['source_key'], extraction_version='changed',
                source_type='document', visibility='public', read_scope='full',
                parts=[EvidencePart(locator_kind='stored_text', locator='one', text='Synthetic changed assertion')]))
    current = scheduled(pipeline, pid)
    pipeline.run(current.job)
    receipt = current.read_checkpoint(material_handoff.STAGE)
    assert not receipt['transfers'] and receipt['retained']
    expected = {'scope': 'scope_or_closeout_not_compatible', 'missing_cap': 'original_budget_not_bound',
                'analysis_pending': 'analysis_owner_not_settled'}.get(boundary, 'input_no_longer_eligible')
    assert {r['reason'] for r in receipt['retained']} == {expected}
    assert len(state['model']) == 3
    with closing(pipeline.store._connect()) as db:
        assert all(db.execute('SELECT job_id FROM studio_evidence_tasks WHERE id=?', (t,)).fetchone()[0] == old.job['id'] for t in pending)


@pytest.mark.parametrize('kind,cap', [('model', 3), ('open_data', 3)])
def test_exhausted_predecessor_cannot_freeze_next_cycles_new_sources(journey, monkeypatch, kind, cap):
    pipeline, old, state = journey
    # Test two independent periods. Bootstrap's same-round followup is covered
    # separately and must not be mistaken for the next ordinary period.
    pipeline.store.touch(old.job['project_id'], 'last_external_at')
    limit = collection_host.limit_budgets
    def bounded(execution, mode):
        limit(execution, mode)
        if execution.job['id'] == old.job['id']:
            execution.budgets[kind] = 6 if kind == 'model' else cap  # collection cap 3 + reserved tail 3
    monkeypatch.setattr(collection_host, 'limit_budgets', bounded)
    pipeline.run(old.job)
    pending = old.read_checkpoint(progressive_bootstrap.CLOSEOUT)['pending_task_ids']
    assert len(pending) == 2 and old.budget_used('model') == (2 if kind == 'model' else 3)
    finish(pipeline, old)
    state['rows'] = [work(4)]
    # Select the due-query fixture; query history fairness is a separate batch.
    monkeypatch.setattr(collection_history, 'schedule', lambda plan, *_a, **_kw: plan)
    current = scheduled(pipeline, old.job['project_id'])
    assert current.job['kind'] == 'external'
    reads = len(state['reads'])
    pipeline.run(current.job)
    receipt = current.read_checkpoint(material_handoff.STAGE)
    assert not receipt['transfers'] and receipt['retained'][0]['reason'] == 'original_budget_insufficient'
    if kind == 'open_data':
        assert receipt['retained'][0]['used']['open_data'] > cap
    assert len(state['reads']) > reads and current.usage()['model']['requests'] == 2
    assert receipt['caps']['model'] == 10  # ordinary new period retains its own allowance
    with closing(pipeline.store._connect()) as db:
        assert db.execute('SELECT count(*) FROM studio_items').fetchone()[0] == 4
        assert db.execute("SELECT count(*) FROM studio_evidence_tasks WHERE job_id=? AND status='complete'", (current.job['id'],)).fetchone()[0] == 1
        assert all(db.execute('SELECT job_id FROM studio_evidence_tasks WHERE id=?', (t,)).fetchone()[0] == old.job['id'] for t in pending)


def test_transfer_failure_rolls_back_owner_links_caps_and_claim(journey, monkeypatch):
    pipeline, old, state = journey
    pending = old_round(journey)
    current = scheduled(pipeline, old.job['project_id'])
    with closing(pipeline.store._connect()) as db:
        # A bound followup already owns inherited accounting links before the
        # material transfer. Rollback must preserve that baseline exactly.
        before_links = [tuple(r) for r in db.execute('SELECT * FROM studio_job_budget_links WHERE job_id=? ORDER BY origin_job_id,call_key',
                                                   (current.job['id'],))]
    take_over = material_handoff.take_over
    def interrupted(execution, task_id, **kwargs):
        if task_id == pending[-1]:
            raise RuntimeError('synthetic transaction interruption')
        return take_over(execution, task_id, **kwargs)
    monkeypatch.setattr(material_handoff, 'take_over', interrupted)
    with pytest.raises(RuntimeError, match='synthetic transaction interruption'):
        pipeline.run(current.job)
    with closing(pipeline.store._connect()) as db:
        assert not current.read_checkpoint(material_handoff.STAGE, db=db)
        assert not old.read_checkpoint(material_handoff.CLAIM, db=db)
        assert db.execute('SELECT count(*) FROM studio_material_transfers').fetchone()[0] == 0
        assert before_links == [tuple(r) for r in db.execute('SELECT * FROM studio_job_budget_links WHERE job_id=? ORDER BY origin_job_id,call_key',
                                                            (current.job['id'],))]
        assert all(db.execute('SELECT job_id FROM studio_evidence_tasks WHERE id=?', (t,)).fetchone()[0] == old.job['id'] for t in pending)
    monkeypatch.setattr(material_handoff, 'take_over', take_over)
    pipeline.run(current.job)
    assert current.read_checkpoint(material_handoff.STAGE)['transfers'] and len(state['model']) == 5


def test_crash_after_transfer_commit_reclaims_same_material_owner(journey, monkeypatch):
    pipeline, old, state = journey
    pending = old_round(journey)
    current = scheduled(pipeline, old.job['project_id'])
    mapping = progressive_bootstrap._map_records
    def crash(research):
        assert research.execution.read_checkpoint(material_handoff.STAGE)['transfers']
        raise RuntimeError('synthetic crash after handoff commit')
    monkeypatch.setattr(progressive_bootstrap, '_map_records', crash)
    with pytest.raises(RuntimeError, match='synthetic crash after handoff commit'):
        pipeline.run(current.job)
    with closing(pipeline.store._connect()) as db, db:
        assert all(db.execute('SELECT job_id FROM studio_evidence_tasks WHERE id=?', (t,)).fetchone()[0] == current.job['id'] for t in pending)
        db.execute("UPDATE studio_jobs SET lease_until='2000-01-01' WHERE id=?", (current.job['id'],))
    monkeypatch.setattr(progressive_bootstrap, '_map_records', mapping)
    reclaimed = pipeline.store.claim()
    assert reclaimed['id'] == current.job['id'] and reclaimed['lease_token'] != current.job['lease_token']
    pipeline.run(reclaimed)
    assert len(state['model']) == 5
    with closing(pipeline.store._connect()) as db:
        assert db.execute('SELECT count(*) FROM studio_material_transfers').fetchone()[0] == 2


def test_missing_dispatch_provenance_retains_only_unsafe_material(journey):
    pipeline, old, state = journey
    pending = old_round(journey)
    with closing(pipeline.store._connect()) as db, db:
        db.execute('INSERT INTO studio_job_checkpoints VALUES (?,?,?)',
                   (old.job['id'], 'extract_attempt:' + pending[-1], '["unattributed-legacy-attempt"]'))
    current = scheduled(pipeline, old.job['project_id'])
    pipeline.run(current.job)
    receipt = current.read_checkpoint(material_handoff.STAGE)
    assert receipt['retained'] == [dict(task_id=pending[-1], reason='material_provenance_not_reusable')]
    assert receipt['transfers'] == [dict(origin=old.job['id'], tasks=pending[:1])]
    assert len(state['model']) == 4  # original saved proposal only needs review
    with closing(pipeline.store._connect()) as db:
        assert db.execute('SELECT job_id FROM studio_evidence_tasks WHERE id=?', (pending[-1],)).fetchone()[0] == old.job['id']


def test_settings_change_between_collection_and_adoption_cannot_transfer(journey, monkeypatch):
    pipeline, old, state = journey
    pending = old_round(journey)
    current = scheduled(pipeline, old.job['project_id'])
    adopt = material_handoff.adopt
    def changed(research):
        settings = pipeline.settings_store.load()
        settings.model.model = 'different-synthetic-model'
        pipeline.settings_store.save(settings)
        return adopt(research)
    monkeypatch.setattr(material_handoff, 'adopt', changed)
    with pytest.raises((ValueError, RuntimeError), match='changed'):
        pipeline.run(current.job)
    assert len(state['model']) == 3 and not current.read_checkpoint(material_handoff.STAGE)
    with closing(pipeline.store._connect()) as db:
        assert all(db.execute('SELECT job_id FROM studio_evidence_tasks WHERE id=?', (t,)).fetchone()[0] == old.job['id'] for t in pending)


def test_transferred_material_and_original_unresolved_analysis_share_one_tail(journey):
    pipeline, old, _state = journey
    old_round(journey)
    current = scheduled(pipeline, old.job['project_id'])
    pipeline.run(current.job)
    assert current.read_checkpoint(material_handoff.STAGE)['transfers']
    round_state = current.read_checkpoint(event_insight_work.ROUND)
    assert round_state['round_root_job_id'] == old.job['id']
    assert old.read_checkpoint(event_insight_work.ROUND)['phase'] == 'waiting_inputs'
    # A later stage based on the material successor must not establish a second
    # root. Its final calls also count when the original unresolved input wakes.
    with closing(pipeline.store._connect()) as db, db:
        queued = public_stage_work.enqueue(pipeline, db, old.job['project_id'], 'scope', {'synthetic': True},
                                           origin=current.job['id'], cap=round_state['cap'])
    manifest = JobExecution(pipeline.store, queued).read_checkpoint(public_stage_work.INPUT)
    assert manifest['round_root_job_id'] == old.job['id']
    assert manifest['predecessor_job_id'] == current.job['id']
    finish(pipeline, current)
    tail = JobExecution(pipeline.store, pipeline.store.claim())
    assert tail.job['id'] == queued['id']
    public_stage_work.prepare(pipeline, tail, manifest)
    tail.call('model', 'synthetic-tail', lambda: Response(200, {}, b'{}'), provider='synthetic')
    finish(pipeline, tail)
    pipeline.objects.save(old.job['project_id'], ObjectInput(kind='company', name='Synthetic Peer',
        purpose='related', visibility='public'), actor='user')
    queued = type(pipeline).schedule_insight(pipeline, old.job['project_id'])
    assert queued
    analysis = JobExecution(pipeline.store, pipeline.store.claim())
    manifest = analysis.read_checkpoint(event_insight_work.INPUT)
    assert manifest['round_root_job_id'] == old.job['id']
    assert manifest['predecessor_job_id'] == tail.job['id']
    assert manifest['cap'] == round_state['cap']
    assert analysis.budget_used('model') == tail.budget_used('model') == 6
