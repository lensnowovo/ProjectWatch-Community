"""Synthetic restart/replay tests for exploration; no provider calls."""
import socket
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from project_watch.studio import plan_replan
from project_watch.studio.app import create_studio_app
from project_watch.studio.exploration import Explorations
from project_watch.studio.http import HttpError
from project_watch.studio.monitor import MonitorStore


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail('no network allowed in exploration replay')
    # Windows asyncio uses a local socketpair to run TestClient. Block real
    # resolution/HTTP connections, not its internal wake-up pipe.
    monkeypatch.setattr(socket, 'create_connection', blocked)
    monkeypatch.setattr(socket, 'getaddrinfo', blocked)


@pytest.fixture
def env(tmp_path):
    p = create_studio_app(tmp_path, start_worker=False).state.studio_pipeline
    pid = p.repository.create_project('合成探索公司').id
    p.store.upsert_project(pid, {'legal_name': '合成探索公司'}, consent=True)
    plan = p.monitoring_plan
    core = plan.add(pid, {'text': '公司常规进展', 'queries': ['常规新闻'], 'cadence': 'high'}, actor='human', origin='human')
    topic = plan.add(pid, {'text': '合成资产交付是否完成', 'queries': ['合成资产交付'], 'objects': ['合成资产'],
                           'tier': 'topic', 'parent_id': core['id'], 'exit_rule': '取得交付完成的公开记录'},
                     actor='system', origin='system')
    loop = plan.explorations
    loop.refresh(pid, p.questions)
    return p, pid, plan, loop, topic, core


def event(p, pid, watch, key, *, title=None, routed=True):
    title = title or '合成资产交付进展' + key
    p.store.add_items(pid, 'company', [{'key': key, 'title': title, 'url': 'https://example.invalid/' + key,
                                     'source': 'synthetic', 'snippet': title, 'published_at': None}])
    item = next(i for i in p.store.untriaged(pid) if i['title'] == title)
    p.store.create_event(pid, {'title': title, 'importance': 'notable', 'category': 'company'}, [item['id']])
    if routed:
        p.monitor.add_hits([(watch['id'], item['id'])])
    return item


def round_(p, pid, plan, loop, watch, cycle):
    loop.refresh(pid, p.questions)
    if not loop.runnable(watch, cycle) or not loop.start(watch, cycle):
        return False
    assert plan.mark_query(watch['id'], watch['queries'][0], success=True, lifecycle_version=watch['lifecycle_version'])
    loop.collected(watch['id'], cycle)
    loop.finish(pid, cycle, p.questions)
    return True


def test_ten_round_replay_stops_and_wakes_without_stopping_core(env):
    p, pid, plan, loop, topic, core = env
    trace = []
    for n in range(1, 11):
        if n == 5:
            event(p, pid, topic, 'new')
        if n == 6:
            # Restart the control store and replay identical input.
            loop = Explorations(MonitorStore(p.store))
        if n == 9:
            event(p, pid, topic, 'unrelated', routed=False)
        if n == 10:
            event(p, pid, topic, 'newer')
        round_(p, pid, plan, loop, topic, str(n))
        state = loop.view(pid)[topic['id']]
        trace.append((state['state'], state['no_gain'], state['generation']))
        assert core['id'] in {w['id'] for w in plan.due(pid)}
        before = loop.view(pid)
        loop.finish(pid, str(n), p.questions)
        assert loop.view(pid) == before
    assert trace == [('active', 1, 1), ('active', 2, 1), ('stopped', 3, 1), ('stopped', 3, 1),
                     ('active', 0, 2), ('active', 1, 2), ('active', 2, 2), ('stopped', 3, 2),
                     ('stopped', 3, 2), ('active', 0, 3)]
    with p.monitor._db() as db:
        assert db.execute("SELECT COUNT(*) FROM studio_exploration_history WHERE reason='new_evidence'").fetchone()[0] == 2


def test_event_cadence_runs_once_per_new_evidence(env):
    p, pid, plan, loop, topic, _ = env
    topic = plan.update(topic['id'], {'cadence': 'event'}, actor='human')
    loop.refresh(pid, p.questions)
    assert topic['id'] not in {w['id'] for w in plan.due(pid)}
    event(p, pid, topic, 'first')
    assert round_(p, pid, plan, loop, topic, 'one')
    assert loop.view(pid)[topic['id']]['reason'] == 'waiting_event'
    assert not round_(p, pid, plan, loop, topic, 'two')
    event(p, pid, topic, 'second')
    assert round_(p, pid, plan, loop, topic, 'three')


