"""M3B: bounded requests must not become a ceiling on work completion."""
import socket
from contextlib import closing

import pytest
from test_studio_question_materials import intake
from test_studio_question_materials import setup as material_setup
from test_studio_questions import LLM, hit_event, setup

from project_watch.studio.llm import ModelError
from project_watch.studio.questions import Assessment, QuestionLedger


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('Backlog tests cannot use live networking')
    monkeypatch.setattr(socket, 'create_connection', denied)
    monkeypatch.setattr(socket, 'getaddrinfo', denied)


class Citing:
    def __init__(self):
        self.calls = []

    def assess(self, llm, project, questions):
        self.calls.append(questions)
        return [{'id': q['id'], 'status': 'unchanged', 'note': '本批资料有相关记录',
                 'event_ids': [e['id'] for e in q['events']],
                 'material_ids': [m['id'] for m in q['materials']]} for q in questions]


def event_context(tmp_path, count=19):
    store, monitor, ledger, pid = setup(tmp_path)
    watch = monitor.add_watchpoint(pid, '交付情况', '交付', ['交付'], '交付', False, visibility='public')
    events = {hit_event(store, monitor, pid, watch['id'], f'合成交付消息 {i}') for i in range(count)}
    ledger.record(pid, watch['id'], 'human', Assessment(status='setback', note='人的原判断'))
    ledger.record(pid, watch['id'], 'agent', Assessment(status='insufficient', note='Agent 原判断'))
    return store, monitor, ledger, pid, watch, events


def state(ledger, pid, watch, **kwargs):
    return next(q for q in ledger.view(pid, **kwargs) if q['id'] == watch['id'])['assessment_state']


def test_one_round_drains_all_events_in_bounded_calls_and_reopen_is_noop(tmp_path):
    store, monitor, ledger, pid, watch, events = event_context(tmp_path)
    advisor = Citing()
    assert ledger.assess(advisor, LLM, pid, {}) == 4
    assert [len(c[0]['events']) for c in advisor.calls] == [6, 6, 6, 1]
    sent = [e['id'] for c in advisor.calls for q in c for e in q['events']]
    assert len(sent) == len(set(sent)) == 19 and set(sent) == events
    assert ledger.new_events(pid, watch['id']) == []
    before = ledger.history(watch['id'])
    current = QuestionLedger(store, monitor)
    assert current.assess(advisor, LLM, pid, {}) == 0
    assert current.history(watch['id']) == before
    assert state(current, pid, watch)['pending_events'] == 0
    assert {a['note'] for a in before if a['actor'] in ('human', 'agent')} == {'人的原判断', 'Agent 原判断'}


def test_all_materials_receive_an_opportunity_in_normal_intake(tmp_path):
    app, web, p, pid, watches = material_setup(tmp_path)
    for i in range(13):
        intake(app, web, p, pid, f'合成光电交付{i}台光学模组。')
    advisor = Citing()
    assert p.questions.assess(advisor, LLM, pid, {}) == 3
    assert [len(c[0]['materials']) for c in advisor.calls] == [6, 6, 1]
    assert len({m['id'] for c in advisor.calls for q in c for m in q['materials']}) == 13
    assert p.questions.new_materials(pid, watches[0]['id']) == []
    assert state(p.questions, pid, watches[0])['pending_materials'] == 0
    view = p.questions.view(pid)[0]
    assert view['latest']['human']['note'] == '人的原判断'
    assert view['latest']['agent']['note'] == 'Agent 原判断'
    assert p.questions.view(pid)[1]['latest'] == {}
    assert state(p.questions, pid, watches[0], include_internal=False)['latest_attempt'] is None
    assert 'pending_materials' not in state(p.questions, pid, watches[0], include_internal=False)


