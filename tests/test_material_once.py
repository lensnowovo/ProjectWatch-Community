"""Finite same-ledger continuation; real isolated storage, synthetic providers."""
import json
import os
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import test_collection_host as fixtures
from test_collection_plan import candidate, plan
from test_studio_dsh_monitor import synthetic_review

from project_watch.studio import dsh_runtime, evidence_tasks
from project_watch.studio import material_once as once
from project_watch.studio.collection_ingest import CollectionIngest
from project_watch.studio.dsh_bootstrap import PublicBootstrap
from project_watch.studio.http import Http, HttpError, Response
from project_watch.studio.sources import _item


@pytest.fixture
def terminal(tmp_path):
    pipeline, ex = fixtures.setup.__wrapped__(tmp_path)
    ex.checkpoint('material_pipeline_v1', dict(owner='materials', entry='bootstrap', policy=evidence_tasks.TYPED_POLICY,
                                             mapping_version='subject-v1'))
    ex.checkpoint('material_round_caps_v1', {**ex.budgets, 'model': 10})
    unit = plan([candidate() | dict(category='paper', source='openalex_affiliation')],
                project_id=ex.job['project_id'], round_id=ex.job['id'])['units'][0]
    refs = []
    for i in range(3):
        item = _item('paper', 'Synthetic paper ' + str(i), 'https://example.org/paper/' + str(i), source='Synthetic',
                     snippet='Synthetic Host reports a public study.',
                     extra={'doi': '10.000/test' + str(i), 'source_identity': 'synthetic:' + str(i),
                            'source_record': {'affiliation': 'Synthetic Host', 'abstract': 'Synthetic public abstract.'}})
        refs += CollectionIngest(ex)(unit, [item])['evidence_refs']
    ex.checkpoint('collection_source_imports_v1', refs)
    ex.checkpoint('collection_admission_v1', {'enabled': False, 'reason': 'synthetic_saved_collection'})
    settings, _, _, collectors = pipeline.tools(execution=ex)
    research = PublicBootstrap(pipeline, ex, collectors, None)
    research.state['finished'] = {'status': 'partial', 'records': 0}
    research.save()
    with closing(pipeline.store._connect()) as db, db:
        ids = [r[0] for r in db.execute('SELECT id FROM studio_evidence_tasks ORDER BY rowid')]
        db.execute("UPDATE studio_evidence_tasks SET status='rejected',result=? WHERE id=?",
                   (json.dumps({'code': 'old_failure'}), ids[0]))
    ex.checkpoint('extract_result:' + ids[0], {'proposal': {'invalid': 'old_failure'}})
    ex.checkpoint('material_closeout_v1', {'reason': 'publication_reserve', 'pending_task_ids': ids[1:]})
    for i in range(8):
        ex.call('model', 'settled-' + str(i), lambda: Response(200, {}, b'{}'))
    pipeline.store.finish(ex.job['id'], lease_token=ex.job['lease_token'])
    frozen = once.plan(pipeline.store, ex.job['id'], ids[1])
    with closing(pipeline.store._connect()) as db:
        history = dict(db.execute('SELECT stage,data FROM studio_job_checkpoints WHERE job_id=?', (ex.job['id'],)))
    return pipeline, frozen, history, settings


def start(terminal):
    pipeline, frozen, _, _ = terminal
    return once.begin(pipeline.store, frozen, deadline=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                      grant_id='synthetic-reviewed-grant')


