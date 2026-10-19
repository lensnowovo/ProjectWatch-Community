"""M2 independent acceptance regressions. Synthetic databases; no network/model calls."""
import socket
from contextlib import closing

import pytest
from test_studio_application_parity import headers, setup_app
from test_studio_questions import LLM, hit_event, setup

from project_watch.studio.questions import Assessment, QuestionLedger


@pytest.fixture
def version_offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Question-version regressions must stay offline')
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(socket, 'getaddrinfo', forbidden)


def test_human_latest_survives_fifty_system_updates(tmp_path):
    store, monitor, ledger, pid = setup(tmp_path)
    watch = monitor.add_watchpoint(pid, '交付进展', '交付', ['q'], '交付', False)
    event = hit_event(store, monitor, pid, watch['id'], '合成光电交付')
    human = ledger.record(pid, watch['id'], 'human', Assessment(status='setback', note='人的判断应保留'))
    for index in range(51):
        ledger.record(pid, watch['id'], 'system', Assessment(status='progress', note='系统更新',
                      event_ids=(event,), idempotency_key=f'refresh-{index}'))
    view = ledger.view(pid)[0]
    assert view['latest']['human']['id'] == human['id']
    assert view['history_count'] == 52


def test_public_agent_cannot_read_owner_notes(tmp_path):
    app, web, pid, grant = setup_app(tmp_path)
    monitor, ledger = app.state.studio_pipeline.monitor, app.state.studio_pipeline.questions
    watch = monitor.add_watchpoint(pid, '公开量产进展', '量产', ['q'], '量产', False)
    marker = 'SYNTHETIC_INTERNAL_COMMITTEE_NOTE'
    ledger.record(pid, watch['id'], 'human', Assessment(status='setback', note=marker))
    reply = web.get(f'/api/studio/agent/projects/{pid}/questions', headers=headers(grant))
    assert reply.status_code == 200 and marker not in reply.text


def test_unprocessed_seventh_event_is_not_lost(tmp_path):
    store, monitor, ledger, pid = setup(tmp_path)
    watch = monitor.add_watchpoint(pid, '交付', '交付', ['q'], '交付', False)
    events = {hit_event(store, monitor, pid, watch['id'], f'合成光电交付消息 {i}') for i in range(7)}

    class Advisor:
        def assess(self, llm, project, questions):
            return [{'id': q['id'], 'status': 'progress', 'note': '交付有进展',
                     'event_ids': [e['id'] for e in q['events']]} for q in questions]

    assert ledger.assess(Advisor(), LLM, pid, {}) == 2
    assert events == {e for row in ledger.history(watch['id']) for e in row['event_ids']}
    assert ledger.assess(Advisor(), LLM, pid, {}) == 0
    assert ledger.new_events(pid, watch['id']) == []


@pytest.mark.parametrize(('source', 'note', 'expected'), [
    ('客户编号138，未披露收入增长率', '营收增长38%', False),
    ('2026年没有销量数据', '出货26万台', False),
    ('收入增长38.4%', '收入增长38%', False),
    ('收入增长38.4%', '收入增长38.4%', True),
    ('出货38万台', '收入增长38%', False),
    ('客户代号AT38', '收入增长38%', False),
    ('出货5万台', '出货9万台', False),
    ('出货5万台', '出货5万台', True),
    ('未实现增长38%', '增长38%', False),
    ('未实现增长38%', '未实现增长38%', True),
    ('未实现增长38%', '未实现增长39%', False),
    ('收入增长38.4％', '收入增长38.4%', True),
    ('交付15,000台', '交付15000台', True),
])
def test_numeric_tokens_are_not_substrings(tmp_path, source, note, expected):
    store, monitor, ledger, pid = setup(tmp_path)
    watch = monitor.add_watchpoint(pid, '收入与交付', '指标', ['q'], '指标', False)
    event = hit_event(store, monitor, pid, watch['id'], f'合成光电{source}')
    assert ledger._numbers_supported(pid, note, (event,)) is expected


