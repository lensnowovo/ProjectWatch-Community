"""Question ledger on watch points (M2), synthetic and offline."""

import uuid
from datetime import UTC, datetime

import pytest

from project_watch.repository import ProjectRepository
from project_watch.studio.monitor import MonitorStore
from project_watch.studio.questions import Assessment, QuestionLedger
from project_watch.studio.store import StudioStore


def setup(tmp_path):
    repo = ProjectRepository(tmp_path / 'db.sqlite3')
    project = repo.create_project('合成光电')
    store = StudioStore(repo.db_path)
    store.upsert_project(project.id, {'legal_name': '合成光电有限公司'}, consent=True)
    monitor = MonitorStore(store)
    return store, monitor, QuestionLedger(store, monitor), project.id


def hit_event(store, monitor, pid, watch_id, title):
    key = uuid.uuid4().hex
    store.add_items(pid, 'company', [{'key': key, 'title': title, 'url': f'https://n.example/{key}',
                                      'source': 's', 'snippet': '', 'published_at': datetime.now(UTC).isoformat()}])
    item = next(i for i in store.untriaged(pid) if i['key'] == key)
    store.save_triage([{'id': item['id'], 'relevant': True, 'importance': 'notable', 'summary': title}])
    event = store.create_event(pid, {'title': title, 'category': 'company', 'importance': 'notable',
                                     'summary': title}, [item['id']])
    monitor.add_hits([(watch_id, item['id'])])
    return event


class Advisor:
    def __init__(self):
        self.suggest_calls, self.assess_calls = 0, []

    def suggest(self, llm, project, plan, profile, existing):
        self.suggest_calls += 1
        return [{'text': '量产交付是否按计划推进', 'label': '量产', 'queries': ['合成光电 量产'], 'criteria': '量产或交付消息'},
                {'text': '主要客户是否扩大', 'label': '客户', 'queries': ['合成光电 客户'], 'criteria': '新客户或订单'},
                {'text': 'x', 'label': 'bad', 'queries': [], 'criteria': ''}]

    def assess(self, llm, project, questions):
        self.assess_calls.append(questions)
        return [{'id': q['id'], 'status': 'progress', 'note': '有新的量产消息',
                 'event_ids': [q['events'][0]['id'], 'not-in-input']} for q in questions]


LLM = type('LLM', (), {'ready': True})()


def test_suggest_once_and_skip_invalid_rows(tmp_path):
    _store, monitor, ledger, pid = setup(tmp_path)
    advisor = Advisor()
    assert ledger.suggest(advisor, LLM, pid, {}, {}, None) == 2
    assert ledger.suggest(advisor, LLM, pid, {}, {}, None) == 0  # AI questions exist: never re-suggest
    assert advisor.suggest_calls == 1
    assert {w['origin'] for w in monitor.watchpoints(pid)} == {'ai'}
    assert all(not w['alert'] for w in monitor.watchpoints(pid))  # suggestions do not push alerts by default


def test_no_advisor_or_model_means_no_calls_but_ledger_works(tmp_path):
    store, monitor, ledger, pid = setup(tmp_path)
    assert ledger.suggest(None, LLM, pid, {}, {}, None) == 0
    watch = monitor.add_watchpoint(pid, '订单', '订单', ['合成光电 订单'], '订单', True)
    event = hit_event(store, monitor, pid, watch['id'], '合成光电获订单')
    assert ledger.assess(None, LLM, pid, {}) == 0
    row = ledger.record(pid, watch['id'], 'human', Assessment(status='progress', note='我认为在推进', event_ids=(event,)))
    assert row['status_label'] == '有进展' and row['event_ids'] == [event]


def test_assess_only_new_hits_one_call_and_cited_events_only(tmp_path):
    store, monitor, ledger, pid = setup(tmp_path)
    advisor = Advisor()
    ledger.suggest(advisor, LLM, pid, {}, {}, None)
    first, _second = monitor.watchpoints(pid)
    event = hit_event(store, monitor, pid, first['id'], '合成光电量产交付')
    assert ledger.assess(advisor, LLM, pid, {}) == 1
    assert len(advisor.assess_calls) == 1 and [q['id'] for q in advisor.assess_calls[0]] == [first['id']]
    [row] = ledger.history(first['id'])
    assert row['actor'] == 'system' and row['event_ids'] == [event]  # the invented id was dropped
    # Nothing new: no call.
    assert ledger.assess(advisor, LLM, pid, {}) == 0 and len(advisor.assess_calls) == 1


