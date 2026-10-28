"""Independent scope worker, using only synthetic transport and temporary SQLite."""
import json
from contextlib import closing
from types import SimpleNamespace

import pytest
from test_bootstrap_collection import BODY
from test_event_insight import install as source_install

from project_watch.studio import public_stage_work, scope_maintenance
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import Http, HttpError, Response
from project_watch.studio.monitoring_contracts import Alias, ObjectInput
from project_watch.studio.pipeline import StudioWorker


def install(tmp_path, monkeypatch, *, native=False):
    pipeline, execution, seen = source_install(tmp_path, monkeypatch, native=native)
    original = pipeline.http_factory(None).transport
    seen.update(scope=[], scope_action='retain', scope_hook=None, target='SYN-ALPHA', relation_type='product')

    def send(method, url, headers, raw, timeout):
        body = json.loads(raw) if raw and 'model.example.invalid' in url else {}
        tools = {t['function']['name'] for t in body.get('tools', [])}
        if 'pw_scope' in tools:
            assert tools == set(scope_maintenance.TOOLS)
            seen['scope'].append(body)
            contexts = [value for m in body['messages'] if m['role'] == 'tool'
                        for value in [json.loads(m['content'])] if isinstance(value, dict) and 'watches' in value]
            if contexts:
                work = contexts[-1]
                if seen['scope_action'] == 'retain':
                    changes = [dict(action='retain', record_key=r['key']) for r in work['records']]
                elif seen['scope_action'] == 'question':
                    changes = [dict(action='question', parent_id=work['watches'][0]['id'],
                                    text='SYN-ALPHA registration progress?', exit_rule='A public registration decision is available.')]
                else:
                    changes = [dict(action='propose', watch_id=work['watches'][0]['id'], fields={'cadence': 'low'})]
                name, args = 'pw_scope', dict(changes_json=json.dumps(changes), reason='Evidence-backed public trial scope.')
                if seen['scope_hook']:
                    seen['scope_hook']()
                    seen['scope_hook'] = None
            else:
                name, args = 'pw_context', {}
            return Response(200, {}, json.dumps({'choices': [{'message': {'role': 'assistant', 'tool_calls': [
                {'id': 'scope-' + str(len(seen['scope'])), 'type': 'function',
                 'function': {'name': name, 'arguments': json.dumps(args)}}]}, 'finish_reason': 'tool_calls'}],
                'usage': {'prompt_tokens': 10, 'completion_tokens': 5}}).encode())
        response = original(method, url, headers, raw, timeout)
        if tools == {'pw_submit_records'}:
            material = seen['materials'][-1]
            for ref, part in material['refs'].items():
                if BODY in part['quote']:
                    value = response.json()
                    function = value['choices'][0]['message']['tool_calls'][0]['function']
                    args = json.loads(function['arguments'])
                    start = part['quote'].index(BODY)
                    args['observations'].append(dict(kind='relation', subject='Synthetic Host', label='SYN-ALPHA development',
                        text=BODY, refs=[ref], start=start, end=start + len(BODY), reason='Synthetic Host develops SYN-ALPHA.',
                        axis='asset', query=seen['target'], relation_type=seen['relation_type'], relation_target=seen['target']))
                    function['arguments'] = json.dumps(args)
                    response = Response(200, {}, json.dumps(value).encode())
                    break
        return response

    pipeline.http_factory = lambda _: Http(send)
    return pipeline, execution, seen


def consume(pipeline, execution, monkeypatch):
    pipeline.run(execution.job)
    intent = execution.read_checkpoint(scope_maintenance.INTENT)
    assert intent['phase'] == 'scheduled'
    pipeline.store.finish(execution.job['id'], lease_token=execution.job['lease_token'])
    worker = StudioWorker(pipeline)
    monkeypatch.setattr(worker, 'schedule', lambda: None)
    assert worker.step()  # Independent insight is terminal before shared-tail scope.
    assert worker.step()
    with closing(pipeline.store._connect()) as db:
        job = dict(db.execute('SELECT * FROM studio_jobs WHERE id=?', (intent['job_id'],)).fetchone())
    assert job['status'] == 'done', job['error']
    return JobExecution(pipeline.store, job)


