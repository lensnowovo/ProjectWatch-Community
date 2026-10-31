"""Normal internal dispatch, not manually calling the question ledger."""
import socket

import pytest
from test_studio_question_materials import intake, setup

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