@pytest.mark.parametrize(('kind', 'reason'), [('figure', 'unsupported_number'), ('uncited', 'missing_reference'), ('status', 'invalid_status')])
def test_rejected_batches_do_not_repeat_in_same_round_or_look_complete(tmp_path, kind, reason):
    store, monitor, ledger, pid, watch, events = event_context(tmp_path, 7)
    class Rejected(Citing):
        def assess(self, *args):
            rows = super().assess(*args)
            for row in rows:
                if kind == 'figure':
                    row['note'] = '交付999999台'
                elif kind == 'uncited':
                    row['event_ids'] = []
                else:
                    row['status'] = 'great'
            return rows
    advisor = Rejected()
    assert ledger.assess(advisor, LLM, pid, {}) == 0
    assert len(advisor.calls) == 2  # no unbounded retry of the first rejected batch
    current = QuestionLedger(store, monitor)
    assert {e['id'] for e in current.new_events(pid, watch['id'], limit=None)} == events
    outcome = state(current, pid, watch)
    assert outcome['pending_events'] == 7
    assert outcome['latest_attempt']['status'] == 'rejected'
    assert outcome['latest_attempt']['reason'] == reason
    assert len(current.history(watch['id'])) == 2


def test_explicit_omission_is_recorded_separately_from_a_judgement(tmp_path):
    _, _, ledger, pid, watch, _ = event_context(tmp_path, 7)
    class Omitted(Citing):
        def assess(self, *args):
            super().assess(*args)
            return []
    advisor = Omitted()
    assert ledger.assess(advisor, LLM, pid, {}) == 0 and len(advisor.calls) == 2
    outcome = state(ledger, pid, watch)
    assert outcome['pending_events'] == 0
    assert outcome['latest_attempt']['status'] == 'omitted'
    assert outcome['latest_attempt']['reason'] == 'advisor_omitted'
    assert len(ledger.history(watch['id'])) == 2


@pytest.mark.parametrize('code', ['request_outcome_unknown', 'request_budget', 'model_content_rejected', 'model_output_invalid_shape'])
def test_failed_second_batch_stops_and_reopen_keeps_exact_remaining_inputs(tmp_path, code):
    store, monitor, ledger, pid, watch, events = event_context(tmp_path)
    class Interrupted(Citing):
        def assess(self, *args):
            if self.calls:
                self.calls.append(args[2])
                raise ModelError('SYNTHETIC_SECRET_KEY never persist this', code=code)
            return super().assess(*args)
    advisor = Interrupted()
    with pytest.raises(ModelError) as exc:
        ledger.assess(advisor, LLM, pid, {})
    assert exc.value.code == code and len(advisor.calls) == 2
    current = QuestionLedger(store, monitor)
    remaining = current.new_events(pid, watch['id'], limit=None)
    consumed = {e['id'] for e in advisor.calls[0][0]['events']}
    assert {e['id'] for e in remaining} == events - consumed and len(remaining) == 13
    assert len(current.history(watch['id'])) == 3
    outcome = state(current, pid, watch)
    assert outcome['latest_attempt']['status'] == 'failed'
    assert outcome['latest_attempt']['reason'] == code
    assert 'SYNTHETIC_SECRET_KEY' not in str(outcome)
    recovery = Citing()
    # Explicit offline recovery, not automatic retry of the ambiguous request.
    assert current.assess(recovery, LLM, pid, {}) == 3
    assert {e['id'] for c in recovery.calls for q in c for e in q['events']} == events - consumed


def test_question_round_suggests_then_assesses_new_material_question(tmp_path):
    app, web, p, pid, _ = material_setup(tmp_path)
    intake(app, web, p, pid, '合成光电生产光学模组。')
    class Advisor(Citing):
        def suggest(self, *args):
            return [{'text': '光学模组有什么进展', 'label': '光学模组', 'queries': ['合成光电 光学模组'], 'criteria': '产品'}]
    p.question_advisor = Advisor()
    p._question_round(pid, p.store.get_project(pid), {}, LLM, lambda _: None)
    watch = next(q for q in p.questions.view(pid) if q['origin'] == 'ai')
    assert watch['latest']['system']['material_ids']
    assert watch['assessment_state']['pending_materials'] == 0


def test_name_entry_new_question_can_use_already_collected_public_event(tmp_path):
    _app, _web, p, pid, watches = material_setup(tmp_path)
    event = hit_event(p.store, p.monitor, pid, watches[0]['id'], '合成光电交付10台')
    class Advisor(Citing):
        def suggest(self, *args):
            return [{'text': '交付情况值得跟踪吗', 'label': '交付', 'queries': ['合成光电 交付'], 'criteria': '交付'}]
    advisor = Advisor()
    p.question_advisor = advisor
    p._question_round(pid, p.store.get_project(pid), {}, LLM, lambda _: None)
    watch = next(q for q in p.questions.view(pid) if q['origin'] == 'ai')
    assert watch['visibility'] == 'public'
    assert watch['latest']['system']['event_ids'] == [event]
    assert p.questions.route_existing_events(pid, {watch['id']}) == 0
    p._question_round(pid, p.store.get_project(pid), {}, LLM, lambda _: None)
    assert len(advisor.calls) == 1