def test_number_and_unit_cannot_be_joined_across_fields(tmp_path):
    store, monitor, ledger, pid = setup(tmp_path)
    watch = monitor.add_watchpoint(pid, '收入', '收入', ['q'], '收入', False)
    event = hit_event(store, monitor, pid, watch['id'], '合成编号38')
    with closing(store._connect()) as db, db:
        db.execute("UPDATE studio_events SET summary='% 的概念说明' WHERE id=?", (event,))
        db.execute("UPDATE studio_items SET summary='',snippet='',body='' WHERE event_id=?", (event,))
    assert not ledger._numbers_supported(pid, '营收增长38%', (event,))


def test_disclosure_filters_notes_counts_events_and_question_text(tmp_path):
    app, web, pid, grant = setup_app(tmp_path)
    monitor, ledger = app.state.studio_pipeline.monitor, app.state.studio_pipeline.questions
    public = monitor.add_watchpoint(pid, '公开产品交付', '交付', ['q'], '交付', False, visibility='public')
    private = monitor.add_watchpoint(pid, 'SYNTHETIC_PRIVATE_QUESTION', '内部', ['内部检索词'], '内部', False)
    ledger.record(pid, public['id'], 'human', Assessment(status='progress', note='公开判断', visibility='public'))
    event = hit_event(app.state.studio_store, monitor, pid, public['id'], '合成事件')
    ledger.record(pid, public['id'], 'human', Assessment(status='setback', note='SYNTHETIC_PRIVATE_NOTE', event_ids=(event,)))
    ledger.record(pid, private['id'], 'human', Assessment(status='progress', note='SYNTHETIC_PRIVATE_TEXT'))
    route = f'/api/studio/agent/projects/{pid}/questions'
    reply = web.get(route, headers=headers(grant))
    [question] = reply.json()['questions']
    assert question['id'] == public['id'] and question['history_count'] == 1
    assert question['latest']['human']['note'] == '公开判断' and question['events'] == {}
    assert 'SYNTHETIC_PRIVATE' not in reply.text
    private_grant = web.post(f'/api/studio/projects/{pid}/agent-grants', json={
        'permission': 'read', 'label': 'internal-reader', 'allow_internal': True,
    }).json()
    private_reply = web.get(route, headers=headers(private_grant)).json()['questions']
    assert len(private_reply) == 2 and private_reply[0]['history_count'] == 2
    assert len(ledger.view(pid)) == 2  # owner loses no history


def test_public_agent_cannot_write_private_question_or_internal_assessment(tmp_path):
    app, web, pid, grant = setup_app(tmp_path)
    monitor = app.state.studio_pipeline.monitor
    public = monitor.add_watchpoint(pid, '公开产品', '产品', ['q'], '产品', False, visibility='public')
    private = monitor.add_watchpoint(pid, '内部问题', '内部', ['q'], '内部', False)
    root = f'/api/studio/agent/projects/{pid}/questions'
    body = {'status': 'progress', 'note': '公开助手结果', 'idempotency_key': 'agent-write'}
    assert web.post(f"{root}/{private['id']}/assessments", json=body, headers=headers(grant)).status_code == 404
    assert web.post(f"{root}/{public['id']}/assessments", json={**body, 'visibility': 'internal'},
                    headers=headers(grant)).status_code == 403
    reply = web.post(f"{root}/{public['id']}/assessments", json=body, headers=headers(grant))
    assert reply.status_code == 201 and reply.json()['visibility'] == 'public'


def test_public_agent_cannot_replay_an_internal_idempotency_receipt(tmp_path):
    app, web, pid, grant = setup_app(tmp_path)
    monitor, ledger = app.state.studio_pipeline.monitor, app.state.studio_pipeline.questions
    watch = monitor.add_watchpoint(pid, '公开产品', '产品', ['q'], '产品', False, visibility='public')
    ledger.record(pid, watch['id'], 'agent', Assessment(
        status='setback', note='SYNTHETIC_PRIVATE_AGENT_NOTE', idempotency_key='known-key', visibility='internal'))
    reply = web.post(f"/api/studio/agent/projects/{pid}/questions/{watch['id']}/assessments",
                     json={'status': 'progress', 'idempotency_key': 'known-key'}, headers=headers(grant))
    assert reply.status_code == 403 and 'SYNTHETIC_PRIVATE_AGENT_NOTE' not in reply.text