def test_timestamps_reworded_summary_and_reposts_do_not_wake(env):
    p, pid, _plan, loop, topic, _ = env
    event(p, pid, topic, 'source', title='合成资产完成交付')
    loop.refresh(pid, p.questions)
    loop.stop(topic, 'answered')
    before = loop.view(pid)
    with p.store._connect() as db, db:
        db.execute("UPDATE studio_events SET summary='模型换了说法',updated_at='2099-01-01'")
    event(p, pid, topic, 'repost', title='合成资产完成交付')
    loop.refresh(pid, p.questions)
    assert loop.view(pid) == before


@pytest.mark.parametrize('reason', ['source_failure', 'waiting_recovery'])
def test_failed_round_is_not_exhaustion_and_unknown_only_resumes_same_cycle(env, reason):
    p, pid, plan, loop, topic, _ = env
    assert loop.start(topic, 'original')
    loop.hold(topic, reason)
    for n in range(10):
        loop.finish(pid, str(n), p.questions)
    assert loop.view(pid)[topic['id']]['no_gain'] == 0
    assert loop.runnable(topic, 'other') == (reason == 'source_failure')
    assert loop.runnable(topic, 'original')
    event(p, pid, topic, 'during-hold')
    loop.refresh(pid, p.questions)
    if reason == 'waiting_recovery':
        assert loop.view(pid)[topic['id']]['reason'] == 'waiting_recovery'
        assert round_(p, pid, plan, loop, topic, 'original')
        assert loop.view(pid)[topic['id']]['state'] == 'active'


def test_partial_collection_cannot_count_as_a_round(env):
    p, pid, plan, loop, topic, _ = env
    topic = plan.update(topic['id'], {'queries': ['第一词', '第二词']}, actor='human')
    loop.refresh(pid, p.questions)
    assert loop.start(topic, 'partial')
    assert not plan.mark_query(topic['id'], '第一词', success=True)
    loop.finish(pid, 'partial', p.questions)
    assert loop.view(pid)[topic['id']]['no_gain'] == 0
    assert [q for w, q in plan.query_jobs(pid, 30) if w['id'] == topic['id']] == ['第二词']
    assert plan.mark_query(topic['id'], '第二词', success=True)
    loop.collected(topic['id'], 'partial')
    loop.finish(pid, 'partial', p.questions)
    assert loop.view(pid)[topic['id']]['no_gain'] == 1


@pytest.mark.parametrize('edit', [{'status': 'retired'}, {'queries': ['新的问题词']}, {'tier': 'core'}, {'text': '改问另一件事情'}])
def test_stale_cycle_cannot_complete_changed_or_retired_question(env, edit):
    p, pid, plan, loop, topic, _ = env
    assert loop.start(topic, 'old')
    loop.collected(topic['id'], 'old')
    plan.update(topic['id'], edit, actor='human')
    loop.finish(pid, 'old', p.questions)
    assert not loop.stop(topic, 'answered')
    assert loop.view(pid)[topic['id']]['no_gain'] == 0


def test_new_evidence_never_restores_human_retired_topic(env):
    p, pid, plan, loop, topic, _ = env
    plan.update(topic['id'], {'status': 'retired'}, actor='human')
    before = loop.view(pid)
    event(p, pid, topic, 'ignored')
    loop.refresh(pid, p.questions)
    assert loop.view(pid) == before
    assert topic['id'] not in {w['id'] for w in plan.due(pid)}


def test_old_generation_cannot_stop_newly_awakened_exploration(env):
    p, pid, _plan, loop, topic, _ = env
    loop.start(topic, 'old')
    loop.collected(topic['id'], 'old')
    loop.stop(topic, 'answered')
    event(p, pid, topic, 'wake')
    loop.refresh(pid, p.questions)
    assert not loop.stop(topic, 'answered', generation=1)
    loop.finish(pid, 'old', p.questions)
    assert loop.view(pid)[topic['id']]['state'] == 'active'
    assert loop.view(pid)[topic['id']]['no_gain'] == 0


