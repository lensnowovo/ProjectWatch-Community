"""Settled truncated output preserves evidence and never auto-retries."""
import copy
import json
import os
from contextlib import closing
from pathlib import Path

import pytest
import test_collection_host as fixtures
from test_dsh_extraction import role_inputs

from project_watch.studio import dsh_extraction, evidence_tasks, progressive_bootstrap
from project_watch.studio.http import Http, HttpError, Response


@pytest.fixture
def setup(tmp_path):
    return fixtures.setup.__wrapped__(tmp_path)


def truncated(tool=False):
    message = dict(role='assistant', content=None, reasoning_content='Synthetic reasoning')
    if tool:
        message['tool_calls'] = [dict(id='cut', type='function', function=dict(
            name='pw_submit_records', arguments='{"observations":['))]
    return {'choices': [{'message': message, 'finish_reason': 'length'}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 4000}}


@pytest.mark.parametrize('configured,expected', [(2000, 2000), (8000, 8000), (16000, 8000)])
def test_allowance_honors_lower_config_and_stays_bounded(setup, configured, expected):
    parent, settings, _, _ = role_inputs(setup)
    settings.model.max_output_tokens = configured
    sent = []
    parent.pipeline.http_factory = lambda _: Http(lambda *args: (
        sent.append(json.loads(args[3])) or Response(200, {}, json.dumps(truncated()).encode())))
    _, http, _, _ = parent.pipeline.tools(execution=parent.execution)
    role = dsh_extraction.Extraction(parent, {'task_id': 'a' * 64, 'refs': {}})
    result = dsh_extraction.ExtractionGateway(role, settings.model, http).model_call(dict(
        model=settings.model.model, messages=[], tools=[{'function': {'name': role.tools[0]}}]))
    assert sent[0]['max_tokens'] == expected and len(sent) == 1
    assert result['choices'][0]['finish_reason'] == 'stop'
    receipt = parent.execution.read_checkpoint(role.deferred_stage)
    assert receipt['code'] == 'material_output_limit' and receipt['dispatch'] == 'completed'
    assert parent.execution.read_checkpoint(role.submission_stage) is None
    assert parent.execution.usage()['model']['completed'] == 1
    with pytest.raises(evidence_tasks.MaterialDeferred):
        dsh_extraction.extract(parent, {'task_id': 'a' * 64, 'refs': {}}, settings.model, http, Path('.'), Path('.'))
    assert len(sent) == 1


def test_old_4000_wire_replays_without_spending_new_allowance(setup):
    parent, settings, _, _ = role_inputs(setup)
    sent = []
    parent.pipeline.http_factory = lambda _: Http(lambda *args: (
        sent.append(json.loads(args[3])) or Response(200, {}, json.dumps(truncated()).encode())))
    _, http, _, _ = parent.pipeline.tools(execution=parent.execution)
    source = {'task_id': 'b' * 64, 'refs': {}}
    role = dsh_extraction.Extraction(parent, source)
    settings.model.max_output_tokens = 8000
    payload = dict(model=settings.model.model, messages=[], tools=[{'function': {'name': role.tools[0]}}])
    gateway = dsh_extraction.ExtractionGateway(role, settings.model, http)
    gateway._send_model({**payload, 'stream': False, 'max_tokens': 4000})
    gateway.model_call(payload)
    assert len(sent) == 1 and sent[0]['max_tokens'] == 4000
    assert parent.execution.usage()['model']['cache_hits'] == 1
    parent.hold('request_outcome_unknown')
    with pytest.raises(HttpError, match='request_outcome_unknown'):
        dsh_extraction.ExtractionGateway(dsh_extraction.Extraction(parent, source), settings.model, http).model_call(payload)
    assert len(sent) == 1