def test_history_pagination_can_reach_human_and_agent_rows(tmp_path):
    app, web, pid, _grant = setup_app(tmp_path)
    monitor, ledger = app.state.studio_pipeline.monitor, app.state.studio_pipeline.questions
    watch = monitor.add_watchpoint(pid, '公开问题', '问题', ['q'], '问题', False)
    for index in range(55):
        ledger.record(pid, watch['id'], 'human', Assessment(status='progress', note=f'合成判断 {index}'))
    root = f"/api/studio/projects/{pid}/questions/{watch['id']}/history"
    first, last = web.get(root).json(), web.get(root, params={'offset': 50, 'limit': 50}).json()
    assert len(first) == 50 and len(last) == 5
    assert not {r['id'] for r in first} & {r['id'] for r in last}
    assert ledger.view(pid)[0]['history_count'] == 55
    assert web.get(root, params={'limit': 201}).status_code == 422
    assert web.get(root, params={'offset': -1}).status_code == 422


class CitingAdvisor:
    def assess(self, llm, project, questions):
        return [{'id': q['id'], 'status': 'progress', 'note': '已有交付消息',
                 'event_ids': [e['id'] for e in q['events']]} for q in questions]


def test_revised_event_and_new_hit_can_be_assessed_again(tmp_path):
    store, monitor, ledger, pid = setup(tmp_path)
    watch = monitor.add_watchpoint(pid, '交付', '交付', ['q'], '交付', False)
    event = hit_event(store, monitor, pid, watch['id'], '合成交付')
    assert ledger.assess(CitingAdvisor(), LLM, pid, {}) == 1
    assert ledger.assess(CitingAdvisor(), LLM, pid, {}) == 0
    with closing(store._connect()) as db, db:
        db.execute("UPDATE studio_events SET summary='修订后的合成消息',updated_at='2099-01-01T00:00:00' WHERE id=?", (event,))
    assert ledger.assess(CitingAdvisor(), LLM, pid, {}) == 1
    assert len(ledger.history(watch['id'])) == 2
    with closing(store._connect()) as db, db:
        db.execute('DELETE FROM studio_question_consumed_hits')
    assert ledger.assess(CitingAdvisor(), LLM, pid, {}) == 1
    assert len(ledger.history(watch['id'])) == 2  # identical input replay is idempotent


def test_known_empty_answer_consumes_only_sent_inputs_unknown_consumes_none(tmp_path):
    from project_watch.studio.http import HttpError

    store, monitor, ledger, pid = setup(tmp_path)
    watch = monitor.add_watchpoint(pid, '交付', '交付', ['q'], '交付', False)
    for i in range(7):
        hit_event(store, monitor, pid, watch['id'], f'合成交付 {i}')

    class Unknown:
        def assess(self, *args):
            raise HttpError('request_outcome_unknown')

    with pytest.raises(HttpError):
        ledger.assess(Unknown(), LLM, pid, {})
    assert len(ledger.new_events(pid, watch['id'])) == 6

    class Declined:
        def assess(self, *args):
            return []

    assert ledger.assess(Declined(), LLM, pid, {}) == 0
    assert ledger.new_events(pid, watch['id']) == []
    hit_event(store, monitor, pid, watch['id'], '新的合成交付')
    assert ledger.assess(CitingAdvisor(), LLM, pid, {}) == 1
    assert ledger.new_events(pid, watch['id']) == []