def test_finish_requires_own_real_evidence_and_exit_rule(env):
    p, pid, plan, loop, topic, _ = env
    event(p, pid, topic, 'answer')
    loop.refresh(pid, p.questions)
    gathered = plan_replan.gather(p, pid, since='2000-01-01', trigger='external')
    key = next(k for k, w in gathered['items'].items() if w['id'] == topic['id'])
    gathered['numbered']['G1'] = {'kind': 'gap', 'text': '还不知道', 'watch_ids': [topic['id']]}
    change = {'action': 'finish', 'item': key, 'text': '合成资产已交付', 'why': '交付记录回答本题', 'refs': ['G1']}
    assert plan_replan.apply(plan, pid, [change], gathered, trigger='external')['dropped'] == 1
    change['refs'] = ['E1']
    assert plan_replan.apply(plan, pid, [change], gathered, trigger='external')['applied'] == 1
    assert loop.view(pid)[topic['id']]['reason'] == 'answered'
    assert plan._get(pid, topic['id'])['status'] != 'retired'


def test_sleeping_topic_is_not_sent_to_reviser(env):
    p, pid, _plan, loop, topic, _ = env
    event(p, pid, topic, 'old')
    loop.refresh(pid, p.questions)
    loop.stop(topic, 'no_gain')
    gathered = plan_replan.gather(p, pid, since='2000-01-01', trigger='external')
    assert topic['id'] not in {w['id'] for w in gathered['items'].values()}


@pytest.mark.parametrize('error,expected', [('http_failure', 'source_failure'), ('request_outcome_unknown', 'waiting_recovery')])
def test_real_collection_path_records_failure_without_counting_empty_result(env, monkeypatch, error, expected):
    p, pid, plan, loop, topic, _ = env
    def failed(*args, **kwargs):
        raise HttpError(error)
    monkeypatch.setattr('project_watch.studio.pipeline._news', failed)
    # Isolate the topic from the routine query; no provider objects or API keys.
    monkeypatch.setattr(type(plan), 'query_jobs', lambda *a, **kw: [(topic, topic['queries'][0])])
    if error == 'request_outcome_unknown':
        with pytest.raises(HttpError):
            p._collect_monitor(pid, {}, SimpleNamespace(has_search=False), lambda *_: None)
    else:
        p._collect_monitor(pid, {}, SimpleNamespace(has_search=False), lambda *_: None)
    row = loop.view(pid)[topic['id']]
    assert row['reason'] == expected and row['no_gain'] == 0


def test_real_collection_and_replan_finish_one_cycle(env, monkeypatch):
    p, pid, _plan, loop, topic, _ = env
    monkeypatch.setattr('project_watch.studio.pipeline._news', lambda *a, **kw: [])
    p._execution_state.current = SimpleNamespace(job={'id': 'synthetic-cycle'})
    try:
        p._collect_monitor(pid, {}, SimpleNamespace(has_search=False), lambda *_: None)
        p.plan_reviser = SimpleNamespace(since_last=lambda *_: '', mark=lambda *_: None, run=lambda *a, **kw: {'skipped': True})
        p._replan(pid, SimpleNamespace(ready=True), lambda *_: None, trigger='external')
        assert loop.view(pid)[topic['id']]['no_gain'] == 1
        p._replan(pid, SimpleNamespace(ready=True), lambda *_: None, trigger='external')
        assert loop.view(pid)[topic['id']]['no_gain'] == 1
    finally:
        p._execution_state.current = None


@pytest.mark.parametrize('incomplete', [False, True])
def test_rejected_proposal_does_not_block_complete_round_accounting(env, incomplete):
    p, pid, plan, loop, topic, _ = env
    p.plan_reviser = SimpleNamespace(since_last=lambda *_: '', mark=lambda *_: None,
        run=lambda *a, **kw: {'dropped': 1, 'incomplete': int(incomplete)})
    try:
        for n in range(3):
            cycle = f'rejected-{n}'
            assert loop.start(topic, cycle)
            assert plan.mark_query(topic['id'], topic['queries'][0], success=True)
            loop.collected(topic['id'], cycle)
            p._execution_state.current = SimpleNamespace(job={'id': cycle})
            p._replan(pid, SimpleNamespace(ready=True), lambda *_: None, trigger='external')
            p._replan(pid, SimpleNamespace(ready=True), lambda *_: None, trigger='external')
        state = loop.view(pid)[topic['id']]
        assert state['no_gain'] == (0 if incomplete else 3)
        assert state['state'] == ('active' if incomplete else 'stopped')
    finally:
        p._execution_state.current = None


