"""The default update entry analyzes raw saved sources before event admission."""
import json
from contextlib import closing

from fastapi.testclient import TestClient
from test_event_insight import install

from project_watch.studio import insight, snapshot_work, update_run
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import Http, Response
from project_watch.studio.pipeline import StudioWorker


def setup(tmp_path, monkeypatch):
    pipeline, seed, seen = install(tmp_path, monkeypatch, monitor=False, user_entry=True)
    monkeypatch.setenv('PROJECT_WATCH_DSH_MONITOR', '1')
    original = pipeline.http_factory(None).transport
    order = []
    def send(method, url, headers, body, timeout):
        value = json.loads(body) if body and 'model.example.invalid' in url else {}
        tools = {t['function']['name'] for t in value.get('tools', [])}
        if tools:
            order.append(sorted(tools))
        if value and not tools:
            payload = json.loads(value['messages'][-1]['content'])
            if 'materials' in payload and 'snapshot_id' in payload:
                order.append('saved-analysis')
                point = dict(text='合成公司正在开展产品研究。', refs=[next(iter(payload['materials']))])
                ring = dict(verdict='unclear', confidence='low', summary=point['text'], **{'for': [point], 'against': []})
                response = dict(headline='', operations=ring, chain=ring, position=ring,
                    industry=dict(verdict='unclear', summary=point['text'], drivers=[point], headwinds=[]),
                    technology=dict(maturity='unclear', consensus='unclear', summary=point['text'], academic=[point], industry=[], risks=[]))
            elif ('fields' in payload and 'analysis' in payload and 'target_object' not in payload
                  and payload.get('purpose') != 'public_record_admission'):
                if 'conflicts' in value['messages'][0]['content']:
                    response = dict(consistent=True, reason='synthetic coherent', conflicts=[])
                else:
                    ref, source = next(iter(payload['evidence'].items()))
                    response = dict(checks=[dict(field=key, status='supported', reason='synthetic',
                        evidence=[dict(ref=ref, quote=source['context'])]) for key in payload['fields']])
            else:
                return original(method, url, headers, body, timeout)
            return Response(200, {}, json.dumps(dict(choices=[dict(message=dict(content=json.dumps(response)))],
                usage=dict(prompt_tokens=10, completion_tokens=5))).encode())
        return original(method, url, headers, body, timeout)
    pipeline.http_factory = lambda _: Http(send)
    worker = StudioWorker(pipeline)
    worker.schedule = lambda: None
    return pipeline, seed, order, worker, seen


def test_default_owner_first_and_increment_publish_sections_without_second_analysis(tmp_path, monkeypatch):
    pipeline, seed, order, worker, seen = setup(tmp_path, monkeypatch)
    pid = seed.job['project_id']
    client = TestClient(pipeline._test_app)
    for first in (True, False):
        seen['baseline_events'] = pipeline.store.list_events(project_id=pid)
        seen['baseline_watches'] = pipeline.monitoring_plan.items(pid)
        seen['baseline_material_count'] = len(seen['materials'])
        jid = seed.job['id'] if first else client.post(f'/api/studio/projects/{pid}/refresh', json={'kind': 'external'}).json()['id']
        assert worker.step()
        with closing(pipeline.store._connect()) as db:
            job = db.execute('SELECT status,error FROM studio_jobs WHERE id=?', (jid,)).fetchone()
            assert job['status'] == 'done', job['error']
            assert not db.execute("SELECT 1 FROM studio_jobs WHERE project_id=? AND kind='insight'", (pid,)).fetchone()
        execution = JobExecution(pipeline.store, {'id': jid})
        assert execution.read_checkpoint(update_run.OWNER)['protocol'] == 2
        state = execution.read_checkpoint(update_run.STATE)
        assert list(state['phases']) == ['sources', 'analysis', 'events', 'reports']
        assert execution.read_checkpoint(snapshot_work.PUBLICATION)['status'] == 'completed'
        view = client.get(f'/api/studio/projects/{pid}/insight')
        assert view.status_code == 200 and view.json()['saved_analysis']['can_analyze']
        assert len(view.json()['record']['section_versions']) == 5
        assert insight.load(pipeline.store, pid)['analysis']['technology']['summary']
    assert 'saved-analysis' in order
    assert order.index('saved-analysis') < next(i for i, value in enumerate(order) if 'pw_submit_records' in value)
    assert not any('pw_judge' in value for value in order)
    reply = client.post(f'/api/studio/projects/{pid}/refresh', json={'kind': 'insight'})
    assert reply.status_code == 202
    assert reply.json()['input_version'].startswith('snapshot:')
    frozen = JobExecution(pipeline.store, reply.json()).read_checkpoint(snapshot_work.INPUT)
    assert frozen['snapshot_id'] == view.json()['saved_analysis']['snapshot_id']


