import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest
from test_external_analysis import inputs

from project_watch.feeds import PublicResponse
from project_watch.public_article import ArticleReader
from project_watch.public_article_store import ArticleStore


class Reader:
    calls = 0

    def read(self, url):
        self.calls += 1
        class Transport:
            def get_response(self, url):
                return PublicResponse(url, {'content-type': 'text/html'},
                                      ('<article><p>' + 'Synthetic public report. ' * 10 + '</p></article>').encode())
        return ArticleReader(Transport()).read(url)


def test_snapshot_persists_offsets_and_restart_never_refetches(tmp_path):
    repo, p, _, packet = inputs(tmp_path)
    store, reader = ArticleStore(repo), Reader()
    saved = store.read_once(p.id, packet['signal_id'], reader)
    assert saved['status'] == 'article_body'
    assert saved['signal_id'] == packet['signal_id'] and saved['project_id'] == p.id
    assert saved['fetched_at'] and len(saved['snapshot_digest']) == 64
    assert ArticleStore(repo).read_once(p.id, packet['signal_id'], reader) == saved
    assert reader.calls == 1
    for para in saved['paragraphs']:
        assert saved['text'][para['start']:para['end']] == para['text']
    with closing(repo._connect()) as db, db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute('DELETE FROM public_article_snapshots')
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE public_article_snapshots SET payload_json='{}'")


def test_cross_project_and_arbitrary_url_are_not_readable(tmp_path):
    repo, p, _, packet = inputs(tmp_path)
    store, reader = ArticleStore(repo), Reader()
    with pytest.raises(ValueError):
        store.read_once('another-project', packet['signal_id'], reader)
    with pytest.raises(ValueError):
        store.read_once(p.id, 'https://arbitrary.example/not-discovered', reader)
    assert reader.calls == 0


def test_concurrent_claim_has_one_owner_and_read_only_lookup(tmp_path):
    repo, p, _, packet = inputs(tmp_path)
    store = ArticleStore(repo)
    assert store.get(p.id, packet['signal_id']) is None
    with ThreadPoolExecutor(4) as pool:
        claims = list(pool.map(lambda _: store.claim(p.id, packet['signal_id']), range(4)))
    assert len([c for c in claims if c is not None]) == 1
    assert store.get(p.id, packet['signal_id']) is None


def test_interruption_becomes_honest_unavailable_not_duplicate_fetch(tmp_path):
    repo, p, _, packet = inputs(tmp_path)
    store, reader = ArticleStore(repo), Reader()
    store.claim(p.id, packet['signal_id'])
    with closing(repo._connect()) as db, db:
        db.execute('UPDATE public_article_fetches SET lease_until=0')
    saved = ArticleStore(repo).read_once(p.id, packet['signal_id'], reader)
    assert saved['status'] == 'unavailable' and saved['error_code'] == 'interrupted'
    assert reader.calls == 0


def test_wrong_reader_source_and_failed_fetch_are_not_retried(tmp_path):
    repo, p, _, packet = inputs(tmp_path)
    store = ArticleStore(repo)
    class Broken:
        calls = 0
        def read(self, url):
            self.calls += 1
            raise RuntimeError('private exception must not be persisted')
    reader = Broken()
    saved = store.read_once(p.id, packet['signal_id'], reader)
    assert saved['status'] == 'unavailable' and saved['error_code'] == 'fetch_failed'
    assert 'private exception' not in str(saved)
    assert store.read_once(p.id, packet['signal_id'], reader) == saved and reader.calls == 1


@pytest.mark.parametrize('interrupted', [False, True])
def test_extractor_upgrade_keeps_existing_evidence_and_no_automatic_refetch(tmp_path, monkeypatch, interrupted):
    import project_watch.public_article as parser
    import project_watch.public_article_store as storage
    repo, p, _, packet = inputs(tmp_path)
    store, reader = ArticleStore(repo), Reader()
    monkeypatch.setattr(parser, 'EXTRACTION_VERSION', 'public-article-v1')
    monkeypatch.setattr(storage, 'EXTRACTION_VERSION', 'public-article-v1')
    if interrupted:
        store.claim(p.id, packet['signal_id'])
        with closing(repo._connect()) as db, db:
            db.execute('UPDATE public_article_fetches SET lease_until=0')
    else:
        saved = store.read_once(p.id, packet['signal_id'], reader)
    before = reader.calls
    monkeypatch.setattr(parser, 'EXTRACTION_VERSION', 'public-article-v2')
    monkeypatch.setattr(storage, 'EXTRACTION_VERSION', 'public-article-v2')
    after = ArticleStore(repo).read_once(p.id, packet['signal_id'], reader)
    assert reader.calls == before
    assert after['extraction_version'] == 'public-article-v1'
    if interrupted:
        assert after['error_code'] == 'interrupted'
    else:
        assert after == saved