@pytest.mark.parametrize('importance', ['routine', 'notable', 'major'])
def test_active_topic_can_answer_from_older_routed_events(env, importance):
    p, pid, plan, loop, topic, core = env
    item = event(p, pid, topic, 'historic', title='合成资产已交付，客户甲已采用')
    with p.store._connect() as db, db:
        eid = db.execute('SELECT event_id FROM studio_items WHERE id=?', (item['id'],)).fetchone()[0]
        db.execute('UPDATE studio_events SET created_at=?,event_at=?,importance=?,summary=? WHERE id=?',
                   ('2020-01-01', '2020-01-01', importance, '合成资产已交付，客户甲已采用', eid))
    loop.refresh(pid, p.questions)
    gathered = plan_replan.gather(p, pid, since='2099-01-01', trigger='external')
    assert gathered is not None
    key = next(k for k, v in gathered['numbered'].items() if v.get('id') == eid)
    wkey = next(k for k, v in gathered['items'].items() if v['id'] == topic['id'])
    assert '既有专题依据' in gathered['text']
    result = plan_replan.apply(plan, pid, [{'action': 'finish', 'item': wkey, 'refs': [key],
        'text': '合成资产已交付，客户甲已采用', 'why': '交付及采用记录满足退出条件'}], gathered,
        trigger='external', numbers_ok=lambda note, ids, texts: p.questions._numbers_supported(pid, note, ids, texts))
    assert result['applied'] == 1
    assert loop.view(pid)[topic['id']]['reason'] == 'answered'
    due = {w['id'] for w in plan.due(pid, cycle='after-answer')}
    assert topic['id'] not in due and core['id'] in due
    # Reopening the control store preserves the real stopped state.
    assert Explorations(MonitorStore(p.store)).view(pid)[topic['id']]['state'] == 'stopped'
    loop.refresh(pid, p.questions)
    assert loop.view(pid)[topic['id']]['state'] == 'stopped'
    event(p, pid, topic, 'later', title='合成资产出现新的客户验收记录')
    loop.refresh(pid, p.questions)
    assert loop.view(pid)[topic['id']]['generation'] == 2
    assert topic['id'] in {w['id'] for w in plan.due(pid, cycle='new-evidence')}


def test_topic_history_excludes_dismissed_unrouted_and_other_project_events(env):
    p, pid, _, loop, topic, _ = env
    item = event(p, pid, topic, 'dismissed')
    event(p, pid, topic, 'unrouted', routed=False)
    other_pid = p.repository.create_project('另一合成公司').id
    p.store.upsert_project(other_pid, {'legal_name': '另一合成公司'}, consent=True)
    # Even a malformed cross-project hit must not leak evidence into this topic.
    foreign = event(p, other_pid, topic, 'foreign', routed=False)
    with p.store._connect() as db, db:
        db.execute("UPDATE studio_items SET status='dismissed' WHERE id=?", (item['id'],))
        db.execute('INSERT INTO studio_watch_hits(watch_id,item_id,created_at) VALUES (?,?,?)',
                   (topic['id'], foreign['id'], '2020-01-01'))
    loop.refresh(pid, p.questions)
    assert plan_replan.gather(p, pid, since='2099-01-01', trigger='external') is None


@pytest.mark.parametrize('source,note,expected', [
    ('2025 全年交付160万台', '2025年交付160万台', True),
    ('2025年度交付160万台', '2025年交付160万台', True),
    ('2025年交付160万台', '2025 全年交付160万台', True),
    ('交付2025台', '2025年交付', False),
    ('2025 全年交付160万台', '2024年交付160万台', False),
    ('2025 全年交付160万台', '2025年交付160亿元', False),
    ('装机182436颗', '装机18.24万颗', False),
    ('药物已获批', '药物2024年8月获批', False),
])
def test_topic_numeric_calendar_wording_is_lossless(env, source, note, expected):
    p, pid, *_ = env
    assert p.questions._numbers_supported(pid, note, [], [source]) is expected


