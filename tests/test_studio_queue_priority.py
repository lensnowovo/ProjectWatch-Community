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
    external = [store.enqueue(pid, 'collect') for pid in ids]
    first = store.claim()
    assert first['id'] == external[0]['id']
    store.finish(first['id'], lease_token=first['lease_token'])
    brief = store.enqueue(ids[0], 'brief')
    assert store.claim()['id'] == brief['id']


def test_brief_priority_is_bounded_and_persisted_across_restarts(tmp_path):
    store, ids = setup(tmp_path, 5)
    old = store.enqueue(ids[0], 'collect')
    briefs = [store.enqueue(pid, 'brief') for pid in ids[1:]]
    for expected in briefs[:2]:
        store = StudioStore(store.db_path)
        job = store.claim()
        assert job['id'] == expected['id']
        assert store.finish(job['id'], lease_token=job['lease_token'])
    assert StudioStore(store.db_path).claim()['id'] == old['id']


def test_priority_does_not_steal_active_or_paused_external_jobs(tmp_path):
    store, ids = setup(tmp_path, 2)
    store.enqueue(ids[0], 'collect')
    active = store.claim()
    store.enqueue(ids[1], 'collect')
    store.set_status(ids[1], 'paused')
    assert store.claim() is None
    brief = store.enqueue(ids[0], 'brief')
    assert store.claim()['id'] == brief['id']
    assert store.jobs(ids[0])[-1]['lease_token'] == active['lease_token']


def test_single_company_brief_is_immediately_next(tmp_path):
    store, [pid] = setup(tmp_path)
    store.enqueue(pid, 'collect')
    job = store.claim()
    store.finish(job['id'], lease_token=job['lease_token'])
    brief = store.enqueue(pid, 'brief')
    assert store.claim()['id'] == brief['id']


def test_concurrent_claims_are_distinct_and_oldest_work_gets_a_turn(tmp_path):
    store, ids = setup(tmp_path, 5)
    old = store.enqueue(ids[0], 'collect')
    for pid in ids[1:]:
        store.enqueue(pid, 'brief')
    with ThreadPoolExecutor(max_workers=3) as pool:
        claimed = list(pool.map(lambda _: StudioStore(store.db_path).claim(), range(3)))
    assert len({j['id'] for j in claimed}) == 3
    assert old['id'] in {j['id'] for j in claimed}


def test_expired_safe_job_recovery_keeps_its_original_order(tmp_path):
    store, ids = setup(tmp_path, 4)
    store.enqueue(ids[0], 'collect')
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
        store.enqueue(pid, 'collect')
    order = []
    while (job := store.claim()) is not None:
        order.append(job['kind'])
        store.finish(job['id'], lease_token=job['lease_token'])
        if job['kind'] == 'collect':
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


def test_a_round_report_does_not_wait_behind_long_side_jobs(tmp_path):
    store, ids = setup(tmp_path, 1)
    side = [store.enqueue(ids[0], kind) for kind in ('details', 'business', 'competition')]
    report = store.enqueue(ids[0], 'report', input_version='analyze:synthetic')
    first = store.claim()
    assert first['id'] == report['id'] and all(first['id'] != s['id'] for s in side)


def test_a_requested_round_does_not_wait_behind_side_jobs(tmp_path):
    store, ids = setup(tmp_path, 1)
    side = [store.enqueue(ids[0], kind) for kind in ('details', 'business', 'competition', 'landscape')]
    collect = store.enqueue(ids[0], 'collect', input_version='manual:synthetic')
    first = store.claim()
    assert first['id'] == collect['id'] and all(first['id'] != s['id'] for s in side)


def test_a_job_the_user_started_goes_ahead_of_background_side_jobs(tmp_path):
    store, ids = setup(tmp_path, 1)
    side = [store.enqueue(ids[0], kind) for kind in ('details', 'business', 'landscape')]
    asked = store.enqueue(ids[0], 'competition', input_version='manual:synthetic')
    first = store.claim()
    assert first['id'] == asked['id'] and all(first['id'] != s['id'] for s in side)


def test_round_and_side_lanes_take_only_their_own_work(tmp_path):
    store, ids = setup(tmp_path, 2)
    side = store.enqueue(ids[0], 'competition')
    collect = store.enqueue(ids[0], 'collect', input_version='manual:a')
    running = store.claim('round')
    assert running['id'] == collect['id']
    # Project 0's round is running: its side job waits; another project's side job may run.
    assert store.claim('side') is None
    other = store.enqueue(ids[1], 'details')
    assert store.claim('side')['id'] == other['id']
    assert store.claim('round') is None
    store.finish(collect['id'], lease_token=running['lease_token'])
    assert store.claim('side')['id'] == side['id']


def test_two_lanes_start_and_stop_together(tmp_path):
    from project_watch.repository import ProjectRepository
    from project_watch.studio.pipeline import Pipeline, StudioWorker
    from project_watch.studio.settings import SettingsStore

    repo = ProjectRepository(tmp_path / 'lanes.sqlite3')
    pipeline = Pipeline(repo, StudioStore(repo.db_path), SettingsStore(tmp_path / 'settings.json'))
    worker = StudioWorker(pipeline, poll_seconds=0.05, lanes=True)
    worker.start()
    try:
        assert worker.runtime_state() == 'running' and worker._side.runtime_state() == 'running'
    finally:
        worker.stop()
    assert worker.runtime_state() == 'stopped' and worker._side.runtime_state() == 'stopped'


def test_same_round_summary_can_publish_while_analysis_is_running(tmp_path):
    store, (pid,) = setup(tmp_path)
    analysis = store.enqueue(pid, 'analyze')
    assert store.claim('round')['id'] == analysis['id']
    old = store.enqueue(pid, 'page_summary', input_version='analyze:other')
    assert store.claim('side') is None
    with store._connect() as db:
        db.execute("UPDATE studio_jobs SET status='done' WHERE id=?", (old['id'],))
    summary = store.enqueue(pid, 'page_summary', input_version='analyze:' + analysis['id'])
    assert store.claim('side')['id'] == summary['id']



def test_a_round_does_not_start_while_its_project_has_a_side_job_running(tmp_path):
    store, ids = setup(tmp_path, 2)
    store.enqueue(ids[0], 'details')
    running = store.claim('side')
    blocked = store.enqueue(ids[0], 'analyze', input_version='collect:a')
    other = store.enqueue(ids[1], 'collect', input_version='manual:b')
    assert store.claim('round')['id'] == other['id']  # another project's round goes ahead
    assert store.claim('round') is None  # this project's analysis waits for its side job
    store.finish(running['id'], lease_token=running['lease_token'])
    assert store.claim('round')['id'] == blocked['id']