def test_malformed_analysis_keeps_events_then_saved_retry_never_collects(tmp_path, monkeypatch):
    pipeline, seed, _, worker, seen = setup(tmp_path, monkeypatch)
    pid = seed.job['project_id']
    original = pipeline.http_factory(None).transport
    invalid = [True]
    def transport(method, url, headers, body, timeout):
        value = json.loads(body) if body and 'model.example.invalid' in url else {}
        if value and not value.get('tools'):
            payload = json.loads(value['messages'][-1]['content'])
            if 'snapshot_id' in payload and 'materials' in payload and invalid[0]:
                invalid[0] = False
                return Response(200, {}, json.dumps({'choices': [{'message': {'content': '[]'}}],
                    'usage': {'prompt_tokens': 10, 'completion_tokens': 5}}).encode())
        return original(method, url, headers, body, timeout)
    pipeline.http_factory = lambda _: Http(transport)
    assert worker.step()
    root = JobExecution(pipeline.store, {'id': seed.job['id']})
    assert root.read_checkpoint(update_run.STATE)['phases']['analysis']['status'] == 'partial'
    assert pipeline.store.list_events(project_id=pid)
    client = TestClient(pipeline._test_app)
    saved = client.get(f'/api/studio/projects/{pid}/insight').json()['saved_analysis']
    assert saved['can_analyze']
    assert saved['status'] == 'partial'
    from project_watch.studio import material_snapshot
    before = material_snapshot.read(pipeline.store, pid, saved['snapshot_id'])
    counts = {key: len(seen[key]) for key in ('pages', 'materials', 'clinical')}
    reply = client.post(f'/api/studio/projects/{pid}/saved-analysis', json={
        'snapshot_id': saved['snapshot_id'], 'request_id': 'explicit-new-analysis',
        'parent_attempt_id': saved['parent_attempt_id']})
    assert reply.status_code == 200
    assert worker.step()
    retry = JobExecution(pipeline.store, reply.json())
    assert retry.read_checkpoint(snapshot_work.PUBLICATION)['status'] == 'completed'
    assert material_snapshot.read(pipeline.store, pid, saved['snapshot_id']) == before
    assert counts == {key: len(seen[key]) for key in counts}
    with closing(pipeline.store._connect()) as db:
        assert not db.execute("SELECT 1 FROM studio_job_calls WHERE job_id=? AND kind!='model'", (retry.job['id'],)).fetchone()


def test_saved_analysis_inherits_only_proven_unrelated_legacy_unknown_exclusion(tmp_path, monkeypatch):
    from test_monitor_completion_journey import call_rows, old_hold

    pipeline, seed, _, worker, _ = setup(tmp_path, monkeypatch)
    pid = seed.job['project_id']
    claimed = pipeline.store.claim()
    assert claimed['id'] == seed.job['id']
    assert pipeline.store.finish(seed.job['id'], lease_token=claimed['lease_token'])
    old = old_hold(pipeline.store, pid)
    old_calls = call_rows(pipeline.store, old)
    client = TestClient(pipeline._test_app)
    root = client.post(f'/api/studio/projects/{pid}/refresh', json={'kind': 'external'})
    assert root.status_code == 202
    assert worker.step()
    saved = client.get(f'/api/studio/projects/{pid}/insight').json()['saved_analysis']
    assert saved['can_analyze']
    reply = client.post(f'/api/studio/projects/{pid}/saved-analysis', json={
        'snapshot_id': saved['snapshot_id'], 'request_id': 'independent-analysis',
        'parent_attempt_id': saved['parent_attempt_id']})
    assert reply.status_code == 200
    assert worker.step()
    retry = JobExecution(pipeline.store, reply.json())
    assert retry.read_checkpoint(snapshot_work.PUBLICATION)['status'] == 'completed'
    assert old_calls == call_rows(pipeline.store, old)
    with closing(pipeline.store._connect()) as db:
        assert not db.execute('SELECT 1 FROM studio_job_budget_links WHERE job_id=? AND origin_job_id=?',
                              (retry.job['id'], old)).fetchone()