@pytest.mark.skipif(not os.environ.get('PW_DSH_TEST_RUNTIME'), reason='requires pinned official DSH')
@pytest.mark.parametrize('tool', [False, True])
def test_native_truncation_stops_batch_and_keeps_original_material(setup, tmp_path, tool):
    from test_collection_ingest import trial
    from test_collection_plan import candidate, plan

    from project_watch.studio.collection_ingest import CollectionIngest

    pipeline, execution = setup
    parent, settings, _, _ = role_inputs(setup)
    unit = plan([candidate()], project_id=execution.job['project_id'], round_id=execution.job['id'])['units'][0]
    other = trial(url='https://example.org/second')
    other['extra']['source_identity'] = 'clinical:NCT00000002'
    CollectionIngest(execution)(unit, [other])
    sent = []
    pipeline.http_factory = lambda _: Http(lambda *args: (
        sent.append(args[3]) or Response(200, {}, json.dumps(truncated(tool)).encode())))
    _, http, _, _ = pipeline.tools(execution=execution)
    with closing(pipeline.store._connect()) as db:
        before = [tuple(r) for r in db.execute('SELECT * FROM studio_evidence_versions ORDER BY rowid')]
    def extract(source, material):
        return dsh_extraction.extract(parent, source, settings.model, http,
            Path(os.environ['PW_DSH_TEST_RUNTIME']), tmp_path / 'roles', execution=material)
    assert evidence_tasks.run_pending(execution, extract) == 1
    assert len(sent) == 1 and execution.usage()['model']['completed'] == 1
    assert evidence_tasks.run_pending(execution, lambda *_: pytest.fail('no automatic next material')) == 0
    with closing(pipeline.store._connect()) as db:
        tasks = [dict(r) for r in db.execute('SELECT * FROM studio_evidence_tasks ORDER BY rowid')]
        assert [t['status'] for t in tasks] == ['pending', 'pending']
        assert json.loads(tasks[0]['result'])['code'] == 'material_output_limit'
        assert before == [tuple(r) for r in db.execute('SELECT * FROM studio_evidence_versions ORDER BY rowid')]
        assert not db.execute("SELECT 1 FROM studio_job_checkpoints WHERE stage LIKE 'extract_result:%'").fetchone()


@pytest.mark.parametrize('first', [True, False])
@pytest.mark.parametrize('isolated', [True, False])
def test_pipeline_partial_and_replay_no_calls(setup, monkeypatch, first, isolated):
    from test_progressive_bootstrap import install_provider, run

    install_provider(setup, monkeypatch, all_valid=True, bounded=True)
    pipeline, execution = setup
    if not isolated:
        material_run = progressive_bootstrap.run
        def legacy_run(research, *args, **kwargs):
            # Freeze only after normal collection/material ownership admission.
            if research.execution.read_checkpoint('material_local_limits_v1') is None:
                research.execution.checkpoint('material_local_limits_v1', {})
            return material_run(research, *args, **kwargs)
        monkeypatch.setattr(progressive_bootstrap, 'run', legacy_run)
    original_factory = pipeline.http_factory
    calls = []
    def factory(settings):
        http = original_factory(settings)
        original = http.transport
        def send(method, url, headers, raw, timeout):
            body = json.loads(raw) if raw else {}
            if (any(t['function']['name'] == 'pw_submit_records' for t in body.get('tools', []))
                    and ('Synthetic paper 1' if first else 'Synthetic paper 2') in json.dumps(body)):
                calls.append(copy.deepcopy(body))
                return Response(200, {}, json.dumps(truncated()).encode())
            return original(method, url, headers, raw, timeout)
        http.transport = send
        return http
    pipeline.http_factory = factory
    # The native path is covered above; expose the gateway's deferred outcome.
    native = dsh_extraction.dsh_runtime.run_dsh
    def turn(role, gateway, *args, **kwargs):
        try:
            return native(role, gateway, *args, **kwargs)
        except HttpError as exc:
            if exc.code == 'dsh_no_valid_submission' and (role.state['finished'] or {}).get('deferred'):
                return role.state['finished']
            raise
    monkeypatch.setattr(dsh_extraction.dsh_runtime, 'run_dsh', turn)
    result = run(setup)
    expected = 2 if isolated else (0 if first else 1)
    assert result['status'] == 'partial' and result['records'] == expected
    assert execution.read_checkpoint(progressive_bootstrap.CLOSEOUT)['reason'] == 'material_output_limit'
    # Papers 1 and 3 propose the same competitor event; existing event dedup
    # merges them, while the two independent material receipts both remain.
    event_count = (2 if first else 1) if isolated else expected
    assert len(pipeline.store.list_events(project_id=execution.job['project_id'])) == event_count
    with closing(pipeline.store._connect()) as db:
        tasks = [dict(t) for t in db.execute('SELECT status,result FROM studio_evidence_tasks ORDER BY rowid')]
        assert sum(t['status'] == 'complete' for t in tasks) == expected
        assert sum(json.loads(t['result']).get('code') == 'material_output_limit' for t in tasks) == 1
    assert run(setup) == result and len(calls) == 1
