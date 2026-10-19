"""Invalid/rejected question answers must not silently consume new inputs."""
import json
import socket
from contextlib import closing

import pytest
from test_studio_questions import LLM, hit_event, setup

from project_watch.studio.llm import ModelError
from project_watch.studio.question_advisor import ModelQuestionAdvisor
from project_watch.studio.questions import Assessment


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('Question rejection controls are offline')
    monkeypatch.setattr(socket, 'create_connection', denied)
    monkeypatch.setattr(socket, 'getaddrinfo', denied)


def context(tmp_path):
    store, monitor, ledger, pid = setup(tmp_path)
    watch = monitor.add_watchpoint(pid, '交付进展', '交付', ['交付'], '交付', False)
    event = hit_event(store, monitor, pid, watch['id'], '合成光电交付10台')
    ledger.record(pid, watch['id'], 'human', Assessment(status='setback', note='人的原判断'))
    ledger.record(pid, watch['id'], 'agent', Assessment(status='insufficient', note='Agent 原判断'))
    return store, monitor, ledger, pid, watch, event


class FakeModel:
    ready = True

    def __init__(self, reply):
        self.reply, self.calls = reply, 0

    def json(self, *args, **kwargs):
        self.calls += 1
        return self.reply


@pytest.mark.parametrize('reply', [None, {}, {'results': None}, {'results': {}}, {'results': [None]},
    {'results': [{'status': 'progress'}]}, {'results': [{'id': 'foreign', 'status': 'great'}]},
    {'results': [{'id': 'foreign', 'status': 'progress', 'event_ids': []}]}])
def test_malformed_native_reply_preserves_inputs_and_history(tmp_path, reply):
    _, _, ledger, pid, watch, event = context(tmp_path)
    model = FakeModel(reply)
    before = ledger.history(watch['id'])
    with pytest.raises(ModelError) as caught:
        ledger.assess(ModelQuestionAdvisor(), model, pid, {})
    assert caught.value.code == 'model_output_invalid_shape'
    assert model.calls == 1
    assert [e['id'] for e in ledger.new_events(pid, watch['id'])] == [event]
    assert ledger.history(watch['id']) == before


@pytest.mark.parametrize('invalid', ['status', 'uncited', 'figure'])
def test_returned_but_rejected_question_is_not_a_decline(tmp_path, invalid):
    _, _, ledger, pid, watch, event = context(tmp_path)
    row = {'id': watch['id'], 'status': 'progress', 'note': '交付10台', 'event_ids': [event]}
    if invalid == 'status':
        row['status'] = 'great'
    elif invalid == 'uncited':
        row['event_ids'] = []
    else:
        row['note'] = '交付100台'
    class Advisor:
        def assess(self, *args):
            return [row]
    before = ledger.history(watch['id'])
    assert ledger.assess(Advisor(), LLM, pid, {}) == 0
    assert [e['id'] for e in ledger.new_events(pid, watch['id'])] == [event]
    assert ledger.history(watch['id']) == before


def test_explicit_native_empty_results_remain_a_known_decline(tmp_path):
    _, _, ledger, pid, watch, _ = context(tmp_path)
    model = FakeModel({'results': []})
    before = ledger.history(watch['id'])
    assert ledger.assess(ModelQuestionAdvisor(), model, pid, {}) == 0
    assert model.calls == 1 and ledger.new_events(pid, watch['id']) == []
    assert ledger.history(watch['id']) == before


@pytest.mark.parametrize('reply', [None, {}, [None], [{'status': 'progress'}],
    [{'id': 'foreign', 'status': 'progress'}]])
def test_bad_injected_advisor_shape_cannot_consume_as_empty_success(tmp_path, reply):
    _, _, ledger, pid, watch, event = context(tmp_path)
    class Advisor:
        def assess(self, *args):
            return reply
    with pytest.raises(ValueError):
        ledger.assess(Advisor(), LLM, pid, {})
    assert [e['id'] for e in ledger.new_events(pid, watch['id'])] == [event]
    assert len(ledger.history(watch['id'])) == 2


def test_duplicate_question_ids_fail_before_first_publication(tmp_path):
    _, _, ledger, pid, watch, event = context(tmp_path)
    row = {'id': watch['id'], 'status': 'progress', 'event_ids': [event]}
    class Advisor:
        def assess(self, *args):
            return [row, row]
    with pytest.raises(ValueError):
        ledger.assess(Advisor(), LLM, pid, {})
    assert len(ledger.history(watch['id'])) == 2
    assert [e['id'] for e in ledger.new_events(pid, watch['id'])] == [event]


def test_valid_sibling_is_saved_rejected_sibling_stays_pending(tmp_path):
    store, monitor, ledger, pid, first, event = context(tmp_path)
    second = monitor.add_watchpoint(pid, '其他交付', '交付', ['交付'], '交付', False)
    second_event = hit_event(store, monitor, pid, second['id'], '合成光电交付20台')
    class Advisor:
        def assess(self, *args):
            return [{'id': first['id'], 'status': 'progress', 'note': '交付10台', 'event_ids': [event]},
                    {'id': second['id'], 'status': 'progress', 'note': '交付200台', 'event_ids': [second_event]}]
    assert ledger.assess(Advisor(), LLM, pid, {}) == 1
    assert ledger.new_events(pid, first['id']) == []
    assert [e['id'] for e in ledger.new_events(pid, second['id'])] == [second_event]
    assert len(ledger.history(first['id'])) == 3 and ledger.history(second['id']) == []