def provider(terminal, monkeypatch, *, mode='valid', native=False):
    pipeline, _, _, _ = terminal
    active, sent = {}, []
    original = dsh_runtime.run_dsh
    def send(method, url, headers, raw, timeout):
        assert method == 'POST' and url == 'https://model.example.invalid/v1/chat/completions'
        body = json.loads(raw)
        sent.append(body)
        if 'tools' in body:
            if mode == 'unknown':
                raise TimeoutError('synthetic')
            if mode == 'length':
                message, reason = {'role': 'assistant', 'content': None, 'reasoning_content': 'Synthetic'}, 'length'
            elif mode == 'no_tool':
                message, reason = {'role': 'assistant', 'content': 'No submission'}, 'stop'
            else:
                source = active['source']
                ref, value = next(iter(source['refs'].items()))
                text = 'Synthetic Host reports a public study.'
                offset = value['quote'].index(text)
                row = dict(kind='company', subject='Synthetic Host', label='Public study', text=text,
                           refs=[ref], start=offset, end=offset + len(text))
                if mode == 'invalid':
                    row['refs'] = ['0' * 64]
                proposal = {'observations': [row]}
                if mode == 'many':
                    proposal['observations'] = [{**row, 'label': 'Study ' + str(i)} for i in range(25)]
                message = {'role': 'assistant', 'tool_calls': [dict(id='records', type='function',
                           function=dict(name='pw_submit_records', arguments=json.dumps(proposal)))]}
                reason = 'tool_calls'
        else:
            if mode == 'review_unknown':
                raise TimeoutError('synthetic review')
            payload = json.loads(body['messages'][-1]['content'])
            checked = synthetic_review(payload)
            if mode == 'semantic':
                checked['checks'][0].update(status='insufficient', reason='Synthetic rejection', evidence=[])
            message = {'role': 'assistant', 'content': json.dumps(checked)}
            reason = 'length' if mode == 'review_length' else 'stop'
        return Response(200, {}, json.dumps({'choices': [{'message': message, 'finish_reason': reason}]}).encode())
    pipeline.http_factory = lambda _: Http(send)
    def turn(role, gateway, *args, **kwargs):
        active['source'] = json.loads(role.task)
        if native:
            return original(role, gateway, *args, **kwargs)
        payload = dict(model=gateway.model.model, messages=[{'role': 'user', 'content': role.task}],
                       tools=[{'function': {'name': 'pw_submit_records'}}])
        result = gateway.model_call(payload)
        if (role.state['finished'] or {}).get('deferred'):
            return role.state['finished']
        message = result['choices'][0]['message']
        if message.get('tool_calls'):
            call = message['tool_calls'][0]['function']
            role.tool(call['name'], json.loads(call['arguments']))
            return role.state['finished']
        # A second attempt must stop at the original journal, before transport.
        gateway.model_call({**payload, 'messages': payload['messages'] + [message]})
        pytest.fail('second extraction call was allowed')
    monkeypatch.setattr(dsh_runtime, 'run_dsh', turn)
    return sent


def unchanged_history(terminal):
    pipeline, frozen, history, _ = terminal
    with closing(pipeline.store._connect()) as db:
        assert dict(db.execute('SELECT * FROM studio_jobs WHERE id=?', (frozen['job_id'],)).fetchone()) == frozen['original_job']
        current = dict(db.execute('SELECT stage,data FROM studio_job_checkpoints WHERE job_id=?', (frozen['job_id'],)))
        assert all(current[k] == value for k, value in history.items())
        assert db.execute("SELECT count(*) FROM studio_evidence_tasks WHERE status='rejected' AND result=?",
                          (json.dumps({'code': 'old_failure'}),)).fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM studio_evidence_tasks WHERE status='pending'").fetchone()[0] >= 1
    with pytest.raises(ValueError, match='already consumed'):
        once.plan(pipeline.store, frozen['job_id'], frozen['material']['task_id'])


@pytest.mark.parametrize('mode,count,published', [('valid', 2, 1), ('length', 1, 0), ('invalid', 1, 0),
    ('semantic', 2, 0), ('many', 1, 0), ('unknown', 1, 0), ('no_tool', 1, 0), ('review_unknown', 2, 0), ('review_length', 2, 0)])
def test_one_material_bounded_stop_and_original_history(terminal, monkeypatch, tmp_path, mode, count, published):
    sent = provider(terminal, monkeypatch, mode=mode)
    execution = start(terminal)
    if mode in ('unknown', 'no_tool', 'review_unknown', 'review_length'):
        with pytest.raises((HttpError, TimeoutError)):
            once.run(terminal[0], execution, Path('.'), tmp_path / 'role')
    else:
        result = once.run(terminal[0], execution, Path('.'), tmp_path / 'role')
        assert result['status'] == ('partial' if published else 'stopped')
    assert len(sent) == count and execution.budget_used('model') == 8 + count
    assert len(terminal[0].store.list_events(project_id=execution.job['project_id'])) == published
    unchanged_history(terminal)


