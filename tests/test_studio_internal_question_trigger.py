"""Normal internal dispatch, not manually calling the question ledger."""
import json
import socket
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from test_studio_question_materials import intake, setup

from project_watch.studio.http import HttpError
from project_watch.studio.llm import ModelError
from project_watch.studio.monitoring_contracts import LocatedRef
from project_watch.studio.questions import QuestionLedger


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('Internal question tests cannot use real networking')
    monkeypatch.setattr(socket, 'create_connection', denied)
    monkeypatch.setattr(socket, 'getaddrinfo', denied)


class Advisor:
    def __init__(self):
        self.calls = []

    def suggest(self, *args):
        return []

    def assess(self, llm, project, questions):
        self.calls.append(questions)
        return [{'id': q['id'], 'status': 'unchanged', 'note': '材料记录交付情况',
                 'material_ids': [m['id'] for m in q['materials']]} for q in questions]


def test_normal_intake_assesses_only_affected_question_and_preserves_history(tmp_path):
    app, web, p, pid, watches = setup(tmp_path)
    advisor = p.question_advisor = Advisor()
    original = p.questions.history(watches[0]['id'])
    intake(app, web, p, pid, '合成光电生产光学模组。')
    assert advisor.calls == []
    source = intake(app, web, p, pid, '合成光电交付10台光学模组。')
    assert len(advisor.calls) == 1
    assert [q['id'] for q in advisor.calls[0]] == [watches[0]['id']]
    [sent] = advisor.calls[0][0]['materials']
    assert sent['source_id'] == source
    p.evidence.resolve(pid, LocatedRef(**sent['ref']))
    current = next(q for q in p.questions.view(pid) if q['id'] == watches[0]['id'])
    assert current['assessment_state']['pending_materials'] == 0
    assert current['latest']['system']['visibility'] == 'internal'
    assert {r['id'] for r in original} <= {r['id'] for r in p.questions.history(watches[0]['id'])}
    assert p.questions.history(watches[1]['id']) == []
    reopened = QuestionLedger(p.store, p.monitor, binding_provider=p.model_binding)
    assert reopened.history(watches[0]['id']) == p.questions.history(watches[0]['id'])
    p.run_internal(p.store.enqueue(pid, 'internal'))
    assert len(advisor.calls) == 1


def test_first_material_generates_and_assesses_questions_in_the_same_intake(tmp_path):
    app, web, p, pid, _ = setup(tmp_path)
    class First(Advisor):
        def suggest(self, llm, project, plan, profile, existing):
            assert profile and profile['summary']
            return [{'text': '光学模组有什么变化', 'label': '光学模组',
                     'queries': ['合成光电 光学模组'], 'criteria': '模组产品'}]
    advisor = p.question_advisor = First()
    intake(app, web, p, pid, '合成光电生产光学模组。')
    watch = next(q for q in p.questions.view(pid) if q['origin'] == 'ai')
    assert watch['latest']['system']['material_ids']
    assert watch['assessment_state']['pending_materials'] == 0
    assert len(advisor.calls) == 1


@pytest.mark.parametrize('code', ['model_content_rejected', 'model_output_invalid_shape', 'request_outcome_unknown'])
def test_normal_intake_failure_keeps_material_pending_and_old_human_history(tmp_path, code):
    app, web, p, pid, watches = setup(tmp_path)
    class Failed(Advisor):
        def assess(self, *args):
            raise ModelError('synthetic failure', code=code)
    p.question_advisor = Failed()
    history = p.questions.history(watches[0]['id'])
    with pytest.raises(ModelError) as error:
        intake(app, web, p, pid, '合成光电交付10台光学模组。')
    assert error.value.code == code
    assert p.store.get_profile(pid)
    assert p.questions.new_materials(pid, watches[0]['id'])
    assert p.questions.history(watches[0]['id']) == history
    assert p.store.get_project(pid)['last_internal_at'] is None


