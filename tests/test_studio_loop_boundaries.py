"""Loop stopping invariants. Synthetic databases and fake transports only."""
import socket
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from threading import Barrier
from types import SimpleNamespace

import pytest

from project_watch.studio import plan_replan
from project_watch.studio.app import create_studio_app
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import HttpError, Response
from project_watch.studio.pipeline import StudioWorker


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail('loop boundary tests must not use the network')
    monkeypatch.setattr(socket.socket, 'connect', blocked)


@pytest.fixture
def env(tmp_path):
    p = create_studio_app(tmp_path, start_worker=False).state.studio_pipeline
    pid = p.repository.create_project('合成边界公司').id
    p.store.upsert_project(pid, {'legal_name': '合成边界公司'}, consent=True)
    return p, pid, p.monitoring_plan


def add(plan, pid, text, **fields):
    return plan.add(pid, {'text': text, 'queries': [text], **fields}, actor='human', origin='human')


def test_restore_and_revert_cannot_exceed_capacity(env):
    _, pid, plan = env
    rows = [add(plan, pid, f'问题{i}') for i in range(16)]
    plan.update(rows[0]['id'], {'status': 'retired'}, actor='human')
    closed = next(c for c in plan.view(pid)['changes'] if c['action'] == 'retire')
    add(plan, pid, '新的关注点')
    with pytest.raises(ValueError, match='16'):
        plan.update(rows[0]['id'], {'status': 'active'}, actor='human')
    with pytest.raises(ValueError, match='16'):
        plan.revert(closed['id'])
    assert len([w for w in plan.items(pid) if w['status'] != 'retired']) == 16


def test_concurrent_last_slot_is_atomic(env):
    _, pid, plan = env
    for i in range(15):
        add(plan, pid, f'并发前问题{i}')
    barrier = Barrier(2)
    def attempt(i):
        barrier.wait(timeout=5)
        try:
            add(plan, pid, f'最后一个位置{i}')
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, range(2))) == [False, True]


def test_graph_updates_cannot_cycle_cross_projects_or_overflow_descendants(env):
    p, pid, plan = env
    root = add(plan, pid, '根问题')
    child = add(plan, pid, '子问题', parent_id=root['id'])
    leaf = add(plan, pid, '末端问题', parent_id=child['id'])
    assert plan._get(pid, leaf['id'])['hop'] == '2'
    for parent in (leaf['id'], root['id']):
        with pytest.raises(ValueError):
            plan.update(root['id'], {'parent_id': parent}, actor='human')
    other = p.repository.create_project('另一家公司').id
    foreign = add(plan, other, '别家根问题')
    with pytest.raises(ValueError):
        plan.update(child['id'], {'parent_id': foreign['id']}, actor='human')
    branch = add(plan, pid, '额外一级', parent_id=root['id'])
    with pytest.raises(ValueError):
        plan.update(child['id'], {'parent_id': branch['id']}, actor='human')


def test_replan_cannot_reset_depth_by_omitting_parent(env):
    _, pid, plan = env
    root = add(plan, pid, '根问题')
    child = add(plan, pid, '一级问题', parent_id=root['id'])
    leaf = add(plan, pid, '二级问题', parent_id=child['id'])
    gathered = {'items': {'W3': leaf}, 'numbered': {'E1': {'kind': 'event', 'id': 'e', 'title': '二级问题的新方向'}}}
    counts = plan_replan.apply(plan, pid, [{'action': 'explore', 'text': '不允许隐藏来源的延伸',
        'section': 'peers', 'refs': ['E1'], 'why': '延伸'}], gathered, trigger='external')
    assert counts['added'] == 0 and counts['dropped'] == 1


def seed_pending(p, pid, watch):
    p.store.add_items(pid, 'company', [{'key': 'one', 'title': '合成资产有进展', 'url': 'https://example.invalid/a',
        'source': 'synthetic', 'snippet': '', 'published_at': None}])
    item = p.store.untriaged(pid)[0]
    p.store.create_event(pid, {'title': '合成资产有进展', 'importance': 'notable', 'category': 'company'}, [item['id']])
    p.monitor.add_hits([(watch['id'], item['id'])])


def test_retired_inputs_never_dispatched_and_history_remains(env, monkeypatch):
    p, pid, plan = env
    w = add(plan, pid, '合成资产进展')
    seed_pending(p, pid, w)
    plan.update(w['id'], {'status': 'retired'}, actor='human')
    monkeypatch.setattr(p.questions, '_assess_batch', lambda *a: pytest.fail('closed question dispatched'))
    assert p.questions.assess(object(), SimpleNamespace(ready=True), pid, p.store.get_project(pid)) == 0
    assert len(p.monitor.watch_event_ids(w['id'])) == 1


def test_close_restore_rejects_old_question_snapshot(env):
    p, pid, plan = env
    w = add(plan, pid, '需要版本保护的问题')
    old = p.questions._question_fingerprint(w)
    plan.update(w['id'], {'status': 'retired'}, actor='human')
    plan.update(w['id'], {'status': 'active'}, actor='human')
    with p.store._connect() as db, pytest.raises(ValueError):
        p.questions._check_question(db, pid, w['id'], old)