@pytest.mark.parametrize('missing', ['model', 'plugin'])
def test_optional_reviser_absence_still_closes_complete_empty_rounds(env, missing):
    p, pid, plan, loop, topic, _ = env
    if missing == 'plugin':
        p.plan_reviser = None
    try:
        for n in range(3):
            cycle = f'no-reviser-{n}'
            assert loop.start(topic, cycle)
            assert plan.mark_query(topic['id'], topic['queries'][0], success=True)
            loop.collected(topic['id'], cycle)
            p._execution_state.current = SimpleNamespace(job={'id': cycle})
            p._replan(pid, SimpleNamespace(ready=missing != 'model'), lambda *_: None, trigger='external')
            p._replan(pid, SimpleNamespace(ready=missing != 'model'), lambda *_: None, trigger='external')
            assert loop.view(pid)[topic['id']]['no_gain'] == n + 1
        assert loop.view(pid)[topic['id']]['reason'] == 'no_gain'
    finally:
        p._execution_state.current = None


def test_optional_reviser_absence_does_not_finish_partial_or_unknown_round(env):
    p, pid, _, loop, topic, _ = env
    try:
        assert loop.start(topic, 'partial')
        p._execution_state.current = SimpleNamespace(job={'id': 'partial'})
        p._replan(pid, SimpleNamespace(ready=False), lambda *_: None, trigger='external')
        assert loop.view(pid)[topic['id']]['no_gain'] == 0
        loop.collected(topic['id'], 'partial')
        loop.hold_cycle(pid, 'partial', 'waiting_recovery')
        p._replan(pid, SimpleNamespace(ready=False), lambda *_: None, trigger='external')
        assert loop.view(pid)[topic['id']]['reason'] == 'waiting_recovery'
        assert loop.view(pid)[topic['id']]['no_gain'] == 0
    finally:
        p._execution_state.current = None


def test_topic_respects_reduced_cadence_and_one_new_evidence_wake(env):
    from datetime import UTC, datetime, timedelta

    p, pid, plan, loop, topic, _ = env
    topic = plan.update(topic['id'], {'status': 'reduced', 'cadence': 'low'}, actor='human')
    loop.refresh(pid, p.questions)
    plan.mark_run([topic['id']])
    assert topic['id'] not in {w['id'] for w in plan.due(pid)}
    assert topic['id'] in {w['id'] for w in plan.due(pid, at=datetime.now(UTC) + timedelta(days=31))}
    loop.stop(topic, 'no_gain')
    event(p, pid, topic, 'fresh-wake')
    loop.refresh(pid, p.questions)
    assert topic['id'] in {w['id'] for w in plan.due(pid, cycle='wake')}
    assert loop.start(topic, 'wake')
    # An unfinished original cycle continues, but a second new cycle cannot
    # consume the same immediate wake before any query made progress.
    assert topic['id'] in {w['id'] for w in plan.due(pid, cycle='wake')}
    assert topic['id'] not in {w['id'] for w in plan.due(pid, cycle='other')}
    assert plan.mark_query(topic['id'], topic['queries'][0], success=True)
    loop.collected(topic['id'], 'wake')
    loop.finish(pid, 'wake', p.questions)
    assert topic['id'] not in {w['id'] for w in plan.due(pid)}


def test_woken_reduced_topic_can_finish_budget_split_queries_in_next_job(env):
    p, pid, plan, loop, topic, _ = env
    topic = plan.update(topic['id'], {'status': 'reduced', 'cadence': 'low', 'queries': ['first', 'second']}, actor='human')
    loop.refresh(pid, p.questions)
    plan.mark_run([topic['id']])
    loop.stop(topic, 'no_gain')
    event(p, pid, topic, 'split-wake')
    loop.refresh(pid, p.questions)
    assert loop.start(topic, 'budget-one')
    assert not plan.mark_query(topic['id'], 'first', success=True)
    jobs = [(w, q) for w, q in plan.query_jobs(pid, 30, cycle='budget-two') if w['id'] == topic['id']]
    assert [q for w, q in jobs] == ['second']
    assert loop.start(jobs[0][0], 'budget-two')
    assert plan.mark_query(topic['id'], 'second', success=True)
    loop.collected(topic['id'], 'budget-two')
    loop.finish(pid, 'budget-two', p.questions)
    assert topic['id'] not in {w['id'] for w in plan.due(pid, cycle='budget-three')}


