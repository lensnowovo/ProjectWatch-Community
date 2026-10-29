"""Oversized material requests stay pending; smaller evidence can still publish."""
import copy
import json
import os
from contextlib import closing
from pathlib import Path

import pytest
import test_collection_host as fixtures
from test_dsh_extraction import role_inputs

from project_watch.studio import (
    collection_context,
    dsh_extraction,
    evidence_tasks,
    progressive_bootstrap,
)
from project_watch.studio.http import Http, HttpError, Response


@pytest.fixture
def setup(tmp_path):
    return fixtures.setup.__wrapped__(tmp_path)


def test_full_body_not_just_input_is_checked_before_dispatch(setup):
    parent, settings, http, _ = role_inputs(setup)
    role = dsh_extraction.Extraction(parent, {'task_id': 'a' * 64, 'refs': {}})
    gateway = dsh_extraction.ExtractionGateway(role, settings.model, http)
    payload = dict(model=settings.model.model, messages=[{'role': 'user', 'content': 'x' * 30000}],
                   tools=[{'function': {'name': role.tools[0], 'description': 'schema' * 1000}}])
    original = copy.deepcopy(payload)
    assert len(collection_context.wire(payload['messages'])) < 32768
    assert gateway.model_call(payload)['choices'][0]['finish_reason'] == 'stop'
    assert payload == original and not parent.execution.usage()
    receipt = parent.execution.read_checkpoint(role.deferred_stage)
    assert receipt['body_bytes'] > 32768 and receipt['dispatch'] == 'not_sent'
    assert parent.execution.read_checkpoint(role.attempt_stage) is None
    assert parent.execution.read_checkpoint(role.submission_stage) is None
    assert parent.execution.read_checkpoint('dsh_hold') is None
    with pytest.raises(evidence_tasks.MaterialDeferred):
        dsh_extraction.extract(parent, {'task_id': 'a' * 64, 'refs': {}}, settings.model, http, Path('.'), Path('.'))


def test_completed_old_large_wire_replays_but_unknown_still_blocks(setup, monkeypatch):
    parent, settings, http, _ = role_inputs(setup)
    payload = dict(model=settings.model.model, messages=[{'role': 'user', 'content': 'x' * 33000}],
                   tools=[{'function': {'name': dsh_extraction.TOOL}}])
    sends = []
    parent.pipeline.http_factory = lambda _: Http(lambda *args: (
        sends.append(args[3]) or Response(200, {}, b'{"choices":[{"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}]}')))
    _, http, _, _ = parent.pipeline.tools(execution=parent.execution)
    source = {'task_id': 'b' * 64, 'refs': {}}
    # Synthesize a completed pre-boundary receipt, not a real request.
    monkeypatch.setattr(collection_context, 'MAX_WIRE_BYTES', 65536)
    dsh_extraction.ExtractionGateway(dsh_extraction.Extraction(parent, source), settings.model, http).model_call(payload)
    monkeypatch.setattr(collection_context, 'MAX_WIRE_BYTES', 32768)
    dsh_extraction.ExtractionGateway(dsh_extraction.Extraction(parent, source), settings.model, http).model_call(payload)
    assert len(sends) == 1 and parent.execution.usage()['model']['cache_hits'] == 1
    parent.hold('request_outcome_unknown')
    with pytest.raises(HttpError, match='request_outcome_unknown'):
        dsh_extraction.ExtractionGateway(dsh_extraction.Extraction(parent, source), settings.model, http).model_call(payload)


@pytest.mark.skipif(not os.environ.get('PW_DSH_TEST_RUNTIME'), reason='requires pinned official DSH')
def test_native_oversize_keeps_full_material_and_next_material_completes(setup, tmp_path):
    from test_collection_ingest import trial
    from test_collection_plan import candidate, plan

    from project_watch.studio.collection_ingest import CollectionIngest

    pipeline, execution = setup
    parent, settings, http, _ = role_inputs(setup)
    unit = plan([candidate()], project_id=execution.job['project_id'], round_id=execution.job['id'])['units'][0]
    other = trial(url='https://example.org/second')
    other['extra']['source_identity'] = 'clinical:NCT00000002'
    CollectionIngest(execution)(unit, [other])
    selected, sends = {}, []
    with closing(pipeline.store._connect()) as db:
        before = [tuple(r) for r in db.execute('SELECT * FROM studio_evidence_versions ORDER BY rowid')]
    def send(_method, _url, _headers, raw, _timeout):
        assert len(raw) <= 32768
        sends.append(raw)
        proposal = {'observations': [{'text': 'Synthetic assertion', 'refs': [next(iter(selected['source']['refs']))]}]}
        message = dict(role='assistant', content=None, tool_calls=[dict(id='candidate', type='function',
            function=dict(name=dsh_extraction.TOOL, arguments=json.dumps(proposal)))])
        return Response(200, {}, json.dumps({'choices': [{'message': message, 'finish_reason': 'tool_calls'}]}).encode())
    pipeline.http_factory = lambda _: Http(send)
    count = 0
    def extract(source, material):
        nonlocal count
        count += 1
        selected['source'] = source
        # Extra context alone needn't exceed the limit: native system and tool
        # overhead are included at the real gateway boundary.
        source = {**source, **({'context': '中' * 12000} if count == 1 else {})}
        return dsh_extraction.extract(parent, source, settings.model, http,
            Path(os.environ['PW_DSH_TEST_RUNTIME']), tmp_path / 'roles', execution=material)
    assert evidence_tasks.run_pending(execution, extract) == 2
    with closing(pipeline.store._connect()) as db:
        tasks = [dict(r) for r in db.execute('SELECT * FROM studio_evidence_tasks ORDER BY rowid')]
        assert [t['status'] for t in tasks] == ['pending', 'complete']
        assert json.loads(tasks[0]['result'])['code'] == 'material_context_limit'
        assert before == [tuple(r) for r in db.execute('SELECT * FROM studio_evidence_versions ORDER BY rowid')]
    assert len(sends) == execution.usage()['model']['completed'] == 1
    assert evidence_tasks.run_pending(execution, lambda *_: pytest.fail('must not loop')) == 0
    assert execution.read_checkpoint('dsh_hold') is None