def test_pause_keeps_received_receipt_but_blocks_next_dispatch(env):
    p, pid, _ = env
    p.store.enqueue(pid, 'external')
    job = p.store.claim()
    execution = JobExecution(p.store, job)
    def receive():
        p.store.set_status(pid, 'paused')
        return Response(200, {}, b'{}')
    execution.call('model', 'first', receive)
    assert execution.usage()['model']['completed'] == 1
    assert execution.call('model', 'first', lambda: pytest.fail('receipt resent')).status == 200
    with pytest.raises(HttpError, match='project_paused'):
        execution.call('model', 'next', lambda: pytest.fail('new call after pause'))
    assert execution.usage()['model']['requests'] == 1


def test_paused_dispatch_remains_same_job_and_resumes_receipts(env, monkeypatch):
    p, pid, _ = env
    p.store.enqueue(pid, 'external')
    job = p.store.claim()
    calls = []
    def dispatch(job):
        execution = p._execution_state.current
        def receive():
            calls.append('received')
            p.store.set_status(pid, 'paused')
            return Response(200, {}, b'{}')
        execution.call('model', 'already-received', receive)
        # Even a downstream collector swallowing a control error cannot mark success.
        with suppress(HttpError):
            execution.call('model', 'next', lambda: Response(200, {}, b'{}'))
    monkeypatch.setattr(p, 'run_external', dispatch)
    with pytest.raises(HttpError, match='project_paused'):
        p.run(job)
    assert p.store.defer_paused(job)
    assert p.store.claim() is None
    p.store.set_status(pid, 'active')
    resumed = p.store.claim()
    assert resumed['id'] == job['id']
    p.run(resumed)
    assert calls == ['received']
    assert JobExecution(p.store, resumed).read_checkpoint('dispatch_completed')


def test_worker_records_pause_as_queued_not_failed(env, monkeypatch):
    p, pid, _ = env
    queued = p.store.enqueue(pid, 'external')
    worker = StudioWorker(p)
    monkeypatch.setattr(worker, 'schedule', lambda: None)
    def pause(job):
        p.store.set_status(pid, 'paused')
        raise HttpError('project_paused')
    monkeypatch.setattr(p, 'run', pause)
    assert worker.step()
    job = next(j for j in p.store.jobs(pid) if j['id'] == queued['id'])
    assert job['status'] == 'queued' and not job['error']
    assert '已暂停' in job['stage']
    assert not worker.step()


def test_paused_project_does_not_claim_brief(env):
    p, pid, _ = env
    p.store.enqueue(pid, 'brief')
    p.store.set_status(pid, 'paused')
    assert p.store.claim() is None


def test_closed_collection_skips_remaining_queries_and_late_hits(env, monkeypatch):
    p, pid, plan = env
    w = add(plan, pid, '监控中关闭', queries=['第一条', '第二条'])
    calls = []
    def search(*args):
        calls.append(args[1])
        plan.update(w['id'], {'status': 'retired'}, actor='human')
        return []
    monkeypatch.setattr('project_watch.studio.pipeline._news', search)
    p._collect_monitor(pid, p.store.get_project(pid), SimpleNamespace(has_search=False), lambda _: None)
    assert calls == ['第一条']
    assert not plan._get(pid, w['id'])['last_run_at']
    seed_pending(p, pid, w)
    assert p.monitor.watch_event_ids(w['id']) == []


def test_old_query_receipt_cannot_advance_restored_item(env):
    _, pid, plan = env
    w = add(plan, pid, '旧查询')
    plan.update(w['id'], {'status': 'retired'}, actor='human')
    plan.update(w['id'], {'status': 'active'}, actor='human')
    plan.mark_query(w['id'], '旧查询', success=True, lifecycle_version='0')
    assert not plan._get(pid, w['id'])['last_run_at']


@pytest.mark.parametrize('action', ['note', 'add', 'retire'])
def test_late_replan_cannot_write_after_close_restore(env, action):
    _, pid, plan = env
    w = add(plan, pid, '合成资产问题')
    plan.update(w['id'], {'status': 'retired'}, actor='human')
    plan.update(w['id'], {'status': 'active'}, actor='human')
    gathered = {'items': {'W1': w}, 'numbered': {'E1': {'kind': 'event', 'id': 'e', 'title': '合成资产问题'}},
                'watch_events': {w['id']: {'e'}}}
    counts = plan_replan.apply(plan, pid, [{'action': action, 'item': 'W1', 'from': 'W1', 'section': 'peers', 'text': '旧分析结果',
        'refs': ['E1'], 'why': '原先的快照'}], gathered, trigger='external')
    assert counts == {'added': 0, 'noted': 0, 'applied': 0, 'proposed': 0, 'dropped': 1}
    assert not plan._get(pid, w['id'])['current_state']


def test_collection_failure_invalidates_prior_stable_state(env):
    p, pid, plan = env
    add(plan, pid, '合成方案')
    for _ in range(3):
        plan.record_revision(pid, {}, 'external')
    assert plan.state(pid)['phase'] == 'stable'
    p.store.put_plan(pid, {'last_report': {'company': {'errors': ['offline']}}})
    assert plan.state(pid)['phase'] != 'stable'


@pytest.mark.parametrize('counts', [{'dropped': 3}, {'applied': 1}, {'incomplete': 1}])
def test_rejected_changed_or_incomplete_round_is_not_stable(env, counts):
    _, pid, plan = env
    add(plan, pid, '合成方案')
    for _ in range(3):
        plan.record_revision(pid, counts, 'external')
    assert plan.state(pid)['phase'] != 'stable'