def test_system_refresh_never_overwrites_human_or_agent(tmp_path):
    store, monitor, ledger, pid = setup(tmp_path)
    watch = monitor.add_watchpoint(pid, '量产', '量产', ['合成光电 量产'], '量产', True)
    e1 = hit_event(store, monitor, pid, watch['id'], '合成光电量产')
    ledger.record(pid, watch['id'], 'human', Assessment(status='setback', note='人的判断：延期'))
    ledger.record(pid, watch['id'], 'agent', Assessment(status='insufficient', note='Agent：证据不足', event_ids=(e1,)))
    ledger.assess(Advisor(), LLM, pid, {})
    [view] = ledger.view(pid)
    assert view['latest']['human']['note'] == '人的判断：延期'
    assert view['latest']['agent']['note'] == 'Agent：证据不足'
    assert view['latest']['system']['status'] == 'progress'
    assert view['history_count'] == 3 and e1 in view['events']


def test_validation_and_idempotency(tmp_path):
    _store, monitor, ledger, pid = setup(tmp_path)
    watch = monitor.add_watchpoint(pid, '量产', '量产', ['q'], 'c', True)
    with pytest.raises(ValueError):
        ledger.record(pid, watch['id'], 'system', Assessment(status='progress'))  # system must cite
    with pytest.raises(ValueError):
        ledger.record(pid, watch['id'], 'agent', Assessment(status='progress', event_ids=('foreign',)))
    with pytest.raises(KeyError):
        ledger.record('other-project', watch['id'], 'human', Assessment(status='progress'))
    with pytest.raises(ValueError):
        ledger.record(pid, watch['id'], 'robot', Assessment(status='progress'))
    a = ledger.record(pid, watch['id'], 'agent', Assessment(status='unchanged', idempotency_key='k1'))
    b = ledger.record(pid, watch['id'], 'agent', Assessment(status='progress', idempotency_key='k1'))
    assert a['id'] == b['id'] and len(ledger.history(watch['id'])) == 1


def test_removing_a_question_removes_its_history(tmp_path):
    _store, monitor, ledger, pid = setup(tmp_path)
    watch = monitor.add_watchpoint(pid, '量产', '量产', ['q'], 'c', True)
    ledger.record(pid, watch['id'], 'human', Assessment(status='progress'))
    monitor.remove_watchpoint(watch['id'])
    assert ledger.history(watch['id']) == []


def test_pipeline_round_skips_model_errors_but_stops_on_unknown(tmp_path):
    from project_watch.studio.app import create_studio_app
    from project_watch.studio.http import HttpError
    from project_watch.studio.llm import ModelError
    from project_watch.studio.question_advisor import ModelQuestionAdvisor

    pipeline = create_studio_app(tmp_path, start_worker=False).state.studio_pipeline
    assert isinstance(pipeline.question_advisor, ModelQuestionAdvisor)  # wired by the composition root
    pid = pipeline.repository.create_project('合成光电').id
    pipeline.store.upsert_project(pid, {}, consent=True)

    class Refusing(Advisor):
        def suggest(self, *a):
            raise ModelError('refused', code='model_content_rejected')

    pipeline.question_advisor = Refusing()
    pipeline._question_round(pid, {}, {}, LLM, lambda _: None)  # skipped, monitoring continues

    class Unknown(Advisor):
        def suggest(self, *a):
            raise HttpError('request_outcome_unknown')

    pipeline.question_advisor = Unknown()
    with pytest.raises(HttpError):
        pipeline._question_round(pid, {}, {}, LLM, lambda _: None)
    pipeline.question_advisor = None
    pipeline._question_round(pid, {}, {}, LLM, lambda _: None)  # no advisor: nothing to do


def test_model_advisor_parses_and_filters_replies():
    from project_watch.studio.llm import ModelError
    from project_watch.studio.question_advisor import ModelQuestionAdvisor

    class Fake:
        ready = True

        def __init__(self, reply):
            self.reply, self.calls = reply, []

        def json(self, system, user, max_tokens=0):
            self.calls.append(user)
            return self.reply

    llm = Fake({'questions': [{'text': 'q', 'queries': ['a']}, 'bad'] * 6})
    rows = ModelQuestionAdvisor().suggest(llm, {'legal_name': '合成光电'}, {'competitors': [{'name': '对手'}]},
                                          {'summary': '材料摘要'}, ['已有'])
    assert len(rows) == 5 and all(isinstance(r, dict) for r in rows)
    assert '材料摘要' in llm.calls[0] and '已有' in llm.calls[0] and '对手' in llm.calls[0]
    llm = Fake({'results': [{'id': 'w', 'status': 'progress'}]})
    assert [r['status'] for r in ModelQuestionAdvisor().assess(llm, {}, [{'id': 'w'}])] == ['progress']
    with pytest.raises(ModelError):
        ModelQuestionAdvisor().assess(Fake({'results': [{'id': 'w', 'status': 'great'}]}), {}, [{'id': 'w'}])
    with pytest.raises(ModelError):
        ModelQuestionAdvisor().assess(Fake(['not a dict']), {}, [])


