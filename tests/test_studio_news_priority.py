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


def test_original_read_keeps_order_and_reads_all_scoped_reports_despite_old_limit(tmp_path):
    pipeline, pid = seeded(tmp_path)
    reads = []

    def read(url):
        reads.append(url)
        return {'status': 'article_body', 'text': 'Synthetic located original product evidence.'}

    pipeline.article_reader = SimpleNamespace(read=read)
    assert pipeline.fetch_bodies(pid, lambda _: None) == 7
    assert reads[:2] == ['https://publisher.example/newest', 'https://publisher.example/recent']
    assert len(set(reads)) == 7
    rows = pipeline.store.untriaged(pid)
    assert len(rows) == 7
    assert all(r['body_status'] == 'article_body' for r in rows)
    assert all(not r['triaged'] for r in rows)
