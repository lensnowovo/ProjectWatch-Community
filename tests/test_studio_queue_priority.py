"""Synthetic queue scheduling; no providers or real service are touched."""

from concurrent.futures import ThreadPoolExecutor

from project_watch.repository import ProjectRepository
from project_watch.studio.store import StudioStore


def setup(tmp_path, count=1):
    repository = ProjectRepository(tmp_path / 'queue.sqlite3')
    store = StudioStore(repository.db_path)
    ids = [repository.create_project(f'Synthetic {i}').id for i in range(count)]
    for pid in ids:
        store.upsert_project(pid, {}, consent=True)
    return store, ids


def test_first_brief_precedes_other_company_collection(tmp_path):
    store, ids = setup(tmp_path, 6)
    external = [store.enqueue(pid, 'external') for pid in ids]
    first = store.claim()
    assert first['id'] == external[0]['id']
    store.finish(first['id'], lease_token=first['lease_token'])
    brief = store.enqueue(ids[0], 'brief')
    assert store.claim()['id'] == brief['id']


def test_brief_priority_is_bounded_and_persisted_across_restarts(tmp_path):
    store, ids = setup(tmp_path, 5)
    old = store.enqueue(ids[0], 'external')
    briefs = [store.enqueue(pid, 'brief') for pid in ids[1:]]
    for expected in briefs[:2]:
        store = StudioStore(store.db_path)
        job = store.claim()
        assert job['id'] == expected['id']
        assert store.finish(job['id'], lease_token=job['lease_token'])
    assert StudioStore(store.db_path).claim()['id'] == old['id']


def test_priority_does_not_steal_active_or_paused_external_jobs(tmp_path):
    store, ids = setup(tmp_path, 2)
    store.enqueue(ids[0], 'external')
    active = store.claim()
    store.enqueue(ids[1], 'external')
    store.set_status(ids[1], 'paused')
    assert store.claim() is None
    brief = store.enqueue(ids[0], 'brief')
    assert store.claim()['id'] == brief['id']
    assert store.jobs(ids[0])[-1]['lease_token'] == active['lease_token']


def test_single_company_brief_is_immediately_next(tmp_path):
    store, [pid] = setup(tmp_path)
    store.enqueue(pid, 'external')
    job = store.claim()
    store.finish(job['id'], lease_token=job['lease_token'])
    brief = store.enqueue(pid, 'brief')
    assert store.claim()['id'] == brief['id']


def test_concurrent_claims_are_distinct_and_oldest_work_gets_a_turn(tmp_path):
    store, ids = setup(tmp_path, 5)
    old = store.enqueue(ids[0], 'external')
    for pid in ids[1:]:
        store.enqueue(pid, 'brief')
    with ThreadPoolExecutor(max_workers=3) as pool:
        claimed = list(pool.map(lambda _: StudioStore(store.db_path).claim(), range(3)))
    assert len({j['id'] for j in claimed}) == 3
    assert old['id'] in {j['id'] for j in claimed}


def test_expired_safe_job_recovery_keeps_its_original_order(tmp_path):
    store, ids = setup(tmp_path, 4)
    store.enqueue(ids[0], 'external')
    old = store.claim()
    with store._connect() as db:
        db.execute("UPDATE studio_jobs SET lease_until='2000-01-01T00:00:00+00:00' WHERE id=?", (old['id'],))
    for pid in ids[1:]:
        store.enqueue(pid, 'brief')
    claimed = [store.claim() for _ in range(3)]
    recovered = next(j for j in claimed if j['id'] == old['id'])
    assert recovered['lease_token'] != old['lease_token'] and recovered['attempts'] == 2


def test_six_company_first_brief_queue_position_compared_with_fifo(tmp_path):
    store, ids = setup(tmp_path, 6)
    for pid in ids:
        store.enqueue(pid, 'external')
    order = []
    while (job := store.claim()) is not None:
        order.append(job['kind'])
        store.finish(job['id'], lease_token=job['lease_token'])
        if job['kind'] == 'external':
            store.enqueue(job['project_id'], 'brief')
    assert len(order) == 12 and order.index('brief') + 1 == 2
    # Same fixture under FIFO: six prequeued external jobs precede generated briefs.
    fifo = ['external'] * 6 + ['brief'] * 6
    assert fifo.index('brief') + 1 == 7


def test_continuously_added_briefs_cannot_starve_old_bulk_work(tmp_path):
    store, ids = setup(tmp_path, 2)
    old = store.enqueue(ids[0], 'landscape')
    selected = []
    for _ in range(3):
        store.enqueue(ids[1], 'brief')
        job = store.claim()
        selected.append(job['id'])
        store.finish(job['id'], lease_token=job['lease_token'])
    assert selected[-1] == old['id']