def test_partial_publication_retry_targets_only_failed_section(tmp_path, monkeypatch):
    pipeline, seed, _, worker, seen = setup(tmp_path, monkeypatch)
    pid = seed.job['project_id']
    original = pipeline.http_factory(None).transport
    requests = []
    def transport(method, url, headers, body, timeout):
        result = original(method, url, headers, body, timeout)
        value = json.loads(body) if body and 'model.example.invalid' in url else {}
        payload = json.loads(value['messages'][-1]['content']) if value and not value.get('tools') else {}
        if 'materials' in payload and 'snapshot_id' in payload:
            requests.append(payload['requested_sections'])
            if len(requests) == 1:
                reply = result.json()
                raw = json.loads(reply['choices'][0]['message']['content'])
                raw['operations']['for'][0]['refs'] = ['N404']
                reply['choices'][0]['message']['content'] = json.dumps(raw)
                return Response(200, {}, json.dumps(reply).encode())
        return result
    pipeline.http_factory = lambda _: Http(transport)
    assert worker.step()
    first = insight.load(pipeline.store, pid)
    assert len(first['section_versions']) == 4
    counts = {key: len(seen[key]) for key in ('pages', 'materials', 'clinical')}
    client = TestClient(pipeline._test_app)
    saved = client.get(f'/api/studio/projects/{pid}/insight').json()['saved_analysis']
    reply = client.post(f'/api/studio/projects/{pid}/saved-analysis', json={
        'snapshot_id': saved['snapshot_id'], 'request_id': 'repair-operations',
        'parent_attempt_id': saved['parent_attempt_id']})
    assert reply.status_code == 200
    assert worker.step()
    assert requests[1] == ['operations']
    result = JobExecution(pipeline.store, reply.json()).read_checkpoint(snapshot_work.PUBLICATION)
    assert result['status'] == 'completed' and result['published'] == ['operations']
    after = insight.load(pipeline.store, pid)
    assert len(after['section_versions']) == 5
    for name, version in first['section_versions'].items():
        assert after['section_versions'][name] == version
    assert counts == {key: len(seen[key]) for key in counts}


def test_changed_section_source_never_dispatches_legacy_analysis(tmp_path, monkeypatch):
    from project_watch.studio import event_insight_work, material_snapshot
    pipeline, seed, _, worker, _ = setup(tmp_path, monkeypatch)
    pid = seed.job['project_id']
    assert worker.step()
    snapshot = JobExecution(pipeline.store, seed.job).read_checkpoint(snapshot_work.INPUT)['snapshot_id']
    sources = material_snapshot.read(pipeline.store, pid, snapshot)['materials']
    pipeline.evidence.dependencies.withdraw(pid, sources[0]['evidence_version_id'], 'synthetic withdrawal', actor='test')
    def forbidden(*args):
        raise AssertionError('new section reached legacy analysis')
    monkeypatch.setattr(event_insight_work, 'rebuild', forbidden)
    worker.dispatch_rebuilds(model_ready=True)
    pending = pipeline.evidence.dependencies.pending(pid)
    sections = [r for r in pending if r['artifact_key'].startswith('snapshot-section:')]
    assert sections and all(r['status'] == 'awaiting_source_update' for r in sections)
