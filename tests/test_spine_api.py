"""Owner-facing routes of the rewritten pipeline. Synthetic data; no network."""
from fastapi.testclient import TestClient
from test_spine_journey import env, work  # noqa: F401  (pytest fixture)

from project_watch.studio.store import now


def test_round_progress_reanalysis_and_assumptions(env):  # noqa: F811
    pipeline, pid, _state = env
    client = TestClient(pipeline.test_app)
    assert client.get(f'/api/studio/projects/{pid}/round').json()['collect'] is None
    pipeline.store.enqueue(pid, 'collect', input_version='manual:' + now())
    work(pipeline, 'collect')
    view = client.get(f'/api/studio/projects/{pid}/round').json()
    assert view['collect']['status'] == 'done' and view['collect']['result']['status'] in ('complete', 'partial')
    assert view['materials']['paper'] >= 1 and view['running']  # analysis queued, materials already visible
    work(pipeline, 'analyze')
    view = client.get(f'/api/studio/projects/{pid}/round').json()
    assert view['analyze']['result']['published'] and view['analyze']['result']['status'] == 'partial'
    # Re-analysis needs the owner's explicit acknowledgement and a finished batch.
    assert client.post(f'/api/studio/projects/{pid}/reanalyze', json={'accept_duplicate_cost': False}).status_code == 409
    assert client.post(f'/api/studio/projects/{pid}/reanalyze', json={'accept_duplicate_cost': True}).status_code == 409
    work(pipeline, 'report')
    with pipeline.store._connect() as db:
        db.execute("UPDATE studio_jobs SET status='done' WHERE kind='brief' AND project_id=?", (pid,))
    accepted = client.post(f'/api/studio/projects/{pid}/reanalyze', json={'accept_duplicate_cost': True})
    assert accepted.status_code == 202 and accepted.json()['input_version'].startswith('redo:')
    # Assumptions: listed, owner-added, edited, retired.
    items = client.get(f'/api/studio/projects/{pid}/assumptions').json()['items']
    assert len(items) == 2
    added = client.post(f'/api/studio/projects/{pid}/assumptions', json={'text': '海外授权收入持续'}).json()
    assert added['author'] == 'user'
    patched = client.patch(f'/api/studio/projects/{pid}/assumptions/{items[0]["id"]}', json={'status': 'retired'}).json()
    assert patched['status'] == 'retired' and patched['user_override']
    assert len(client.get(f'/api/studio/projects/{pid}/assumptions').json()['items']) == 2
    assert client.patch(f'/api/studio/projects/{pid}/assumptions/A00000000', json={'status': 'retired'}).status_code == 404
    warning = client.get(f'/api/studio/projects/{pid}/warning').json()
    signal = next(a for a in warning['assumptions'] if a['signals'])['signals'][0]
    assert signal['source']['title'] and warning['unforeseen'][0]['sources']