def test_deferred_material_does_not_starve_publication_or_same_job_replay(setup, monkeypatch):
    from test_progressive_bootstrap import install_provider, run

    calls, _roles = install_provider(setup, monkeypatch)
    native = dsh_extraction.dsh_runtime.run_dsh
    def turn(role, gateway, *args, **kwargs):
        if isinstance(role, dsh_extraction.Extraction) and 'Synthetic paper 1' in role.task:
            gateway.model_call(dict(model=gateway.model.model,
                messages=[dict(role='system', content='x' * 33000), dict(role='user', content=role.task)],
                tools=[{'function': {'name': 'pw_submit_records'}}]))
            return role.state['finished']
        return native(role, gateway, *args, **kwargs)
    monkeypatch.setattr(dsh_extraction.dsh_runtime, 'run_dsh', turn)
    result = run(setup)
    pipeline, execution = setup
    assert result['status'] == 'partial' and result['records'] == 2
    with closing(pipeline.store._connect()) as db:
        tasks = [dict(r) for r in db.execute('SELECT * FROM studio_evidence_tasks ORDER BY rowid')]
    assert [t['status'] for t in tasks] == ['pending', 'complete', 'complete']
    closeout = execution.read_checkpoint(progressive_bootstrap.CLOSEOUT)
    assert closeout['reason'] == 'material_context_limit'
    assert closeout['context_deferred_task_ids'] == [tasks[0]['id']]
    assert len(pipeline.store.list_events(project_id=execution.job['project_id'])) == 2
    assert len(calls) == 3  # Two materials and one semantic review; oversize sent zero.
    assert run(setup) == result and len(calls) == 3


def test_all_materials_deferred_finish_partial_without_fake_proposals(setup, monkeypatch):
    from test_progressive_bootstrap import install_provider, run

    calls, _ = install_provider(setup, monkeypatch)
    native = dsh_extraction.dsh_runtime.run_dsh
    def turn(role, gateway, *args, **kwargs):
        if isinstance(role, dsh_extraction.Extraction):
            gateway.model_call(dict(model=gateway.model.model,
                messages=[dict(role='user', content=role.task + 'x' * 33000)],
                tools=[{'function': {'name': 'pw_submit_records'}}]))
            return role.state['finished']
        return native(role, gateway, *args, **kwargs)
    monkeypatch.setattr(dsh_extraction.dsh_runtime, 'run_dsh', turn)
    result = run(setup)
    pipeline, execution = setup
    assert result['status'] == 'partial' and result['records'] == 0
    assert not calls
    with closing(pipeline.store._connect()) as db:
        assert {r[0] for r in db.execute('SELECT status FROM studio_evidence_tasks')} == {'pending'}
        assert not db.execute("SELECT 1 FROM studio_job_checkpoints WHERE stage LIKE 'extract_result:%'").fetchone()
    assert len(execution.read_checkpoint(progressive_bootstrap.CLOSEOUT)['context_deferred_task_ids']) == 3
    assert run(setup) == result and not calls


def test_explicit_transfer_keeps_deferral_instead_of_retrying(tmp_path):
    import test_collection_view as saved
    from test_evidence_tasks import rows
    from test_material_ownership import next_owner

    from project_watch.studio.material_ownership import take_over

    owner = saved.owner.__wrapped__(tmp_path)
    previous, _, _, _ = saved.save(owner)
    task = rows(owner)[0]
    receipt = dict(code='material_context_limit', dispatch='not_sent', task_id=task['id'], body_bytes=40000)
    previous.checkpoint(evidence_tasks.DEFERRED + task['id'], receipt)
    current = next_owner(previous)
    take_over(current, task['id'])
    assert current.read_checkpoint(evidence_tasks.DEFERRED + task['id']) == receipt
    assert evidence_tasks.run_pending(current, lambda *_: pytest.fail('same evidence must not be retried')) == 0
    assert rows(owner)[0]['status'] == 'pending' and not current.usage()