@pytest.mark.parametrize('native', [False, True])
def test_real_worker_scope_without_judgment_and_completed_receipt_replay(tmp_path, monkeypatch, native):
    pipeline, execution, seen = install(tmp_path, monkeypatch, native=native)
    child = consume(pipeline, execution, monkeypatch)
    result = child.read_checkpoint(scope_maintenance.RESULT)
    assert result['status'] == 'complete' and result['items'][0]['status'] == 'trial'
    watches = pipeline.monitoring_plan.items(execution.job['project_id'])
    assert len(watches) == 1 and watches[0]['scope_kind'] == 'relation' and watches[0]['status'] == 'trial'
    assert not pipeline.monitoring_plan.explorations.managed(watches[0])
    assert 'judged' not in child.read_checkpoint('dsh_state')
    assert child.budget_used('model') == len(seen['models']) + len(seen['insight']) + len(seen['scope'])
    assert child.budget_used('model') <= child.read_checkpoint(public_stage_work.INPUT)['cap']
    count = len(seen['scope'])
    # Replay dispatch checkpoint through a fresh lease; neither mutation nor model repeats.
    with closing(pipeline.store._connect()) as db, db:
        db.execute("UPDATE studio_jobs SET status='queued' WHERE id=?", (child.job['id'],))
    replay = pipeline.store.claim()
    pipeline.run(replay)
    assert len(seen['scope']) == count and len(pipeline.monitoring_plan.items(execution.job['project_id'])) == 1


def periodic(pipeline, pid):
    with closing(pipeline.store._connect()) as db, db:
        db.execute('UPDATE studio_state SET value=? WHERE key=?', ('2020-01-01T00:00:00+00:00', 'scope-last-admitted:' + pid))
    job = scope_maintenance.dispatch(pipeline, pid)
    assert job and job['kind'] == 'scope'
    return JobExecution(pipeline.store, pipeline.store.claim())


def test_periodic_question_inherits_standing_scope_and_does_not_repeat_candidates(tmp_path, monkeypatch):
    pipeline, execution, seen = install(tmp_path, monkeypatch)
    consume(pipeline, execution, monkeypatch)
    pid = execution.job['project_id']
    parent = pipeline.monitoring_plan.items(pid)[0]
    seen['scope_action'] = 'question'
    child = periodic(pipeline, pid)
    pipeline.run(child.job)
    watches = pipeline.monitoring_plan.items(pid)
    question = next(w for w in watches if w['scope_kind'] == 'question')
    assert question['parent_id'] == parent['id'] and question['queries'] == parent['queries']
    assert question['relation_target'] == parent['relation_target'] and question['hop'] == '1'
    assert child.read_checkpoint(public_stage_work.INPUT)['cap'] == 4 and child.budget_used('model') == 2
    assert scope_maintenance.dispatch(pipeline, pid) is None


def test_human_scope_changed_after_dispatch_rejects_old_write(tmp_path, monkeypatch):
    pipeline, execution, seen = install(tmp_path, monkeypatch)
    consume(pipeline, execution, monkeypatch)
    pid = execution.job['project_id']
    parent = pipeline.monitoring_plan.items(pid)[0]
    child = periodic(pipeline, pid)
    seen['scope_action'] = 'propose'
    seen['scope_hook'] = lambda: pipeline.monitoring_plan.update(parent['id'], {'text': 'Owner revised scope'}, actor='human')
    pipeline.run(child.job)
    assert child.read_checkpoint(scope_maintenance.RESULT)['reason'] == 'input_version_changed'
    assert pipeline.monitoring_plan.items(pid)[0]['text'] == 'Owner revised scope'
    with closing(pipeline.store._connect()) as db:
        assert db.execute("SELECT count(*) FROM studio_plan_changes WHERE state='proposed'").fetchone()[0] == 0