@pytest.mark.parametrize('with_t0', [False, True])
def test_unknown_question_request_is_not_resent_by_external_round_or_restart(tmp_path, with_t0):
    from test_studio_question_materials import SUPPORT

    from project_watch.studio.question_advisor import _ASSESS, _SUGGEST, ModelQuestionAdvisor

    home = tmp_path / 'data'
    app, web, p, pid, watches = setup(home)
    if with_t0:
        intake(app, web, p, pid, '合成光电生产光学模组。')
        # The direct routing fixture leaves an unclaimed synthetic queue record.
        # It is not a real leased failed request; finish before the worker T1.
        for job in p.store.jobs(pid):
            if job['status'] == 'queued':
                p.store.finish(job['id'])
    p.question_advisor = ModelQuestionAdvisor()
    transport = app.state.synthetic_transport
    normal = transport.model_result
    question_calls = []
    def response(system, user):
        if system.startswith(_SUGGEST):
            return {'questions': []}
        if system.startswith(_ASSESS):
            question_calls.append(user)
            raise HttpError('connection_failed')
        return normal(system, user)
    transport.model_result = response
    source = intake(app, web, p, pid, '合成光电交付10台光学模组。', run=False)
    p.store.enqueue(pid, 'internal')
    worker = app.state.studio_worker
    worker._last_schedule = datetime.now(UTC)
    assert worker.step()
    failures = [j for j in p.store.jobs(pid) if j['kind'] == 'internal' and j['status'] == 'failed']
    assert len(failures) == 1, (p.store.jobs(pid), len(question_calls))
    [failed] = failures
    assert len(question_calls) == 1
    assert source in p.store.analyzed_source_ids(pid)
    profile = p.store.get_profile(pid)
    changes = p.store.doc_changes(pid)
    assert len(changes) == int(with_t0)
    p._question_round(pid, p.store.get_project(pid), {}, p.tools()[2], lambda _: None)
    assert len(question_calls) == 1

    reopened, later = SUPPORT['make_app'](home)
    with TestClient(reopened) as client:
        pipeline = reopened.state.studio_pipeline
        assert pipeline.questions.new_materials(pid, watches[0]['id'])
        pipeline._question_round(pid, pipeline.store.get_project(pid), {}, pipeline.tools()[2], lambda _: None)
        assert later.calls == []
        route = f'/api/studio/projects/{pid}/jobs/{failed["id"]}/recovery'
        preview = client.get(route)
        assert preview.status_code == 200 and preview.json()['can_recover'], preview.text
        assert pipeline.store.get_profile(pid) == profile
        assert pipeline.store.doc_changes(pid) == changes
        # Owner-confirmed recovery through the normal API, not a direct ledger call.
        def recovered(system, user):
            if system.startswith(_SUGGEST):
                return {'questions': []}
            if system.startswith(_ASSESS):
                questions = json.loads(user)['questions']
                return {'results': [{'id': q['id'], 'status': 'unchanged', 'note': '资料记录交付情况',
                        'material_ids': [m['id'] for m in q['materials']]} for q in questions]}
            return later.model_result_original(system, user)
        later.model_result_original = later.model_result
        later.model_result = recovered
        authorized = client.post(route, json={'expected_version': preview.json()['version'],
                  'idempotency_key': 'synthetic-question-recovery', 'accept_duplicate_cost': True})
        assert authorized.status_code == 202, authorized.text
        current_worker = reopened.state.studio_worker
        current_worker._last_schedule = datetime.now(UTC)
        assert current_worker.step()
        job = next(j for j in pipeline.store.jobs(pid) if j['id'] == failed['id'])
        assert job['status'] == 'done', job
        assert pipeline.store.get_profile(pid) == profile
        assert pipeline.store.doc_changes(pid) == changes
        assert pipeline.questions.new_materials(pid, watches[0]['id']) == []
        assert all(c['host'] == 'open.bigmodel.cn' for c in later.calls)
        assert len(later.calls) == 1  # suggestion was completed and replays


def test_no_advisor_and_unapproved_source_do_not_create_system_judgements(tmp_path):
    app, web, p, pid, watches = setup(tmp_path)
    p.question_advisor = None
    intake(app, web, p, pid, '合成光电交付10台光学模组。')
    assert len(p.questions.history(watches[0]['id'])) == 2
    advisor = p.question_advisor = Advisor()
    denied_source = intake(app, web, p, pid, '合成光电交付20台光学模组。', run=False)
    from contextlib import closing
    with closing(p.store._connect()) as db, db:
        db.execute('INSERT OR REPLACE INTO studio_source_permissions VALUES (?,?,?,?,?)',
                   (denied_source, pid, '', 'synthetic', '2026-10-13'))
    p.run_internal(p.store.enqueue(pid, 'internal'))
    assert all(m['source_id'] != denied_source for call in advisor.calls for q in call for m in q['materials'])
    assert denied_source not in p.store.analyzed_source_ids(pid)
