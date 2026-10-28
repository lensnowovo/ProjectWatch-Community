"""Fresh topic materials and independent questions, without live providers."""
import json
from contextlib import closing

import pytest
from test_bootstrap_collection import BODY
from test_event_insight import install as source_install

from project_watch.studio import dsh_topic, question_assessment, question_work
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import Http, HttpError, Response
from project_watch.studio.pipeline import StudioWorker


def install(tmp_path, monkeypatch, *, native=False, answered=False, source_count=1):
    pipeline, initial, seen = source_install(tmp_path, monkeypatch, native=native, source_count=source_count)
    pid = initial.job['project_id']
    pipeline.store.finish(initial.job['id'], lease_token=initial.job['lease_token'])
    pipeline._execution_state.current = None
    original = pipeline.http_factory(None).transport
    seen.update(assess=[], question_reviews=[], question_hook=None, answer='answered' if answered else 'unresolved', empty=False, reject=False)
    parent = pipeline.monitoring_plan.add(pid, dict(text='SYN-ALPHA', scope_kind='relation', relation_type='product',
        relation_target='SYN-ALPHA', sources='https://public.example.invalid/overview', reason='Public source scope',
        queries=['SYN-ALPHA'], tier='background', cadence='medium'), actor='system', origin='system', visibility='public')
    watch = pipeline.monitoring_plan.add(pid, dict(text='SYN-ALPHA development', scope_kind='question', parent_id=parent['id'],
        queries=['SYN-ALPHA'], tier='topic', cadence='medium', exit_rule=BODY),
        actor='system', origin='system', visibility='public')

    def tool(name, args, number):
        return Response(200, {}, json.dumps({'choices': [{'message': {'role': 'assistant', 'tool_calls': [
            {'id': str(number), 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]},
            'finish_reason': 'tool_calls'}], 'usage': {'prompt_tokens': 10, 'completion_tokens': 5}}).encode())

    def send(method, url, headers, raw, timeout):
        if seen['empty'] and 'api.tavily.com' in url:
            return Response(200, {}, b'{"results":[]}')
        body = json.loads(raw) if raw and 'model.example.invalid' in url else {}
        tools = {t['function']['name'] for t in body.get('tools', [])}
        if 'pw_collect' in tools:
            seen['models'].append(body)
            return tool('pw_finish', {'gaps': {}}, len(seen['models']))
        if 'pw_assess' in tools:
            assert tools == set(question_assessment.TOOLS)
            seen['assess'].append(body)
            if seen.get('extra_status') and len(seen['assess']) == 1:
                return tool('pw_status', {}, 1)
            contexts = [value for m in body['messages'] if m['role'] == 'tool'
                for value in [json.loads(m['content'])] if isinstance(value, dict) and 'numbered' in value]
            if not contexts:
                return tool('pw_context', {}, len(seen['assess']))
            ctx = contexts[-1]
            args = dict(watch_id=ctx['watches'][0]['id'], status='progress', change_basis='evidence_only',
                note=BODY, refs=list(ctx['numbered']), answer_status=seen['answer'],
                gaps=[] if seen['answer'] == 'answered' else ['Await a later registration decision.'])
            return tool('pw_assess', args, len(seen['assess']))
        if body and not tools:
            payload = json.loads(body['messages'][-1]['content'])
            if payload.get('question_contract'):
                seen['question_reviews'].append(body)
                if seen['question_hook']:
                    seen['question_hook']()
                    seen['question_hook'] = None
                if seen['reject']:
                    checks = [dict(field=key, status='insufficient', reason='Synthetic exit condition not supported', evidence=[])
                              for key in payload['fields']]
                    return Response(200, {}, json.dumps({'choices': [{'message': {'role': 'assistant',
                        'content': json.dumps({'checks': checks})}, 'finish_reason': 'stop'}]}).encode())
        response = original(method, url, headers, raw, timeout)
        if tools == {'pw_submit_records'}:
            material = seen['materials'][-1]
            pair = next(((r, p) for r, p in material['refs'].items() if BODY in p['quote']), None)
            if pair is None:
                return tool('pw_submit_records', {'observations': []}, len(seen['models']))
            ref, part = pair
            start = part['quote'].index(BODY)
            response = tool('pw_submit_records', {'observations': [dict(kind='company', subject='Synthetic Host',
                label='SYN-ALPHA development', text=BODY, refs=[ref], start=start, end=start + len(BODY))]}, len(seen['models']))
        return response

    pipeline.http_factory = lambda _: Http(send)
    job = dsh_topic.enqueue_topic(pipeline, pid, watch['id'], watch['lifecycle_version'])
    execution = JobExecution(pipeline.store, pipeline.store.claim())
    assert execution.job['id'] == job['id']
    return pipeline, execution, watch, seen


def source(pipeline, execution):
    pipeline.run(execution.job)
    assert execution.read_checkpoint('material_pipeline_v1')['owner'] == 'materials'
    work = execution.read_checkpoint(question_work.INTENT)
    assert work and work['phase'] == 'scheduled'
    assert work['questions'][0]['numbered']
    pipeline.store.finish(execution.job['id'], lease_token=execution.job['lease_token'])
    return work


@pytest.mark.parametrize('native', [False, True])
def test_fresh_topic_worker_material_then_independent_question_without_judge_or_revise(tmp_path, monkeypatch, native):
    pipeline, execution, watch, seen = install(tmp_path, monkeypatch, native=native)
    work = source(pipeline, execution)
    assert not seen['assess'] and not seen['insight']
    count = len(seen['pages']), len(seen['materials'])
    worker = StudioWorker(pipeline)
    monkeypatch.setattr(worker, 'schedule', lambda: None)
    assert worker.step()
    with closing(pipeline.store._connect()) as db:
        child = dict(db.execute('SELECT * FROM studio_jobs WHERE id=?', (work['job_id'],)).fetchone())
    assert child['status'] == 'done', child['error']
    result = JobExecution(pipeline.store, child).read_checkpoint(question_work.RESULT)
    assert result['status'] == 'complete' and result['questions'][0]['knowledge_state'] == 'unresolved'
    assert result['questions'][0]['assessment_status'] == 'unchanged'
    assert count == (len(seen['pages']), len(seen['materials'])) and seen['question_reviews']
    assert seen['question_reviews'][0]['messages'][0]['role'] == 'system'
    payload = json.loads(seen['question_reviews'][0]['messages'][-1]['content'])
    assert payload['question_contract']['exit_rule'] == watch['exit_rule']
    assert result['model_used'] <= result['cap'] and not seen['insight']


def test_answered_exit_closes_only_automatic_question_and_replay_is_local(tmp_path, monkeypatch):
    pipeline, execution, watch, seen = install(tmp_path, monkeypatch, answered=True)
    source(pipeline, execution)
    child = pipeline.store.claim()
    pipeline.run(child)
    outcome = JobExecution(pipeline.store, child).read_checkpoint(question_work.RESULT)['questions'][0]
    assert outcome['knowledge_state'] == outcome['closure'] == 'answered'
    assert pipeline.monitoring_plan.explorations.view(execution.job['project_id'])[watch['id']]['reason'] == 'answered'
    calls = len(seen['assess']), len(seen['question_reviews'])
    pipeline.run(child)
    assert calls == (len(seen['assess']), len(seen['question_reviews']))
    assert len(pipeline.questions.history(watch['id'], include_internal=False)) == 1


@pytest.mark.parametrize('change', ['confirmed', 'human_state', 'exit', 'event', 'new_event'])
def test_review_commit_respects_human_takeover_and_new_input(tmp_path, monkeypatch, change):
    from test_event_insight import revise_event
    pipeline, execution, watch, seen = install(tmp_path, monkeypatch, answered=True)
    pid = execution.job['project_id']
    source(pipeline, execution)
    def change_input():
        if change == 'confirmed':
            pipeline.monitoring_plan.confirm_review(pid)
        elif change == 'human_state':
            pipeline.monitoring_plan.update(watch['id'], {'current_state': 'Owner conclusion'}, actor='human')
        elif change == 'exit':
            pipeline.monitoring_plan.update(watch['id'], {'exit_rule': 'A later registration record'}, actor='human')
        elif change == 'event':
            revise_event(pipeline, pid)
        else:
            pipeline.store.add_items(pid, 'company', [dict(key='counter', title='SYN-ALPHA counter evidence',
                snippet='SYN-ALPHA development now paused', url='https://public.example.invalid/counter', source='Synthetic', published_at=None)])
            item = next(i for i in pipeline.store.untriaged(pid) if i['title'] == 'SYN-ALPHA counter evidence')
            pipeline.store.create_event(pid, dict(title=item['title'], category='company', importance='notable'), [item['id']])
            question_work.refresh(pipeline, pid)
    seen['question_hook'] = change_input
    child = pipeline.store.claim()
    pipeline.run(child)
    result = JobExecution(pipeline.store, child).read_checkpoint(question_work.RESULT)['questions'][0]
    if change in ('confirmed', 'human_state'):
        assert result['closure'] == 'owner_confirmation_required'
        if change == 'human_state':
            assert next(w for w in pipeline.monitoring_plan.items(pid) if w['id'] == watch['id'])['current_state'] == 'Owner conclusion'
    else:
        assert result['reason'] == 'input_version_changed' and not pipeline.questions.history(watch['id'], include_internal=False)
    assert pipeline.monitoring_plan.explorations.view(pid)[watch['id']]['reason'] != 'answered'


def test_shared_small_budget_preserves_draft_without_answer_or_hold(tmp_path, monkeypatch):
    from project_watch.studio import event_insight_work, progressive_bootstrap
    from project_watch.studio.event_insight import RoleExecution
    pipeline, execution, watch, seen = install(tmp_path, monkeypatch)
    progressive_bootstrap.freeze(execution, 'monitor', materials=True)
    execution.budgets['model'] = 5
    seen['extra_status'] = True
    event_insight_work.begin(execution)
    execution.checkpoint(question_work.PROTOCOL, True)
    work = source(pipeline, execution)
    child = pipeline.store.claim()
    pipeline.run(child)
    current = JobExecution(pipeline.store, child)
    result = current.read_checkpoint(question_work.RESULT)
    assert result['status'] == 'pending' and result['model_used'] <= 5
    assert result['questions'][0]['reason'] == 'semantic_review_budget'
    assert RoleExecution(current, work['questions'][0]['fingerprint']).read_checkpoint('question_draft')
    assert not current.read_checkpoint('dsh_hold') and not seen['question_reviews']
    assert pipeline.monitoring_plan.explorations.view(execution.job['project_id'])[watch['id']]['no_gain'] == 0


def test_unknown_blocks_restart_and_never_publishes(tmp_path, monkeypatch):
    pipeline, execution, watch, _seen = install(tmp_path, monkeypatch)
    source(pipeline, execution)
    child = pipeline.store.claim()
    calls = []
    def unknown(*args):
        calls.append(1)
        raise HttpError('connection_failed')
    pipeline.http_factory = lambda _: Http(unknown)
    for _ in range(2):
        with pytest.raises(HttpError, match=r'request_outcome_unknown|connection_failed'):
            pipeline.run(child)
    assert len(calls) == 1 and not pipeline.questions.history(watch['id'], include_internal=False)
    assert JobExecution(pipeline.store, child).read_checkpoint('dsh_hold')


def test_rejected_review_cannot_answer_or_consume_events(tmp_path, monkeypatch):
    pipeline, execution, watch, seen = install(tmp_path, monkeypatch, answered=True)
    source(pipeline, execution)
    seen['reject'] = True
    child = pipeline.store.claim()
    with pytest.raises(ValueError, match='judgment_evidence_review'):
        pipeline.run(child)
    assert not pipeline.questions.history(watch['id'], include_internal=False)
    assert pipeline.questions.new_events(execution.job['project_id'], watch['id'])
    assert pipeline.monitoring_plan.explorations.view(execution.job['project_id'])[watch['id']]['reason'] != 'answered'


def test_atomic_answer_receipt_survives_role_checkpoint_interruption(tmp_path, monkeypatch):
    pipeline, execution, watch, seen = install(tmp_path, monkeypatch)
    source(pipeline, execution)
    child = pipeline.store.claim()
    original = question_assessment.QuestionRole.save
    def crash(role):
        if role.execution.read_checkpoint('question_answer'):
            raise RuntimeError('after atomic answer')
        return original(role)
    monkeypatch.setattr(question_assessment.QuestionRole, 'save', crash)
    with pytest.raises(RuntimeError, match='after atomic answer'):
        pipeline.run(child)
    paid = (len(seen['assess']), len(seen['question_reviews']))
    monkeypatch.setattr(question_assessment.QuestionRole, 'save', original)
    pipeline.run(child)
    assert paid == (len(seen['assess']), len(seen['question_reviews']))
    assert len(pipeline.questions.history(watch['id'], include_internal=False)) == 1


@pytest.mark.parametrize('change', ['correction', 'withdrawal'])
def test_changed_evidence_wakes_once_and_stale_answer_survives_failed_recheck(tmp_path, monkeypatch, change):
    from test_event_insight import revise_event
    pipeline, execution, watch, _seen = install(tmp_path, monkeypatch, answered=True)
    pid = execution.job['project_id']
    source(pipeline, execution)
    child = pipeline.store.claim()
    pipeline.run(child)
    pipeline.store.finish(child['id'], lease_token=child['lease_token'])
    old = question_work.current(pipeline, pid, watch['id'])
    before = pipeline.monitoring_plan.explorations.view(pid)[watch['id']]['generation']
    if change == 'correction':
        revise_event(pipeline, pid)
    else:
        pipeline.evidence.dependencies.withdraw(pid, old['work']['evidence_ids'][0], 'Synthetic correction', actor='user')
    worker = StudioWorker(pipeline)
    worker.dispatch_rebuilds(model_ready=True)
    worker.dispatch_rebuilds(model_ready=True)
    question_work.dispatch(pipeline, pid)
    state = pipeline.monitoring_plan.explorations.view(pid)[watch['id']]
    assert state['state'] == 'active' and state['generation'] == before + 1
    current = question_work.current(pipeline, pid, watch['id'])
    assert current['knowledge_state'] == 'needs_recheck' and current['text'] == old['text']
    view = dsh_topic.latest_topic(pipeline.store, pid, watch['id'], pipeline=pipeline)
    assert view['result']['knowledge_state'] == 'needs_recheck' and view['result']['sources']
    rebuild = pipeline.store.claim()
    assert rebuild and rebuild['kind'] == 'assessment'
    pipeline.store.finish(rebuild['id'], error='Synthetic recheck failure', lease_token=rebuild['lease_token'])
    worker.dispatch_rebuilds(model_ready=True)
    worker.dispatch_rebuilds(model_ready=True)
    assert pipeline.store.claim() is None
    assert question_work.current(pipeline, pid, watch['id'])['text'] == old['text']
    assert pipeline.monitoring_plan.explorations.view(pid)[watch['id']]['generation'] == before + 1


def test_scheduler_baseline_survives_sleep_and_ordinary_downstream_failures(tmp_path, monkeypatch):
    pipeline, execution, watch, seen = install(tmp_path, monkeypatch)
    pid = execution.job['project_id']
    pipeline.store.finish(execution.job['id'], lease_token=execution.job['lease_token'])
    pipeline.monitoring_plan.explorations.stop(watch, 'no_gain')
    for kind in ('insight', 'scope', 'assessment'):
        job = pipeline.store.enqueue(pid, kind)
        claimed = pipeline.store.claim()
        assert claimed['id'] == job['id']
        pipeline.store.finish(job['id'], error='Synthetic ordinary failure', lease_token=claimed['lease_token'])
    with closing(pipeline.store._connect()) as db, db:
        db.execute("UPDATE studio_projects SET last_external_at='2000-01-01T00:00:00+00:00' WHERE project_id=?", (pid,))
    worker = StudioWorker(pipeline)
    worker.schedule()
    child = pipeline.store.claim()
    assert child['kind'] == 'external'
    pipeline.run(child)
    plan = JobExecution(pipeline.store, child).read_checkpoint('collection_round_v1')['plan']
    assert any(u['target_id'] == pid and u['disposition'] == 'pending' for u in plan['units'])
    assert not any(u['target_id'] == 'watch:' + watch['id'] and u['disposition'] == 'pending' for u in plan['units'])
    awake = pipeline.monitoring_plan.explorations.view(pid)[watch['id']]
    assert awake['reason'] == 'new_evidence' and awake['state'] == 'active'
    calls = len(seen['models']), len(seen['materials'])
    pipeline.run(child)
    assert calls == (len(seen['models']), len(seen['materials']))
    assert pipeline.monitoring_plan.explorations.view(pid)[watch['id']]['generation'] == awake['generation']


def test_three_actual_empty_topic_cycles_finish_once_and_sleep_only_question(tmp_path, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from project_watch.studio import collection_host, query_ledger

    pipeline, execution, watch, seen = install(tmp_path, monkeypatch)
    pid = execution.job['project_id']
    seen['empty'] = True
    at = datetime.now(UTC)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return at
    monkeypatch.setattr(query_ledger, 'datetime', Clock)
    monkeypatch.setattr(collection_host, 'now', lambda: at.isoformat())
    for index, expected in enumerate((1, 1, 2, 3)):
        if index >= 2:
            at += timedelta(hours=2)  # A new acquisition after the real cache TTL.
        if index:
            dsh_topic.enqueue_topic(pipeline, pid, watch['id'], watch['lifecycle_version'])
            execution = JobExecution(pipeline.store, pipeline.store.claim())
        pipeline.run(execution.job)
        work = execution.read_checkpoint(question_work.INTENT)
        assert work['collection_complete'] and not work['questions'][0]['numbered']
        pipeline.store.finish(execution.job['id'], lease_token=execution.job['lease_token'])
        child = pipeline.store.claim()
        pipeline.run(child)
        pipeline.run(child)
        pipeline.store.finish(child['id'], lease_token=child['lease_token'])
        state = pipeline.monitoring_plan.explorations.view(pid)[watch['id']]
        with closing(pipeline.store._connect()) as db:
            cycle = dict(db.execute('SELECT * FROM studio_exploration_cycles WHERE watch_id=? AND cycle=?',
                                   (watch['id'], execution.job['id'])).fetchone())
        assert state['no_gain'] == expected, json.dumps(dict(state=state, cycle=cycle,
            child=child['id'], result=JobExecution(pipeline.store, child).read_checkpoint(question_work.RESULT)))
        if index == 1:
            assert cycle['state'] == 'reused'
    assert state['state'] == 'stopped' and state['reason'] == 'no_gain'
    assert not seen['assess'] and not seen['question_reviews']
    assert next(w for w in pipeline.monitoring_plan.items(pid) if w['id'] == watch['parent_id'])['status'] == 'trial'
    assert watch['parent_id'] not in pipeline.monitoring_plan.explorations.view(pid)


def test_partial_source_withdrawal_rebuild_filters_refs_and_does_not_block_scheduler_baseline(tmp_path, monkeypatch):
    from project_watch.studio import public_stage_work
    pipeline, execution, watch, _seen = install(tmp_path, monkeypatch, answered=True, source_count=2)
    pid = execution.job['project_id']
    source(pipeline, execution)
    child = pipeline.store.claim()
    pipeline.run(child)
    pipeline.store.finish(child['id'], lease_token=child['lease_token'])
    old = question_work.current(pipeline, pid, watch['id'])
    assert len(old['work']['evidence_ids']) == 2 and len(old['work']['event_versions']) == 1
    withdrawn = old['work']['evidence_ids'][0]
    pipeline.evidence.dependencies.withdraw(pid, withdrawn, 'Synthetic source withdrawal', actor='user')
    with closing(pipeline.store._connect()) as db, db:
        db.execute("UPDATE studio_projects SET last_external_at='2000-01-01T00:00:00+00:00' WHERE project_id=?", (pid,))
    pipeline.store.set_state('external_attempt:' + pid, '2000-01-01T00:00:00+00:00')
    worker = StudioWorker(pipeline)
    worker.schedule()
    worker.schedule()
    with closing(pipeline.store._connect()) as db:
        jobs = [dict(r) for r in db.execute("SELECT * FROM studio_jobs WHERE project_id=? AND status='queued'", (pid,))]
    assert any(j['kind'] == 'external' for j in jobs)
    pending = [j for j in jobs if j['kind'] == 'assessment']
    assert len(pending) == 1
    manifest = JobExecution(pipeline.store, pending[0]).read_checkpoint(public_stage_work.INPUT)
    assert manifest['work']['questions'][0]['evidence_ids'] == [e for e in old['work']['evidence_ids'] if e != withdrawn]


@pytest.mark.parametrize('guard', ['paused', 'lease'])
def test_question_pre_dispatch_guards_do_not_send_or_close(tmp_path, monkeypatch, guard):
    pipeline, execution, watch, seen = install(tmp_path, monkeypatch)
    pid = execution.job['project_id']
    source(pipeline, execution)
    child = pipeline.store.claim()
    if guard == 'paused':
        pipeline.store.set_status(pid, 'paused')
    else:
        with closing(pipeline.store._connect()) as db, db:
            db.execute("UPDATE studio_jobs SET lease_token='other-worker' WHERE id=?", (child['id'],))
    with pytest.raises(HttpError if guard == 'paused' else RuntimeError, match=r'project_paused|job lease expired or replaced'):
        pipeline.run(child)
    assert not seen['assess'] and not seen['question_reviews']
    assert not pipeline.questions.history(watch['id'], include_internal=False)


def test_incomplete_body_collection_never_counts_no_gain_or_answers(tmp_path, monkeypatch):
    from project_watch.studio import collection_host

    pipeline, execution, watch, seen = install(tmp_path, monkeypatch)
    pid = execution.job['project_id']
    original = collection_host.limit_budgets
    def limit(current, mode):
        original(current, mode)
        current.budgets['page'] = 0
    monkeypatch.setattr(collection_host, 'limit_budgets', limit)
    pipeline.run(execution.job)
    work = execution.read_checkpoint(question_work.INTENT)
    assert not work['collection_complete']
    pipeline.store.finish(execution.job['id'], lease_token=execution.job['lease_token'])
    child = pipeline.store.claim()
    pipeline.run(child)
    assert pipeline.monitoring_plan.explorations.view(pid)[watch['id']]['no_gain'] == 0
    assert pipeline.monitoring_plan.explorations.view(pid)[watch['id']]['reason'] != 'answered'
    assert not seen['pages'] and not seen['question_reviews']
