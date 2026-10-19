"""Installed-wheel financial briefing acceptance with synthetic files; no network."""
import argparse
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from project_watch.api import create_app
from project_watch.repository import ProjectRepository


def exercise(root):
    database = Path(root) / 'watch.sqlite3'
    app = create_app(database)
    client = TestClient(app)
    repo = ProjectRepository(database)
    project = repo.create_project('合成光学 · 变化阅读验收')
    route = f'/api/projects/{project.id}'
    for year, value in [('2024', '80'), ('2025', '100')]:
        content = f'合成验收材料，非真实公司数据。\n{year} 年合并口径实际营业收入 {value} 万元。'
        response = client.post(route+'/files', files={'file': (f'synthetic-{year}.txt', content.encode(), 'text/plain')})
        assert response.status_code == 202, response.text
        assert app.state.intake_worker.process_one().status == 'completed'
        source = next(s for s in repo.list_sources(project.id) if s.reference == f'synthetic-{year}.txt')
        fragment = next(f for f in repo.list_fragments(source.id) if year in f.text)
        observation = client.post(route+'/observations', json=dict(source_id=source.id,
            subject='财务表现', statement='营业收入', number=value, unit='万元', period=year,
            scope='合并', basis='actual', source_locator=fragment.locator))
        assert observation.status_code == 201, observation.text
        reviewed = client.post(f"/api/observations/{observation.json()['id']}/confirm",
                               json=dict(reviewer='synthetic-local-reviewer', expected_version=1))
        assert reviewed.status_code == 200, reviewed.text
    endpoint = f'/api/workspace/projects/{project.id}/briefing'
    result = client.get(endpoint).json()
    assert len(result['items']) == 1, result
    assert result['items'][0]['comparison']['percent'] == '25.00'
    assert TestClient(create_app(database)).get(endpoint).json() == result
    for ref in result['items'][0]['evidence_refs']:
        assert any(f.locator == ref['locator'] for f in repo.list_fragments(ref['source_id']))
    repo.edit_monitor_profile(project.id, expected_version=0, fields={'thesis': '合成验收：收入增长原因与回款仍需核实。'})
    assert client.get(endpoint).json() == result
    print('Installed briefing passed: two real uploads, parsing, reviewed comparison, source locations, restart, human thesis unchanged. Synthetic; no network.', flush=True)
    return app, project.id


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--port', type=int, default=8797)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='pw-briefing-synthetic-') as root:
        app, project_id = exercise(root)
        if args.serve:
            import uvicorn
            print(f'http://127.0.0.1:{args.port}/#project/{project_id}/overview', flush=True)
            uvicorn.run(app, host='127.0.0.1', port=args.port)


if __name__ == '__main__':
    main()