@pytest.mark.parametrize('field,value', [('status', {}), ('event_ids', 'an-id'),
    ('event_ids', [[]]), ('material_ids', {}), ('note', {'text': 'not prose'})])
@pytest.mark.parametrize('native', [False, True])
def test_bad_row_fields_fail_before_valid_sibling_is_written(tmp_path, field, value, native):
    store, monitor, ledger, pid, first, event = context(tmp_path)
    second = monitor.add_watchpoint(pid, '其他交付', '交付', ['交付'], '交付', False)
    second_event = hit_event(store, monitor, pid, second['id'], '合成光电交付20台')
    bad = {'id': second['id'], 'status': 'progress', 'event_ids': [second_event]}
    bad[field] = value
    rows = [{'id': first['id'], 'status': 'progress', 'event_ids': [event]}, bad]
    class Advisor:
        def assess(self, *args):
            return rows
    model = FakeModel({'results': rows})
    with pytest.raises(ModelError if native else ValueError):
        ledger.assess(ModelQuestionAdvisor() if native else Advisor(), model, pid, {})
    assert len(ledger.history(first['id'])) == 2 and ledger.history(second['id']) == []
    assert [e['id'] for e in ledger.new_events(pid, first['id'])] == [event]
    assert [e['id'] for e in ledger.new_events(pid, second['id'])] == [second_event]
    assert model.calls == int(native)


def test_rejected_material_remains_pending_after_database_reopen(tmp_path):
    from test_studio_question_materials import intake, material
    from test_studio_question_materials import setup as material_setup

    from project_watch.studio.questions import QuestionLedger
    app, web, p, pid, watches = material_setup(tmp_path)
    intake(app, web, p, pid, '合成光电交付10台光学模组。')
    [hit] = material(p, pid, watches[0]['id'])
    class Advisor:
        def assess(self, *args):
            return [{'id': watches[0]['id'], 'status': 'progress', 'note': '交付100台', 'material_ids': [hit['id']]}]
    before = p.questions.history(watches[0]['id'])
    assert p.questions.assess(Advisor(), LLM, pid, {}) == 0
    reopened = QuestionLedger(p.store, p.monitor, binding_provider=p.model_binding)
    assert [m['id'] for m in reopened.new_materials(pid, watches[0]['id'])] == [hit['id']]
    assert reopened.history(watches[0]['id']) == before


@pytest.mark.parametrize('outcome', ['malformed', 'rejected', 'empty', 'unknown'])
def test_leased_http_and_reopen_do_not_resend_bad_or_uncertain_response(tmp_path, outcome):
    from project_watch.studio.execution import JobExecution
    from project_watch.studio.http import Http, HttpError, Response
    from project_watch.studio.llm import LLM as NativeLLM
    from project_watch.studio.monitor import MonitorStore
    from project_watch.studio.questions import QuestionLedger
    from project_watch.studio.settings import ModelConfig
    from project_watch.studio.store import StudioStore

    store, _, ledger, pid, watch, event = context(tmp_path)
    store.enqueue(pid, 'brief')
    job = store.claim()
    calls = []
    def send(*args):
        calls.append('model')
        if outcome == 'unknown':
            raise HttpError('connection_failed')
        data = {} if outcome == 'malformed' else {'results': []}
        if outcome == 'rejected':
            data['results'] = [{'id': watch['id'], 'status': 'progress', 'note': '交付100台', 'event_ids': [event]}]
        return Response(200, {}, json.dumps({'choices': [{'message': {'content': json.dumps(data)},
            'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 40, 'completion_tokens': 20}}).encode())
    cfg = ModelConfig(base_url='https://model.synthetic.example/v1', model='synthetic', api_key='synthetic-only')
    def assess(current, execution):
        http = execution.http(Http(send), lambda _: ('model', 'synthetic-provider'))
        model = NativeLLM(cfg, http)
        if outcome in ('malformed', 'unknown'):
            with pytest.raises(ModelError) as error:
                current.assess(ModelQuestionAdvisor(), model, pid, {})
            assert error.value.code == ('model_output_invalid_shape' if outcome == 'malformed' else 'request_outcome_unknown')
        else:
            assert current.assess(ModelQuestionAdvisor(), model, pid, {}) == 0
    assess(ledger, JobExecution(store, job))
    assert calls == ['model']
    with closing(store._connect()) as db, db:
        db.execute("UPDATE studio_jobs SET lease_until='2000-01-01' WHERE id=?", (job['id'],))
    reopened = StudioStore(store.db_path)
    retry = reopened.claim()
    current = QuestionLedger(reopened, MonitorStore(reopened))
    if outcome == 'unknown':
        assert retry is None  # Ambiguous calls are held, not automatically reclaimed.
        failed = next(j for j in reopened.jobs(pid) if j['id'] == job['id'])
        assert failed['status'] == 'failed' and failed['retry_after'] == '9999-12-31'
        assert reopened.claim() is None
        execution = JobExecution(reopened, failed)
    else:
        assert retry['id'] == job['id'] and retry['lease_token'] != job['lease_token']
        execution = JobExecution(reopened, retry)
        assess(current, execution)
    assert calls == ['model']
    assert len(current.history(watch['id'])) == 2
    pending = [e['id'] for e in current.new_events(pid, watch['id'])]
    assert pending == ([] if outcome == 'empty' else [event])
    usage = execution.usage()['model']
    assert usage['requests'] == 1
    assert usage['unknown'] == int(outcome == 'unknown')
    assert usage['completed'] == int(outcome != 'unknown')
    assert usage['cache_hits'] == int(outcome in ('malformed', 'rejected'))
