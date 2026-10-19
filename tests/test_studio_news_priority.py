"""Synthetic pending-news ordering; no public/private company fixtures."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from project_watch.studio.app import create_studio_app


def seeded(tmp_path):
    pipeline = create_studio_app(tmp_path, start_worker=False).state.studio_pipeline
    pid = pipeline.repository.create_project('Synthetic Cell Monitor').id
    pipeline.store.upsert_project(pid, {}, consent=True)
    today = datetime.now(UTC).date()
    dates = {'recent': (today - timedelta(days=7)).isoformat(),
             'newest': (today - timedelta(days=1)).isoformat(),
             'old': (today - timedelta(days=800)).isoformat(),
             'undated': None, 'future': (today + timedelta(days=7)).isoformat(),
             'malformed': 'not-a-date', 'impossible': '2025-02-30'}
    pipeline.store.add_items(pid, 'company', [
        {'key': key, 'title': 'Synthetic original ' + key, 'url': f'https://publisher.example/{key}',
         'source': 'Synthetic Publisher', 'snippet': '', 'published_at': value}
        for key, value in dates.items()])
    return pipeline, pid


def test_recent_published_reports_precede_collected_undated_and_future_rows(tmp_path):
    pipeline, pid = seeded(tmp_path)
    selected = pipeline.store.untriaged(pid, limit=2)
    assert [r['key'] for r in selected] == ['newest', 'recent']
    rows = pipeline.store.untriaged(pid)
    assert len(rows) == 7 and rows[-1]['key'] == 'old'
    assert {r['key'] for r in rows} == {'newest', 'recent', 'old', 'undated', 'future', 'malformed', 'impossible'}
    assert all(not r['triaged'] and r['body_status'] == '' for r in rows)


def test_original_read_budget_uses_recent_order_and_retains_deferred_reports(tmp_path):
    pipeline, pid = seeded(tmp_path)
    reads = []

    def read(url):
        reads.append(url)
        return {'status': 'article_body', 'text': 'Synthetic located original product evidence.'}

    pipeline.article_reader = SimpleNamespace(read=read)
    assert pipeline.fetch_bodies(pid, lambda _: None, page_limit=2) == 2
    assert {url.rsplit('/', 1)[-1] for url in reads} == {'newest', 'recent'}
    rows = pipeline.store.untriaged(pid)
    assert len(rows) == 7
    assert {r['key'] for r in rows if r['body_status'] == 'article_body'} == {'newest', 'recent'}
    assert all(not r['triaged'] for r in rows)