def test_human_proposal_keeps_original_scope_and_budget_stop_is_pending(tmp_path, monkeypatch):
    pipeline, execution, seen = install(tmp_path, monkeypatch)
    consume(pipeline, execution, monkeypatch)
    pid = execution.job['project_id']
    parent = pipeline.monitoring_plan.items(pid)[0]
    pipeline.monitoring_plan.update(parent['id'], {'text': 'Owner relation'}, actor='human')
    seen['scope_action'] = 'propose'
    child = periodic(pipeline, pid)
    pipeline.run(child.job)
    assert child.read_checkpoint(scope_maintenance.RESULT)['items'][0]['status'] == 'proposed'
    assert pipeline.monitoring_plan.items(pid)[0]['cadence'] == parent['cadence']
    pipeline.store.finish(child.job['id'], lease_token=child.job['lease_token'])
    next_job = periodic(pipeline, pid)
    manifest = next_job.read_checkpoint(public_stage_work.INPUT)
    next_job.checkpoint(public_stage_work.INPUT, {**manifest, 'cap': 0})
    next_job.checkpoint('event_insight_budget_tail_v1', dict(cap=0, tail_job_id=next_job.job['id']))
    count = len(seen['scope'])
    pipeline.run(next_job.job)
    assert next_job.read_checkpoint(scope_maintenance.RESULT)['reason'] == 'scope_generation_budget_or_turn_limit'
    assert len(seen['scope']) == count


def test_unknown_origin_blocks_scope_dispatch(tmp_path, monkeypatch):
    pipeline, execution, _seen = install(tmp_path, monkeypatch)
    pipeline.run(execution.job)
    execution.checkpoint('dsh_hold', {'code': 'request_outcome_unknown'})
    with pytest.raises(HttpError, match='request_outcome_unknown'):
        scope_maintenance.dispatch(pipeline, execution.job['project_id'], execution)


def test_excluded_public_relationship_cannot_be_reintroduced_by_scope_role(tmp_path, monkeypatch):
    from project_watch.studio.relationship_contracts import PeerSelection
    pipeline, execution, seen = install(tmp_path, monkeypatch)
    pid = execution.job['project_id']
    service = SimpleNamespace(instance_id='synthetic', evidence=pipeline.evidence)
    pipeline.relationships.correct(service, pid, PeerSelection(name='Synthetic Hospital', decision='exclude',
        expected_version=pipeline.store.get_project(pid)['config_version'], idempotency_key='exclude-synthetic'))
    seen.update(target='Synthetic Hospital', relation_type='competitor')
    child = consume(pipeline, execution, monkeypatch)
    assert child.read_checkpoint(scope_maintenance.RESULT)['items'][0]['reason'] == 'owner_excluded'
    assert pipeline.monitoring_plan.items(pid) == []
    assert not pipeline.relationships.page(pid, include_internal=False)


def test_alias_resolves_stable_target_and_frozen_object_version_blocks_write(tmp_path, monkeypatch):
    from project_watch.studio.scope_identity import bindings
    pipeline, execution, _seen = install(tmp_path, monkeypatch)
    pid = execution.job['project_id']
    parent = pipeline.objects.ensure_subject(pid)
    obj = pipeline.objects.save(pid, ObjectInput(kind='product', name='Canonical Product', visibility='public',
        parent_id=parent['id'], aliases=(Alias(name='SYN-ALPHA', visibility='public'),)), actor='user')
    child = consume(pipeline, execution, monkeypatch)
    work = child.read_checkpoint(public_stage_work.INPUT)['work']
    identity = next(iter(work['scope_bindings'].values()))
    assert identity['target_object_id'] == obj['id'] and identity['subject_object_id'] == parent['id']
    with closing(pipeline.store._connect()) as db:
        link = json.loads(db.execute("SELECT value FROM studio_state WHERE key LIKE 'scope-link:%'").fetchone()[0])
        canonical = {**work['records'][0], 'relation_target': obj['name']}
        assert bindings(pipeline, db, pid, [canonical])[canonical['key']]['key'] == identity['key']
    assert link['identity'] == identity and link['watch_id'] == pipeline.monitoring_plan.items(pid)[0]['id']
    other = pipeline.objects.save(pid, ObjectInput(kind='company', name='Other Subject', visibility='public'), actor='user')
    with closing(pipeline.store._connect()) as db:
        different = {**canonical, 'subject': other['name']}
        assert bindings(pipeline, db, pid, [different])[different['key']]['key'] != identity['key']
    following = periodic(pipeline, pid)
    pipeline.objects.save(pid, ObjectInput(kind='product', name='Renamed Product', visibility='public', parent_id=parent['id']),
        actor='user', object_id=obj['id'], expected_version=obj['version'])
    pipeline.run(following.job)
    assert following.read_checkpoint(scope_maintenance.RESULT)['reason'] == 'input_version_changed'


