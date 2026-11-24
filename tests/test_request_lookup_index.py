"""Duplicate-request checks find a request and its rate-limit retries through an index."""
from contextlib import closing

from project_watch.studio import dispatch
from project_watch.studio.app import create_studio_app


def store(tmp_path):
    return create_studio_app(tmp_path, start_worker=False).state.studio_pipeline.store


def test_the_check_uses_the_key_index_not_a_scan_of_every_reply(tmp_path):
    sql = (f"SELECT 1 FROM studio_job_calls c JOIN studio_jobs j ON j.id=c.job_id "
           f"WHERE j.project_id=? AND {dispatch.SAME_REQUEST} LIMIT 1")
    with closing(store(tmp_path)._connect()) as db:
        plan = ' | '.join(r[3] for r in db.execute('EXPLAIN QUERY PLAN ' + sql, ('p', *dispatch.same_request('k'))))
    assert 'studio_job_calls_key' in plan and 'SCAN c' not in plan, plan


def test_it_matches_exactly_the_request_and_its_retries(tmp_path):
    keys = ['k', 'k:429:1', 'k:429:12', 'k:4291', 'k:429', 'kk', 'k:430:1', 'j:429:1']
    p = create_studio_app(tmp_path, start_worker=False).state.studio_pipeline
    pid = p.repository.create_project('Synthetic Lookup Co').id
    p.store.upsert_project(pid, {'legal_name': 'Synthetic Lookup Co'}, consent=True)
    job = p.store.enqueue(pid, 'brief', input_version='manual:lookup')
    with closing(p.store._connect()) as db, db:
        db.executemany("INSERT INTO studio_job_calls(job_id,call_key,kind,provider,status,created_at) "
                       "VALUES (?,?,'model','p','completed','x')", [(job['id'], k) for k in keys])
        found = {r[0] for r in db.execute(f'SELECT call_key FROM studio_job_calls c WHERE {dispatch.SAME_REQUEST}',
                                          dispatch.same_request('k'))}
        glob = {r[0] for r in db.execute("SELECT call_key FROM studio_job_calls WHERE call_key=? OR call_key GLOB ?||':429:*'",
                                         ('k', 'k'))}
    assert found == glob == {'k', 'k:429:1', 'k:429:12'}
