"""Every entry that starts public work creates the new round kinds only. Synthetic data; no network."""
from fastapi.testclient import TestClient
from synthetic_providers import legacy_job
from test_spine_journey import env, work  # noqa: F401  (pytest fixture)

from project_watch.studio import dispatch, spine
from project_watch.studio.store import now


def _kinds(pipeline, pid):
    with pipeline.store._connect() as db:
        return [r[0] for r in db.execute("SELECT kind FROM studio_jobs WHERE project_id=? AND status IN ('queued','running') "
                                         'ORDER BY created_at,rowid', (pid,))]


def test_refresh_starts_one_round_despite_an_old_unknown_request(env):  # noqa: F811
    pipeline, pid, _state = env
    legacy = legacy_job(pipeline.store, pid, 'external')
    with pipeline.store._connect() as db:
        db.execute("UPDATE studio_jobs SET status='failed' WHERE id=?", (legacy['id'],))
        db.execute('INSERT INTO studio_job_calls(job_id,call_key,kind,provider,status,created_at) VALUES (?,?,?,?,?,?)',
                   (legacy['id'], 'legacy-key', 'search', 'synthetic', 'unknown', now()))
    client = TestClient(pipeline.test_app)
    first = client.post(f'/api/studio/projects/{pid}/refresh', json={'kind': 'external'})
    assert first.status_code == 202 and first.json()['kind'] == 'collect'
    assert first.json()['input_version'].startswith('manual:')
    # A second click joins the round in progress; the follow-up kind is no longer created either.
    again = client.post(f'/api/studio/projects/{pid}/refresh', json={'kind': 'external_followup'})
    assert again.json()['id'] == first.json()['id']
    assert _kinds(pipeline, pid) == ['collect']
    with pipeline.store._connect() as db:
        status = db.execute("SELECT status FROM studio_job_calls WHERE call_key='legacy-key'").fetchone()[0]
    assert status == 'unknown'  # accounted, never settled or resent


def test_scheduler_and_watch_entry_use_the_round(env):  # noqa: F811
    pipeline, pid, _state = env
    job = spine.request_round(pipeline.store, pid, 'scheduled', automatic=True)
    assert job['kind'] == 'collect' and job['input_version'].startswith('scheduled:')
    work(pipeline, 'collect')
    # While analysis of this round is queued, another request joins it instead of collecting again.
    assert spine.request_round(pipeline.store, pid, 'manual')['kind'] == 'analyze'


def test_legacy_jobs_are_archived_and_never_run(env):  # noqa: F811
    pipeline, pid, _state = env
    queued = legacy_job(pipeline.store, pid, 'external_followup')
    assert dispatch.archive_legacy(pipeline.store) == [queued['id']]
    assert dispatch.archive_legacy(pipeline.store) == []
    assert pipeline.store.claim() is None


def test_new_internal_documents_trigger_one_analysis_without_collection(env):  # noqa: F811
    pipeline, pid, _state = env
    internal = pipeline.store.enqueue(pid, 'internal', input_version='docs')
    with pipeline.store._connect() as db:
        db.execute("UPDATE studio_jobs SET status='done',finished_at=? WHERE id=?", (now(), internal['id']))
    job = spine.schedule_document_analysis(pipeline, pid)
    assert job['kind'] == 'analyze' and job['input_version'] == 'documents:' + internal['id']
    assert spine.schedule_document_analysis(pipeline, pid) is None  # busy
    with pipeline.store._connect() as db:
        db.execute("UPDATE studio_jobs SET status='failed' WHERE id=?", (job['id'],))
    assert spine.schedule_document_analysis(pipeline, pid) is None  # failed: the owner redoes it, not a loop