def test_http_routes_list_and_record_human_judgements(tmp_path):
    from fastapi.testclient import TestClient

    from project_watch.studio.app import create_studio_app

    app = create_studio_app(tmp_path, start_worker=False)
    client = TestClient(app)
    pid = client.post('/api/studio/projects', json={'entity': {'legal_name': '合成光电'}, 'consent': True}).json()['id']
    monitor = app.state.studio_pipeline.monitor
    watch = monitor.add_watchpoint(pid, '量产交付是否按计划', '量产', ['合成光电 量产'], '量产消息', False, origin='ai')
    base = f'/api/studio/projects/{pid}/questions'
    created = client.post(f"{base}/{watch['id']}/assessments", json={'status': 'setback', 'note': '我的判断'})
    assert created.status_code == 201 and created.json()['actor'] == 'human'
    assert client.post(f"{base}/{watch['id']}/assessments", json={'status': 'great'}).status_code == 422
    assert client.post(f"{base}/{watch['id']}/assessments", json={'status': 'progress', 'event_ids': ['x']}).status_code == 422
    assert client.post(f'{base}/missing/assessments', json={'status': 'progress'}).status_code == 404
    [q] = client.get(base).json()['questions']
    assert q['origin'] == 'ai' and q['latest']['human']['note'] == '我的判断'
    assert len(client.get(f"{base}/{watch['id']}/history").json()) == 1
    assert client.get(f'/api/studio/projects/{pid}/monitor').json()['questions'][0]['id'] == watch['id']


def test_agent_reads_the_pack_and_writes_back_with_grants(tmp_path):
    from test_studio_application_parity import headers, setup_app

    app, web, pid, grant = setup_app(tmp_path)
    store = app.state.studio_store
    monitor = app.state.studio_pipeline.monitor
    watch = monitor.add_watchpoint(pid, '客户是否扩大', '客户', ['q'], 'c', False, origin='ai', visibility='public')
    event = hit_event(store, monitor, pid, watch['id'], 'Synthetic Optics 获新客户')
    root = f'/api/studio/agent/projects/{pid}/questions'
    assert web.get(root).status_code == 401
    [q] = web.get(root, headers=headers(grant)).json()['questions']
    assert q['id'] == watch['id']
    body = {'status': 'progress', 'note': 'Agent 研究：新客户已签约', 'event_ids': [event], 'idempotency_key': 'agent-1'}
    assert web.post(f"{root}/{watch['id']}/assessments", json={**body, 'idempotency_key': None},
                    headers=headers(grant)).status_code == 422
    first = web.post(f"{root}/{watch['id']}/assessments", json=body, headers=headers(grant))
    again = web.post(f"{root}/{watch['id']}/assessments", json=body, headers=headers(grant))
    assert first.status_code == 201 and first.json()['id'] == again.json()['id'] and first.json()['actor'] == 'agent'
    read_only = web.post(f'/api/studio/projects/{pid}/agent-grants', json={'permission': 'read', 'label': 'reader'}).json()
    assert web.post(f"{root}/{watch['id']}/assessments", json={**body, 'idempotency_key': 'agent-2'},
                    headers=headers(read_only)).status_code == 403
    page = web.get(f'/api/studio/projects/{pid}/questions').json()['questions'][0]
    assert page['latest']['agent']['note'] == 'Agent 研究：新客户已签约' and event in page['events']


def test_a_system_figure_missing_from_cited_events_is_not_stored(tmp_path):
    store, monitor, ledger, pid = setup(tmp_path)
    watch = monitor.add_watchpoint(pid, '量产', '量产', ['q'], 'c', True)
    hit_event(store, monitor, pid, watch['id'], '合成光电月交付突破 5 万台')

    class Inventing(Advisor):
        def assess(self, llm, project, questions):
            q = questions[0]
            return [{'id': q['id'], 'status': 'progress', 'note': '交付 5 万台，营收增长 38.4%', 'event_ids': [q['events'][0]['id']]}]

    assert ledger.assess(Inventing(), LLM, pid, {}) == 0  # 38.4 is not in the cited event
    hit_event(store, monitor, pid, watch['id'], '合成光电营收增长 38.4%')

    class Grounded(Advisor):
        def assess(self, llm, project, questions):
            q = questions[0]
            return [{'id': q['id'], 'status': 'progress', 'note': '营收增长 38.4%', 'event_ids': [e['id'] for e in q['events']]}]

    assert ledger.assess(Grounded(), LLM, pid, {}) == 1