def test_changed_input_during_advisor_call_cannot_commit_judgement(tmp_path):
    store, monitor, ledger, pid = setup(tmp_path)
    watch = monitor.add_watchpoint(pid, '交付', '交付', ['q'], '交付', False)
    event = hit_event(store, monitor, pid, watch['id'], '合成交付')

    class Mutating(CitingAdvisor):
        def assess(self, *args):
            with closing(store._connect()) as db, db:
                db.execute("UPDATE studio_events SET updated_at='2099-01-01' WHERE id=?", (event,))
            return super().assess(*args)

    with pytest.raises(ValueError, match='input changed'):
        ledger.assess(Mutating(), LLM, pid, {})
    assert ledger.history(watch['id']) == []
    assert ledger.new_events(pid, watch['id'])


@pytest.mark.parametrize('field,value', [
    ('text', '关注融资而非交付'),
    ('label', '融资'),
    ('queries', '["融资"]'),
    ('criteria', '融资款实际到账'),
    ('visibility', 'internal'),
])
@pytest.mark.parametrize('declined', [False, True])
@pytest.mark.usefixtures('version_offline')
def test_changed_question_during_public_advisor_call_keeps_inputs_pending(tmp_path, field, value, declined):
    store, monitor, ledger, pid = setup(tmp_path)
    watch = monitor.add_watchpoint(pid, '交付', '交付', ['交付'], '交付', False, visibility='public')
    event = hit_event(store, monitor, pid, watch['id'], '合成交付消息')
    original = ledger.record(pid, watch['id'], 'human', Assessment(status='setback', note='人的原判断'))

    class Mutating(CitingAdvisor):
        def assess(self, *args):
            with closing(store._connect()) as db, db:
                # Fixed parametrized column names only, no caller-controlled SQL.
                db.execute(f'UPDATE studio_watchpoints SET {field}=? WHERE id=?', (value, watch['id']))
            return [] if declined else super().assess(*args)

    with pytest.raises(ValueError, match='question input changed'):
        ledger.assess(Mutating(), LLM, pid, {})
    assert ledger.history(watch['id']) == [original]
    assert [row['id'] for row in ledger.new_events(pid, watch['id'])] == [event]
    with closing(store._connect()) as db:
        assert db.execute('SELECT count(*) FROM studio_question_consumed_hits').fetchone()[0] == 0


@pytest.mark.usefixtures('version_offline')
def test_question_change_after_reply_validation_is_rechecked_inside_write(tmp_path, monkeypatch):
    store, monitor, ledger, pid = setup(tmp_path)
    watch = monitor.add_watchpoint(pid, '交付', '交付', ['交付'], '交付', False, visibility='public')
    event = hit_event(store, monitor, pid, watch['id'], '合成交付消息')

    def late_change(*args):
        with closing(store._connect()) as db, db:
            db.execute('UPDATE studio_watchpoints SET criteria=? WHERE id=?', ('融资到账', watch['id']))
        return True

    monkeypatch.setattr(ledger, '_numbers_supported', late_change)
    with pytest.raises(ValueError, match='question input changed'):
        ledger.assess(CitingAdvisor(), LLM, pid, {})
    assert ledger.history(watch['id']) == []
    assert ledger.new_events(pid, watch['id'])[0]['id'] == event


@pytest.mark.usefixtures('version_offline')
def test_alert_toggle_and_new_human_note_do_not_invalidate_system_response(tmp_path):
    store, monitor, ledger, pid = setup(tmp_path)
    watch = monitor.add_watchpoint(pid, '交付', '交付', ['交付'], '交付', False, visibility='public')
    hit_event(store, monitor, pid, watch['id'], '合成交付消息')

    class HumanEdited(CitingAdvisor):
        def assess(self, *args):
            monitor.update_watchpoint(watch['id'], alert=True)
            ledger.record(pid, watch['id'], 'human', Assessment(status='setback', note='请求期间新的人工意见'))
            return super().assess(*args)

    assert ledger.assess(HumanEdited(), LLM, pid, {}) == 1
    latest = ledger.view(pid)[0]['latest']
    assert latest['human']['note'] == '请求期间新的人工意见'
    assert latest['system']['status'] == 'progress'
    assert ledger.new_events(pid, watch['id']) == []