@pytest.mark.parametrize('change', ['pause', 'dismiss', 'withdraw', 'lease', 'window', 'other_job'])
def test_changed_inputs_or_lease_stop_before_send(terminal, change):
    ex = start(terminal)
    with closing(ex.store._connect()) as db, db:
        if change == 'pause':
            db.execute("UPDATE studio_projects SET status='paused' WHERE project_id=?", (ex.job['project_id'],))
        elif change == 'dismiss':
            db.execute("UPDATE studio_items SET status='dismissed'")
        elif change == 'withdraw':
            db.execute('INSERT INTO studio_evidence_lifecycle VALUES (?,?,?,?,?)',
                       (ex.job['project_id'], ex.frozen['material']['evidence_version_id'], 'withdrawn', 'synthetic', 'test'))
        elif change == 'lease':
            db.execute("UPDATE studio_jobs SET lease_token='replaced' WHERE id=?", (ex.job['id'],))
        elif change == 'window':
            ex.deadline = '2000-01-01T00:00:00+00:00'
    if change == 'other_job':
        ex.store.enqueue(ex.job['project_id'], 'scope')
    with pytest.raises((ValueError, RuntimeError, HttpError)):
        ex.call('model', 'must-not-send', lambda: pytest.fail('network invoked'))
    assert ex.budget_used('model') == 8


def test_plan_change_repeated_start_source_denial_and_expiry(terminal):
    pipeline, frozen, _, _ = terminal
    bad = {**frozen, 'used_model': 0}
    with pytest.raises(ValueError, match='baseline'):
        once.begin(pipeline.store, bad, deadline=(datetime.now(UTC) + timedelta(minutes=2)).isoformat(), grant_id='bad')
    with pytest.raises(ValueError, match='finite'):
        once.begin(pipeline.store, frozen, deadline='2000-01-01T00:00:00+00:00', grant_id='expired')
    ex = start(terminal)
    with pytest.raises(ValueError):
        start(terminal)
    for kind in ('search', 'page', 'open_data', 'site'):
        with pytest.raises(HttpError, match='continuation_source_denied'):
            ex.call(kind, 'forbidden', lambda: pytest.fail('source invoked'))
    once.finish(ex, {'status': 'stopped', 'reason': 'synthetic'})
    unchanged_history(terminal)


def test_original_lower_cap_and_completed_call_ledger_are_not_reset(terminal):
    ex = start(terminal)
    with closing(ex.store._connect()) as db:
        task = dict(db.execute('SELECT * FROM studio_evidence_tasks WHERE id=?',
                               (ex.frozen['material']['task_id'],)).fetchone())
    material = once.OnceMaterial(ex, task)
    sent = []
    def transport():
        sent.append(True)
        return Response(200, {}, b'{}')
    material.call('model', 'extraction', transport)
    # Exact completed replay is free, but a new second extraction is forbidden.
    material.call('model', 'extraction', transport)
    with pytest.raises(HttpError, match='request_budget'):
        material.call('model', 'second-extraction', transport)
    ex.call('model', 'review', transport)
    with pytest.raises(HttpError, match='request_budget'):
        ex.call('model', 'third', transport)
    assert len(sent) == 2 and ex.budget_used('model') == 10
    once.finish(ex, {'status': 'stopped', 'reason': 'synthetic'})
    unchanged_history(terminal)


def test_plan_cannot_start_after_old_unknown_or_lower_cap(terminal):
    pipeline, frozen, _, _ = terminal
    with closing(pipeline.store._connect()) as db, db:
        db.execute("UPDATE studio_job_checkpoints SET data=json_set(data,'$.model',9) WHERE stage='material_round_caps_v1'")
    with pytest.raises(ValueError, match='two original'):
        once.plan(pipeline.store, frozen['job_id'], frozen['material']['task_id'])
    with closing(pipeline.store._connect()) as db, db:
        db.execute("UPDATE studio_job_calls SET status='unknown' WHERE call_key=(SELECT call_key FROM studio_job_calls LIMIT 1)")
    with pytest.raises(HttpError, match='request_outcome_unknown'):
        once.plan(pipeline.store, frozen['job_id'], frozen['material']['task_id'])


@pytest.mark.skipif(not os.environ.get('PW_DSH_TEST_RUNTIME'), reason='requires pinned official DSH')
def test_native_dsh_single_material_and_real_review_protocol(terminal, monkeypatch, tmp_path):
    sent = provider(terminal, monkeypatch, native=True)
    execution = start(terminal)
    result = once.run(terminal[0], execution, Path(os.environ['PW_DSH_TEST_RUNTIME']), tmp_path / 'native')
    assert result['reason'] == 'fixed_material_published' and len(sent) == 2
    unchanged_history(terminal)