def test_question_delete_cleans_attempts(tmp_path):
    store, monitor, ledger, pid, watch, _ = event_context(tmp_path, 1)
    ledger.assess(Citing(), LLM, pid, {})
    monitor.remove_watchpoint(watch['id'])
    with closing(store._connect()) as db:
        assert db.execute('SELECT count(*) FROM studio_question_assessment_attempts WHERE watch_id=?', (watch['id'],)).fetchone()[0] == 0


def test_new_arrivals_are_not_an_infinite_extension_of_the_current_round(tmp_path):
    store, monitor, ledger, pid, watch, events = event_context(tmp_path, 7)
    arrivals = []
    class Arriving(Citing):
        def assess(self, *args):
            arrivals.append(hit_event(store, monitor, pid, watch['id'], '下一轮合成输入'))
            return super().assess(*args)
    advisor = Arriving()
    assert ledger.assess(advisor, LLM, pid, {}) == 2
    assert {e['id'] for c in advisor.calls for q in c for e in q['events']} == events
    assert {e['id'] for e in ledger.new_events(pid, watch['id'], limit=None)} == set(arrivals)


def test_current_question_version_is_rechecked_before_next_dispatch(tmp_path):
    store, _, ledger, pid, watch, _ = event_context(tmp_path, 7)
    advisor = Citing()
    original = ledger._assess_batch
    def change_after_commit(*args):
        result = original(*args)
        with closing(store._connect()) as db, db:
            db.execute('UPDATE studio_watchpoints SET text=? WHERE id=?', ('问题已改成另一个范围', watch['id']))
        return result
    ledger._assess_batch = change_after_commit
    with pytest.raises(ValueError, match='question input changed'):
        ledger.assess(advisor, LLM, pid, {})
    assert len(advisor.calls) == 1
    assert len(ledger.history(watch['id'])) == 3
    assert state(ledger, pid, watch)['latest_attempt'] is None


def test_newest_input_finishes_last_and_remains_the_current_system_judgement(tmp_path):
    store, _, ledger, pid, _watch, events = event_context(tmp_path, 7)
    ordered = sorted(events)
    with closing(store._connect()) as db, db:
        for i, event in enumerate(ordered):
            db.execute('UPDATE studio_events SET event_at=? WHERE id=?', (f'2026-10-{i+1:02d}', event))
    advisor = Citing()
    assert ledger.assess(advisor, LLM, pid, {}) == 2
    assert [e['id'] for c in advisor.calls for q in c for e in q['events']] == ordered
    assert ledger.view(pid)[0]['latest']['system']['event_ids'] == [ordered[-1]]


def test_malformed_reply_is_not_an_omission_and_error_details_stay_private(tmp_path):
    store, monitor, ledger, pid, watch, events = event_context(tmp_path, 7)
    class Malformed(Citing):
        def assess(self, *args):
            super().assess(*args)
            return {'SYNTHETIC_SECRET': 'not a results list'}
    advisor = Malformed()
    with pytest.raises(ValueError):
        ledger.assess(advisor, LLM, pid, {})
    current = QuestionLedger(store, monitor)
    assert len(advisor.calls) == 1
    assert {e['id'] for e in current.new_events(pid, watch['id'], limit=None)} == events
    assert state(current, pid, watch)['latest_attempt']['status'] == 'failed'
    assert 'SYNTHETIC_SECRET' not in str(state(current, pid, watch))


