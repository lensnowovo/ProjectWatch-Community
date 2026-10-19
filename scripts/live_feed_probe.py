"""Single public RSS/Atom request through the installed app. Preview by default.

No keys, private files or pilot database. An operator must approve a public term
and exact feed URL. Optional loopback proxy only routes this explicitly named URL.
"""
import argparse
import json
import tempfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from project_watch.api import create_app
from project_watch.feeds import validate_fixed_feed_targets
from project_watch.repository import ProjectRepository


def exercise(root, *, term, target, proxy_url=None):
    validate_fixed_feed_targets((target,))
    database = Path(root) / 'public-only.sqlite3'
    app = create_app(database, fixed_feed_proxy_url=proxy_url,
                     fixed_feed_targets=(target,) if proxy_url else ())
    client = TestClient(app)
    repo = ProjectRepository(database)
    project = repo.create_project(term)
    route = f'/api/projects/{project.id}'
    plan = client.post(route+'/monitoring/plans', json={
        'kind':'rss', 'target':target, 'cadence_minutes':1440})
    assert plan.status_code == 201, plan.text
    plan_id = plan.json()['id']
    key = 'single-approved-public-probe'
    run = client.post(route+f'/monitoring/plans/{plan_id}/runs', json={'idempotency_key':key})
    assert run.status_code == 202, run.text
    assert app.state.monitoring_scheduler.process_one().status == 'done'
    receipt = repo.list_source_runs(plan_id)[0]
    before = client.get(route+'/monitoring/external').json()
    again = client.post(route+f'/monitoring/plans/{plan_id}/runs', json={'idempotency_key':key})
    assert again.status_code == 202
    assert app.state.monitoring_scheduler.process_one() is None
    after = TestClient(create_app(database)).get(route+'/monitoring/external').json()
    assert before == after
    visible = before['categories'][0]['items']
    report = {'checked_at':datetime.now(UTC).isoformat(), 'term':term, 'target':target,
              'network_requests_limit':1, 'private_materials_sent':False, 'model_calls':0,
              'receipt':asdict(receipt), 'visible_items':len(visible),
              'sample':[{'title':i['title'], 'url':i['url'], 'published_at':i['published_at']} for i in visible[:3]],
              'restart_and_idempotency_verified':True}
    print(json.dumps(report, ensure_ascii=True, default=str), flush=True)
    return app, project.id


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--term', required=True)
    parser.add_argument('--target', required=True)
    parser.add_argument('--proxy-url')
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--port', type=int, default=8797)
    args = parser.parse_args()
    validate_fixed_feed_targets((args.target,))
    if not args.run:
        print(json.dumps({'mode':'preview_no_network', 'term':args.term, 'target':args.target,
            'proxy_url':args.proxy_url, 'max_requests':1, 'private_materials_sent':False}, ensure_ascii=True))
        return
    with tempfile.TemporaryDirectory(prefix='pw-public-only-') as root:
        app, project_id = exercise(root, term=args.term, target=args.target, proxy_url=args.proxy_url)
        if args.serve:
            import uvicorn
            print(f'http://127.0.0.1:{args.port}/#project/{project_id}/external', flush=True)
            uvicorn.run(app, host='127.0.0.1', port=args.port)


if __name__ == '__main__':
    main()