def test_old_model_answer_cannot_replace_new_generation(env):
    p, pid, plan, loop, topic, _ = env
    event(p, pid, topic, 'old-answer')
    loop.refresh(pid, p.questions)
    gathered = plan_replan.gather(p, pid, since='2000-01-01', trigger='external')
    key = next(k for k, w in gathered['items'].items() if w['id'] == topic['id'])
    loop.stop(topic, 'answered')
    event(p, pid, topic, 'new-answer')
    loop.refresh(pid, p.questions)
    change = {'action': 'finish', 'item': key, 'text': '过期答案', 'why': '旧依据', 'refs': ['E1']}
    assert plan_replan.apply(plan, pid, [change], gathered, trigger='external')['dropped'] == 1
    assert plan._get(pid, topic['id'])['current_state'] == ''
    assert loop.view(pid)[topic['id']]['state'] == 'active'


def test_depth_boundary_is_recorded_and_does_not_retire_parent(env):
    p, pid, plan, loop, topic, _ = env
    leaf = plan.add(pid, {'text': '合成资产二阶关系', 'objects': ['合成资产'], 'tier': 'topic', 'parent_id': topic['id']},
                    actor='system', origin='system')
    loop.refresh(pid, p.questions)
    event(p, pid, leaf, 'leaf')
    gathered = plan_replan.gather(p, pid, since='2000-01-01', trigger='external')
    key = next(k for k, w in gathered['items'].items() if w['id'] == leaf['id'])
    change = {'action': 'explore', 'from': key, 'text': '不能继续向三阶延伸', 'section': 'peers', 'refs': ['E1'], 'why': '继续探究'}
    assert plan_replan.apply(plan, pid, [change], gathered, trigger='external')['dropped'] == 1
    assert loop.view(pid)[leaf['id']]['reason'] == 'relation_limit'
    assert plan._get(pid, leaf['id'])['status'] != 'retired'


def test_capacity_boundary_is_recorded(env):
    p, pid, plan, loop, topic, _ = env
    for i in range(14):
        plan.add(pid, {'text': f'人工关注点{i}'}, actor='human', origin='human')
    event(p, pid, topic, 'capacity')
    gathered = plan_replan.gather(p, pid, since='2000-01-01', trigger='external')
    key = next(k for k, w in gathered['items'].items() if w['id'] == topic['id'])
    change = {'action': 'explore', 'from': key, 'text': '容量已满不能继续', 'section': 'peers', 'refs': ['E1'], 'why': '继续探究'}
    assert plan_replan.apply(plan, pid, [change], gathered, trigger='external')['dropped'] == 1
    assert loop.view(pid)[topic['id']]['reason'] == 'capacity_limit'


def test_downstream_failure_never_counts_as_no_gain_and_recovers_original_cycle(env):
    p, pid, _plan, loop, topic, _ = env
    loop.start(topic, 'job')
    loop.collected(topic['id'], 'job')
    loop.hold_cycle(pid, 'job', 'waiting_recovery')
    loop.finish(pid, 'job', p.questions)
    assert loop.view(pid)[topic['id']]['no_gain'] == 0
    loop.resume_collected(pid, 'other-job')
    assert loop.view(pid)[topic['id']]['reason'] == 'waiting_recovery'
    loop.resume_collected(pid, 'job')
    assert loop.view(pid)[topic['id']]['state'] == 'active'


@pytest.mark.parametrize('permitted', [True, False])
def test_real_internal_routing_wakes_only_current_permitted_material(tmp_path, permitted):
    from test_studio_question_materials import intake, setup

    app, client, p, pid, watches = setup(tmp_path)
    plan = p.monitoring_plan
    watch = plan.update(watches[0]['id'], {'tier': 'topic'}, actor='human')
    loop = plan.explorations
    loop.refresh(pid, p.questions)
    loop.stop(watch, 'no_gain')
    source = intake(app, client, p, pid, '合成光电交付10台光学模组。', run=False)
    refs, _, ids = p._fragments(pid)
    assert p.questions.route_materials(pid, refs, set(ids), binding=p.model_binding()) > 0
    if not permitted:
        with p.store._connect() as db, db:
            db.execute('INSERT OR REPLACE INTO studio_source_permissions VALUES (?,?,?,?,?)',
                       (source, pid, '', 'synthetic', '2026-10-16'))
    loop.refresh(pid, p.questions)
    assert loop.view(pid)[watch['id']]['state'] == ('active' if permitted else 'stopped')
    before = loop.view(pid)
    loop.refresh(pid, p.questions)
    assert loop.view(pid) == before