def test_failure_attempt_is_hidden_when_model_binding_changes(tmp_path):
    store, monitor, ledger, pid, watch, _ = event_context(tmp_path, 1)
    active = ['synthetic-binding-one']
    ledger.binding_provider = lambda: active[0]
    class Unknown:
        def assess(self, *args):
            raise ModelError('SYNTHETIC_SECRET', code='request_outcome_unknown')
    with pytest.raises(ModelError):
        ledger.assess(Unknown(), LLM, pid, {})
    assert state(ledger, pid, watch)['latest_attempt']['status'] == 'failed'
    active[0] = 'synthetic-binding-two'
    current = QuestionLedger(store, monitor, binding_provider=lambda: active[0])
    assert state(current, pid, watch)['latest_attempt'] is None


def test_lost_lease_prevents_judgement_consumption_and_diagnostic_writes(tmp_path):
    store, _, ledger, pid, watch, events = event_context(tmp_path, 7)
    expired = [False]
    def guard(_db):
        if expired[0]:
            raise RuntimeError('synthetic lease lost')
    class Expired(Citing):
        def assess(self, *args):
            result = super().assess(*args)
            expired[0] = True
            return result
    advisor = Expired()
    with store.write_scope(guard), pytest.raises(RuntimeError, match='synthetic lease lost'):
        ledger.assess(advisor, LLM, pid, {})
    assert len(advisor.calls) == 1 and len(ledger.history(watch['id'])) == 2
    assert {e['id'] for e in ledger.new_events(pid, watch['id'], limit=None)} == events
    with closing(store._connect()) as db:
        assert db.execute('SELECT count(*) FROM studio_question_assessment_attempts').fetchone()[0] == 0


def test_internal_failure_metadata_does_not_leak_through_public_agent_api(tmp_path):
    from test_studio_application_parity import headers
    app, web, p, pid, _watches = material_setup(tmp_path)
    intake(app, web, p, pid, '合成光电交付10台光学模组。')
    class Unknown:
        def assess(self, *args):
            raise ModelError('SYNTHETIC_SECRET', code='request_outcome_unknown')
    with pytest.raises(ModelError):
        p.questions.assess(Unknown(), LLM, pid, {})
    public = app.state.studio_application.issue_grant(pid, 'read', 'public')
    internal = app.state.studio_application.issue_grant(pid, 'read', 'internal', allow_internal=True)
    route = '/api/studio/agent/projects/' + pid + '/questions'
    public_reply = web.get(route, headers=headers(public))
    private_reply = web.get(route, headers=headers(internal))
    assert public_reply.status_code == private_reply.status_code == 200
    assert 'request_outcome_unknown' not in public_reply.text
    assert 'pending_materials' not in public_reply.text
    assert 'request_outcome_unknown' in private_reply.text
    assert 'SYNTHETIC_SECRET' not in private_reply.text


def test_omitted_material_is_processed_but_not_labelled_assessed(tmp_path):
    app, web, p, pid, watches = material_setup(tmp_path)
    intake(app, web, p, pid, '合成光电交付10台光学模组。')
    class Omitted:
        def assess(self, *args):
            return []
    assert p.questions.assess(Omitted(), LLM, pid, {}) == 0
    view = next(q for q in p.questions.view(pid) if q['id'] == watches[0]['id'])
    assert view['materials'][0]['status'] == 'omitted'
    assert len(p.questions.history(watches[0]['id'])) == 2


def test_legacy_consumption_migration_does_not_invent_a_judgement(tmp_path):
    app, web, p, pid, watches = material_setup(tmp_path)
    intake(app, web, p, pid, '合成光电交付10台光学模组。')
    [hit] = p.questions.new_materials(pid, watches[0]['id'])
    with closing(p.store._connect()) as db, db:
        db.execute('DROP TABLE studio_question_consumed_materials')
        db.execute('CREATE TABLE studio_question_consumed_materials(watch_id TEXT NOT NULL,hit_id TEXT NOT NULL,PRIMARY KEY(watch_id,hit_id))')
        db.execute('INSERT INTO studio_question_consumed_materials VALUES (?,?)', (watches[0]['id'], hit['id']))
    current = QuestionLedger(p.store, p.monitor, binding_provider=p.model_binding)
    assert current.view(pid)[0]['materials'][0]['status'] == 'processed'
    assert current.new_materials(pid, watches[0]['id']) == []
    current.record(pid, watches[0]['id'], 'agent', Assessment(status='insufficient', material_ids=(hit['id'],)))
    assert current.view(pid)[0]['materials'][0]['status'] == 'assessed'
