"""M3B: a failed T1 comparison must not masquerade as a completed update."""

import json
import runpy
import socket
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from project_watch.studio.http import HttpError
from project_watch.studio.llm import ModelError

SUPPORT = runpy.run_path(str(Path(__file__).with_name('distribution_journey_support.py')))
CORE = runpy.run_path(str(Path(__file__).with_name('core_without_insight_support.py')))


@pytest.mark.parametrize('code', [
    'request_outcome_unknown', 'model_timeout', 'request_budget', 'provider_backoff',
    'model_content_rejected', 'model_access_denied', 'model_output_invalid_shape',
])
def test_comparison_failure_is_not_an_empty_success(tmp_path, code):
    app, _ = SUPPORT['make_app'](tmp_path)
    pipeline = app.state.studio_pipeline
    project = app.state.intake_worker.repository.create_project('合成变化项目')

    class FailedModel:
        def json(self, *_args, **_kwargs):
            raise ModelError('本轮变化对照未完成', code=code)

    # No fake DB repair or model retry: this is the real comparison method.
    with pytest.raises(ModelError) as caught:
        pipeline._record_changes(
            project.id, FailedModel(),
            {'F1': {'source_id': 'synthetic-new', 'source_name': 'synthetic-t1.txt', 'locator': 'line 1'}},
            {'synthetic-new'}, {'summary': '合成旧材料'},
            [{'text': '合成新材料出现变化', 'ref': 'F1'}],
        )
    assert caught.value.code == code


def test_uncertain_new_material_comparison_survives_restart_without_resend(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError('M3B synthetic acceptance must not use the network')

    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(socket, 'getaddrinfo', forbidden)
    home = tmp_path / 'data'
    app, transport = SUPPORT['make_app'](home)
    with TestClient(app) as client:
        SUPPORT['configure'](client)
        receipt = CORE['bp_incremental_history'](client, app, transport)
        pid = receipt['project_id']
        route = '/api/studio/projects/' + pid
        store, service = app.state.studio_store, app.state.studio_application
        pipeline = app.state.studio_pipeline
        material_watch = pipeline.monitor.add_watchpoint(pid, '交付进展', '交付', ['交付'], '交付', False)
        previous = client.get(route).json()
        previous_profile = {k: v for k, v in store.get_profile(pid).items() if k != 'dependency'}
        calls = []
        original = transport.model_result

        def fail_comparison(system, user):
            if '新一批内部资料' in system:
                calls.append('comparison')
                raise HttpError('connection_failed')
            return original(system, user)

        transport.model_result = fail_comparison
        response = client.post(route + '/files', files={
            'file': ('Synthetic-T2.txt',
                     '合成光学科技有限公司2026年营业收入1200万元。补充材料：交付计划调整。'.encode(), 'text/plain')})
        assert response.status_code == 202, response.text
        parsed = app.state.intake_worker.process_one()
        assert parsed.status == 'completed'
        version = service.project(pid)['config_version']
        authorized = client.post(route + '/sources/' + parsed.source_id + '/authorize-analysis',
                                 json={'expected_version': version, 'idempotency_key': 'synthetic-t2'})
        assert authorized.status_code == 200, authorized.text
        CORE['drain'](app)
        failed = [j for j in store.jobs(pid) if j['kind'] == 'internal' and j['status'] == 'failed']
        assert len(failed) == 1, store.jobs(pid)
        assert len(calls) == 1
        assert {k: v for k, v in store.get_profile(pid).items() if k != 'dependency'} == previous_profile
        assert parsed.source_id not in store.analyzed_source_ids(pid)
        assert pipeline.questions.new_materials(pid, material_watch['id']) == []
        detail = client.get(route).json()
        assert detail['project']['last_internal_at'] == previous['project']['last_internal_at']
        assert detail['doc_changes'] == previous['doc_changes']
        assert {m['source_id'] for m in detail['metrics']} >= set(receipt['source_ids'])
        history = client.get(route + '/questions/' + receipt['watch_id'] + '/history').json()
        assert sorted(r['id'] for r in history) == receipt['history_ids']
        CORE['drain'](app)
        assert len(calls) == 1
        failed_job_id = failed[0]['id']

    reopened, later_transport = SUPPORT['make_app'](home)
    from project_watch.studio.question_advisor import _ASSESS
    normal_after_reopen = later_transport.model_result
    def assess_material(system, user):
        if system.startswith(_ASSESS):
            return {'results': [{'id': q['id'], 'status': 'unchanged', 'note': '资料记录交付计划调整',
                    'material_ids': [m['id'] for m in q['materials']]} for q in json.loads(user)['questions']]}
        return normal_after_reopen(system, user)
    later_transport.model_result = assess_material
    with TestClient(reopened) as client:
        CORE['drain'](reopened)
        assert later_transport.calls == []
        job = next(j for j in reopened.state.studio_store.jobs(pid) if j['id'] == failed_job_id)
        assert job['status'] == 'failed'
        preview = client.get(route + '/jobs/' + failed_job_id + '/recovery')
        assert preview.status_code == 200 and preview.json()['can_recover'], preview.text
        # Viewing recovery is still stored-only, not permission to send again.
        assert later_transport.calls == []
        CORE['recover_after_restart'](client, reopened, {'project_id': pid, 'job_id': failed_job_id})
        assert parsed.source_id in reopened.state.studio_store.analyzed_source_ids(pid)
        question = next(q for q in reopened.state.studio_pipeline.questions.view(pid) if q['id'] == material_watch['id'])
        [material_hit] = question['materials']
        assert material_hit['source_id'] == parsed.source_id
        assert material_hit['status'] == 'assessed'
        assert question['assessment_state']['pending_materials'] == 0
        assert question['latest']['system']['material_ids'] == [material_hit['id']]
        changes = client.get(route).json()['doc_changes']
        assert sum('Synthetic-T2.txt' in c['documents'] for c in changes) == 1
        history = client.get(route + '/questions/' + receipt['watch_id'] + '/history').json()
        assert sorted(r['id'] for r in history) == receipt['history_ids']
        # Extraction/profile replay. Comparison, question suggestion/assessment
        # and newly queued brief are the four authorized synthetic HTTP requests.
        assert sum(c['host'] == 'open.bigmodel.cn' for c in later_transport.calls) == 4
        before = list(later_transport.calls)
        CORE['drain'](reopened)
        assert later_transport.calls == before