def test_upgraded_existing_scope_seeds_first_period_without_new_candidates(tmp_path, monkeypatch):
    from test_studio_relation_monitoring import RELATION
    pipeline, execution, seen = install(tmp_path, monkeypatch)
    pid = execution.job['project_id']
    pipeline.store.finish(execution.job['id'], lease_token=execution.job['lease_token'])
    pipeline.monitoring_plan.add(pid, RELATION, actor='human', origin='human', visibility='public')
    assert scope_maintenance.dispatch(pipeline, pid) is None
    assert pipeline.store.get_state('scope-last-admitted:' + pid)
    seen['scope_action'] = 'propose'
    child = periodic(pipeline, pid)
    pipeline.run(child.job)
    assert child.read_checkpoint(scope_maintenance.RESULT)['items'][0]['status'] == 'proposed'
    assert child.budget_used('model') == 2 and scope_maintenance.dispatch(pipeline, pid) is None


def test_scope_commit_before_role_checkpoint_recovers_without_model_or_duplicate_watch(tmp_path, monkeypatch):
    pipeline, execution, seen = install(tmp_path, monkeypatch)
    original = scope_maintenance.ScopeRole.save
    def crash(role):
        if role.state['finished']:
            raise RuntimeError('synthetic interruption after scope commit')
        return original(role)
    monkeypatch.setattr(scope_maintenance.ScopeRole, 'save', crash)
    pipeline.run(execution.job)
    intent = execution.read_checkpoint(scope_maintenance.INTENT)
    pipeline.store.finish(execution.job['id'], lease_token=execution.job['lease_token'])
    worker = StudioWorker(pipeline)
    monkeypatch.setattr(worker, 'schedule', lambda: None)
    assert worker.step() and worker.step()
    with closing(pipeline.store._connect()) as db, db:
        assert db.execute('SELECT status FROM studio_jobs WHERE id=?', (intent['job_id'],)).fetchone()[0] == 'failed'
        db.execute("UPDATE studio_jobs SET status='queued' WHERE id=?", (intent['job_id'],))
    count = len(seen['scope'])
    monkeypatch.setattr(scope_maintenance.ScopeRole, 'save', original)
    job = pipeline.store.claim()
    pipeline.run(job)
    assert JobExecution(pipeline.store, job).read_checkpoint(scope_maintenance.RESULT)['status'] == 'complete'
    assert len(seen['scope']) == count and len(pipeline.monitoring_plan.items(execution.job['project_id'])) == 1


def test_existing_watch_alias_then_canonical_candidate_keeps_one_stable_watch(tmp_path, monkeypatch):
    from test_studio_relation_monitoring import RELATION
    pipeline, execution, _seen = install(tmp_path, monkeypatch)
    pid = execution.job['project_id']
    parent = pipeline.objects.ensure_subject(pid)
    obj = pipeline.objects.save(pid, ObjectInput(kind='product', name='Synthetic Hospital', visibility='public',
        parent_id=parent['id'], aliases=(Alias(name='SYN-ALPHA', visibility='public'),)), actor='user')
    original = scope_maintenance.intent
    def existing(research, records):
        pipeline.monitoring_plan.add(pid, {**RELATION, 'text': 'Owner product watch', 'relation_type': 'product',
            'relation_target': 'SYN-ALPHA', 'queries': ['SYN-ALPHA']}, actor='human', origin='human', visibility='public')
        original(research, records)
    monkeypatch.setattr(scope_maintenance, 'intent', existing)
    child = consume(pipeline, execution, monkeypatch)
    assert child.read_checkpoint(scope_maintenance.RESULT)['items'][0]['status'] == 'retained_existing'
    watch = pipeline.monitoring_plan.items(pid)[0]
    candidate = {**child.read_checkpoint(public_stage_work.INPUT)['work']['records'][0], 'relation_target': obj['name']}
    with closing(pipeline.store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        work = scope_maintenance.freeze(pipeline, db, pid, [candidate], {'reason': 'synthetic next public input'})
        public_stage_work.enqueue(pipeline, db, pid, 'scope', work, cap=4)
    next_job = pipeline.store.claim()
    pipeline.run(next_job)
    result = JobExecution(pipeline.store, next_job).read_checkpoint(scope_maintenance.RESULT)
    assert result['items'][0]['watch_id'] == watch['id'] and result['items'][0]['status'] == 'retained_existing'
    assert len(pipeline.monitoring_plan.items(pid)) == 1 and pipeline.monitoring_plan.items(pid)[0]['origin'] == 'human'