@pytest.mark.usefixtures('version_offline')
def test_changed_question_has_distinct_system_receipt_for_same_events(tmp_path):
    store, monitor, ledger, pid = setup(tmp_path)
    watch = monitor.add_watchpoint(pid, '交付', '交付', ['交付'], '交付', False, visibility='public')
    hit_event(store, monitor, pid, watch['id'], '合成交付消息')
    assert ledger.assess(CitingAdvisor(), LLM, pid, {}) == 1
    before = ledger.history(watch['id'])[0]
    with closing(store._connect()) as db, db:
        db.execute('UPDATE studio_watchpoints SET criteria=? WHERE id=?', ('交付数量与阶段', watch['id']))
        # Explicit replay of the same evidence under a new question, not an
        # automatic paid retry or a UI feature introduced by this fix.
        db.execute('DELETE FROM studio_question_consumed_hits WHERE watch_id=?', (watch['id'],))
    assert ledger.assess(CitingAdvisor(), LLM, pid, {}) == 1
    after = ledger.history(watch['id'])[0]
    assert after['id'] != before['id'] and after['fingerprint'] != before['fingerprint']
    assert len(ledger.history(watch['id'])) == 2
    assert ledger.assess(CitingAdvisor(), LLM, pid, {}) == 0


@pytest.mark.parametrize('declined', [False, True])
@pytest.mark.usefixtures('version_offline')
def test_removed_question_during_response_does_not_leave_orphan_consumption(tmp_path, declined):
    store, monitor, ledger, pid = setup(tmp_path)
    watch = monitor.add_watchpoint(pid, '交付', '交付', ['交付'], '交付', False, visibility='public')
    hit_event(store, monitor, pid, watch['id'], '合成交付消息')

    class Removed(CitingAdvisor):
        def assess(self, *args):
            monitor.remove_watchpoint(watch['id'])
            return [] if declined else super().assess(*args)

    with pytest.raises(ValueError, match='question input changed'):
        ledger.assess(Removed(), LLM, pid, {})
    assert ledger.history(watch['id']) == []
    with closing(store._connect()) as db:
        assert db.execute('SELECT count(*) FROM studio_question_consumed_hits').fetchone()[0] == 0


def test_legacy_migration_does_not_mark_uncited_inputs_consumed(tmp_path):
    store, monitor, ledger, pid = setup(tmp_path)
    watch = monitor.add_watchpoint(pid, '交付', '交付', ['q'], '交付', False)
    first = hit_event(store, monitor, pid, watch['id'], '合成交付一')
    second = hit_event(store, monitor, pid, watch['id'], '合成交付二')
    ledger.record(pid, watch['id'], 'system', Assessment(status='progress', event_ids=(first,)))
    with closing(store._connect()) as db, db:
        db.execute('DROP TABLE studio_question_consumed_hits')
        db.execute("DELETE FROM studio_state WHERE key='question_hit_migration_v1'")
        db.execute('ALTER TABLE studio_watchpoints DROP COLUMN visibility')
        db.execute('ALTER TABLE studio_question_assessments DROP COLUMN visibility')
    reopened = QuestionLedger(store, monitor)
    assert {e['id'] for e in reopened.new_events(pid, watch['id'])} == {second}
    assert reopened.view(pid, include_internal=False) == []
    assert reopened.history(watch['id'])[0]['visibility'] == 'internal'
    assert reopened.history(watch['id'])[0]['event_ids'] == [first]


def test_private_profile_suggestions_are_not_shared_with_public_grants(tmp_path):
    from test_studio_questions import Advisor

    _store, _monitor, ledger, pid = setup(tmp_path)
    ledger.suggest(Advisor(), LLM, pid, {}, {}, {'summary': '合成内部资料'})
    assert ledger.view(pid, include_internal=False) == []
    assert len(ledger.view(pid)) == 2
