"""Installed-wheel isolated external-focus acceptance. Synthetic feeds, no network."""
import argparse
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from project_watch.api import create_app
from project_watch.monitoring import FeedItem, ManualSnapshotAdapter
from project_watch.public_search import gdelt_rss_target
from project_watch.repository import ProjectRepository


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--port', type=int, default=8797)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='pw-external-synthetic-') as root:
        database = Path(root) / 'watch.sqlite3'
        repo = ProjectRepository(database)
        project = repo.create_project('Synthetic monitoring acceptance')
        term = 'Synthetic Peer'
        now = datetime.now(UTC)
        items = (
            FeedItem('one', 'Synthetic Peer 公布员工活动安排', 'https://example.org/synthetic-news',
                     '合成验收消息，非真实业务信息。', now),
            FeedItem('two', 'Synthetic Peer 否认完成融资，条款尚未确定', 'https://example.org/synthetic-news',
                     '合成验收：不存在已完成交易的确认。', now-timedelta(hours=1)),
            FeedItem('three', 'Synthetic Peer 公布季度营收', 'https://example.org/synthetic-results',
                     '合成验收：报告期及口径仍需查看原始财报。', now-timedelta(hours=2)),
        )
        app = create_app(database, monitoring_adapters={'rss': ManualSnapshotAdapter({gdelt_rss_target(term):items})})
        client = TestClient(app)
        material = '合成验收材料，非真实公司数据。\n2025 年，公司关注 Synthetic Peer 的产品试验，尚未确认双方存在竞争关系。'
        upload = client.post(f'/api/projects/{project.id}/files', files={
            'file': ('synthetic-peer-note.txt', material.encode(), 'text/plain')})
        assert upload.status_code == 202, upload.text
        assert app.state.intake_worker.process_one().status == 'completed'
        path = f'/api/projects/{project.id}/monitoring/focus'
        selection = {'category':'competitors', 'term':term, 'provider':'gdelt'}
        preview = client.get(path, params=selection).json()
        assert repo.list_monitoring_plans(project.id) == []
        response = client.post(path, json={**selection, 'consent_version':preview['consent_version']})
        assert response.status_code == 202, response.text
        started = response.json()
        assert app.state.monitoring_scheduler.process_one().status == 'done'
        result = client.get(f'/api/projects/{project.id}/monitoring/external').json()
        assert len(result['categories'][2]['items']) == 3
        attention = {i['title']:i['attention'] for i in result['categories'][2]['items']}
        assert attention[items[0].title]['level'] == 'all_news'
        assert attention[items[1].title]['topic'] == 'capital'
        assert attention[items[1].title]['event_verified'] is False
        assert attention[items[2].title]['topic'] == 'performance'
        assert 'Synthetic Peer' in result['categories'][2]['items'][0]['relevance']
        context = result['categories'][2]['items'][0]['internal_context']
        assert context['status'] == 'shared_term_only' and context['impact'] is None
        assert len(context['materials']) == 1
        original = context['materials'][0]
        assert '尚未确认' in original['quote']
        assert any(original['quote'] in f.text and f.locator == original['locator']
                   for f in repo.list_fragments(original['source_id']))
        restarted = TestClient(create_app(database)).get(f'/api/projects/{project.id}/monitoring/external').json()
        assert restarted == result
        assert restarted['categories'][2]['items'][0]['internal_context'] == context
        assert client.post(path, json={**selection, 'consent_version':preview['consent_version']}).json()['job']['id'] == started['job']['id']
        assert len(ProjectRepository(database).list_monitoring_jobs(project.id)) == 1
        paused = client.patch(f"/api/projects/{project.id}/monitoring/plans/{started['plan']['id']}", json={'enabled':False})
        assert paused.status_code == 200
        assert client.get(f'/api/projects/{project.id}/monitoring/external').json()['categories'][2]['coverage'] == 'paused'
        print('Installed external-focus smoke passed: real file upload/parsing, original quote association, preview, consent, queue, competitor relevance, restart, duplicate, pause. Synthetic; no network.', flush=True)
        if args.serve:
            import uvicorn
            print(f'http://127.0.0.1:{args.port}/#project/{project.id}/external', flush=True)
            uvicorn.run(app, host='127.0.0.1', port=args.port)


if __name__ == '__main__':
    main()