def test_new_body_wakes_once_even_when_the_event_id_is_unchanged(env):
    p, pid, _plan, loop, topic, _ = env
    item = event(p, pid, topic, 'body')
    p.store.save_body(item['id'], 'article_body', '合成资产进入试交付阶段。')
    loop.refresh(pid, p.questions)
    loop.stop(topic, 'answered')
    p.store.save_body(item['id'], 'article_body', '合成资产交付已完成并取得验收文件。')
    loop.refresh(pid, p.questions)
    assert loop.view(pid)[topic['id']]['reason'] == 'new_evidence'
    loop.stop(topic, 'answered')
    before = loop.view(pid)
    p.store.save_body(item['id'], 'article_body', '合成资产交付已完成并取得验收文件。')
    loop.refresh(pid, p.questions)
    assert loop.view(pid) == before


def test_concurrent_finish_counts_once(env):
    p, pid, _plan, loop, topic, _ = env
    loop.start(topic, 'same')
    loop.collected(topic['id'], 'same')
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: loop.finish(pid, 'same', p.questions), range(2)))
    assert loop.view(pid)[topic['id']]['no_gain'] == 1


def test_concurrent_wake_creates_only_one_generation(env):
    p, pid, _plan, loop, topic, _ = env
    loop.stop(topic, 'answered')
    event(p, pid, topic, 'simultaneous')
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: loop.refresh(pid, p.questions), range(2)))
    assert loop.view(pid)[topic['id']]['generation'] == 2


def test_failed_attempt_breaks_consecutive_empty_rounds(env):
    p, pid, plan, loop, topic, _ = env
    round_(p, pid, plan, loop, topic, 'one')
    round_(p, pid, plan, loop, topic, 'two')
    loop.hold(topic, 'source_failure')
    assert loop.view(pid)[topic['id']]['no_gain'] == 0
    round_(p, pid, plan, loop, topic, 'three')
    assert loop.view(pid)[topic['id']]['state'] == 'active'


def test_stale_parent_generation_cannot_add_descendant(env):
    p, pid, plan, loop, topic, _ = env
    loop.stop(topic, 'answered')
    event(p, pid, topic, 'new-generation')
    loop.refresh(pid, p.questions)
    with pytest.raises(ValueError, match='探索已变化'):
        plan.add(pid, {'text': '过期延伸不得写入', 'parent_id': topic['id']}, actor='system', origin='system',
                 expected_parent_version=topic['lifecycle_version'], expected_parent_generation=1)


@pytest.mark.parametrize('late_action', ['collected', 'resume'])
def test_late_recovery_cannot_change_retired_exploration(env, late_action):
    _p, pid, plan, loop, topic, _ = env
    loop.start(topic, 'old')
    if late_action == 'resume':
        loop.collected(topic['id'], 'old')
    loop.hold(topic, 'waiting_recovery')
    plan.update(topic['id'], {'status': 'retired'}, actor='human')
    before = loop.view(pid)
    if late_action == 'collected':
        loop.collected(topic['id'], 'old')
    else:
        loop.resume_collected(pid, 'old')
    assert loop.view(pid) == before


@pytest.mark.parametrize('error,reason', [('request_outcome_unknown', 'waiting_recovery'), ('model_output_invalid_json', 'source_failure')])
def test_dispatch_failure_after_collection_holds_exploration(env, monkeypatch, error, reason):
    p, pid, _plan, loop, topic, _ = env
    parent = p.store.enqueue(pid, 'external')
    claimed = p.store.claim()
    assert p.store.finish(parent['id'], lease_token=claimed['lease_token'])
    p.store.enqueue(pid, 'external_followup', input_version=parent['id'])
    job = p.store.claim()
    assert job is not None
    def dispatch(job):
        assert loop.start(topic, job['id'])
        loop.collected(topic['id'], job['id'])
        raise HttpError(error)
    monkeypatch.setattr(p, 'run_external_followup', dispatch)
    with pytest.raises(HttpError, match=error):
        p.run(job)
    state = loop.view(pid)[topic['id']]
    assert state['reason'] == reason and state['no_gain'] == 0
