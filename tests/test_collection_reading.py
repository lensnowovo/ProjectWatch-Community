"""Collected snapshots and fetched pages remain separately citable."""
from contextlib import closing
from types import SimpleNamespace

import pytest
import test_collection_supplements as fixtures

from project_watch.studio import collection_dsh
from project_watch.studio.dsh_bootstrap import PublicBootstrap
from project_watch.studio.http import HttpError


@pytest.fixture
def research(tmp_path):
    loop, searches = fixtures.research.__wrapped__(tmp_path)
    pages = []

    def read(url):
        pages.append(url)
        return {'status': 'article_body', 'text': 'Synthetic original article evidence'}

    loop.reader = SimpleNamespace(read=read)
    loop.collectors.deepseek_fetch = lambda _: None
    [entry] = collection_dsh.catalog(loop)['sources']
    return loop, entry, searches, pages


def test_page_view_keeps_snapshot_and_page_citations_separate(research):
    loop, entry, searches, pages = research
    original = loop.tool('pw_read', {'source_id': entry['id']})
    assert not pages
    page = loop.tool('pw_read', {'source_id': entry['id'], 'view': 'page'})
    assert page['id'] != original['id'] and page['collection_parent'] == original['id']
    assert page['source_type'] == 'public_page' and page['read_scope'] == 'full'
    assert page['evidence_version_id'] != original['evidence_version_id']
    for source, quote in [(page, 'Synthetic original article evidence'), (original, 'Public source excerpt')]:
        loop.tool('pw_record', {'kind': 'business', 'source_id': source['id'], 'quote': quote,
                               'text': quote, 'label': 'Synthetic evidence'})
    for row in loop.state['records']:
        [ref] = row['refs']
        saved = loop.pipeline.evidence.get(loop.project_id, ref['evidence_version_id'])
        assert saved['source_type'] == ref['source_type']
        assert saved['parts'][ref['part_index']]['text'][ref['start']:ref['end']] == ref['quote']
    assert loop.tool('pw_read', {'source_id': entry['id']}) == original
    assert loop.tool('pw_read', {'source_id': page['id']})['excerpt'] == page['excerpt']
    assert loop.tool('pw_read', {'source_id': entry['id'], 'view': 'page'})['id'] == page['id']
    assert pages == [entry['url']] and searches == ['base']
    assert loop.execution.usage()['page']['requests'] == 1


def test_page_journal_replay_after_source_checkpoint_interruption(research, monkeypatch):
    loop, entry, _, pages = research
    checkpoint = loop.execution.checkpoint

    def interrupted(stage, value):
        if stage.startswith('dsh_source_P') and value.get('read_attempted'):
            raise RuntimeError('synthetic checkpoint interruption')
        return checkpoint(stage, value)

    monkeypatch.setattr(loop.execution, 'checkpoint', interrupted)
    with pytest.raises(RuntimeError, match='checkpoint interruption'):
        loop.tool('pw_read', {'source_id': entry['id'], 'view': 'page'})
    monkeypatch.setattr(loop.execution, 'checkpoint', checkpoint)
    resumed = PublicBootstrap(loop.pipeline, loop.execution, loop.collectors, loop.reader)
    page = resumed.tool('pw_read', {'source_id': entry['id'], 'view': 'page'})
    assert page['source_type'] == 'public_page' and pages == [entry['url']]


def test_unavailable_page_retains_summary_scope_without_repeated_requests(research):
    loop, entry, _, pages = research

    def unavailable(url):
        pages.append(url)
        return {'status': 'unavailable', 'text': '', 'error_code': 'http_failure'}

    loop.reader.read = unavailable
    page = loop.tool('pw_read', {'source_id': entry['id'], 'view': 'page'})
    assert page['read_scope'] == 'summary' and page['source_type'] == 'search_summary'
    assert page['evidence_version_id'] == entry['evidence_version_id']
    loop.tool('pw_read', {'source_id': entry['id'], 'view': 'page'})
    assert pages == [entry['url']]


def test_unknown_page_blocks_other_requests_and_never_retries(research):
    loop, entry, searches, pages = research

    def unknown(url):
        pages.append(url)
        raise HttpError('connection_failed')

    loop.reader.read = unknown
    with pytest.raises(HttpError):
        loop.tool('pw_read', {'source_id': entry['id'], 'view': 'page'})
    with pytest.raises(HttpError, match='request_outcome_unknown'):
        loop.tool('pw_read', {'source_id': entry['id'], 'view': 'page'})
    with pytest.raises(HttpError, match='request_outcome_unknown'):
        loop.tool('pw_search', {'query': 'extra', 'category': 'company'})
    assert pages == [entry['url']] and searches == ['base']
    assert loop.execution.usage()['page']['unknown'] == 1


def test_partial_fetch_uses_page_windows_not_snapshot_fields(research):
    loop, entry, _, pages = research
    calls = []

    def fetch(url):
        calls.append(url)
        return {'text': 'x' * 12500 + 'Synthetic later article evidence'}

    loop.collectors.deepseek_fetch = fetch
    first = loop.tool('pw_read', {'source_id': entry['id'], 'view': 'page'})
    assert first['source_type'] == 'public_page' and first['read_scope'] == 'partial'
    last = loop.tool('pw_read', {'source_id': first['id'], 'offset': first['next_offset']})
    assert 'Synthetic later article evidence' in last['excerpt'] and last['next_offset'] is None
    assert calls == [entry['url']] and not pages
    assert loop.source(entry['id'])['source_type'] == 'search_summary'


def test_page_news_projection_reuses_collected_item(research):
    loop, entry, _, _ = research
    page = loop.tool('pw_read', {'source_id': entry['id'], 'view': 'page'})
    loop.tool('pw_record', {'kind': 'company', 'source_id': page['id'],
                           'quote': page['excerpt'], 'text': page['excerpt'], 'label': 'Synthetic article'})
    loop.tool('pw_finish', {'gaps': {}})
    with closing(loop.store._connect()) as db:
        rows = db.execute('SELECT id,category,body_status FROM studio_items WHERE project_id=?',
                          (loop.project_id,)).fetchall()
    assert len(rows) == 1 and rows[0]['id'] == entry['matches'][0]['item_id']
    assert rows[0]['category'] == 'company' and rows[0]['body_status'] == 'article_body'


def test_unopened_page_and_invalid_view_cannot_dispatch(research):
    loop, entry, _, pages = research
    for args in [{'source_id': 'P' + entry['id'][1:]},
                 {'source_id': entry['id'], 'view': 'arbitrary'}]:
        with pytest.raises(ValueError):
            loop.tool('pw_read', args)
    assert not pages


def test_page_budget_exhaustion_preserves_snapshot_without_dispatch(research):
    loop, entry, searches, pages = research
    loop.execution.budgets['page'] = 0
    with pytest.raises(HttpError, match=r'^request_budget$'):
        loop.tool('pw_read', {'source_id': entry['id'], 'view': 'page'})
    assert not pages and searches == ['base']
    assert loop.execution.read_checkpoint('dsh_source_' + entry['id'])['read_scope'] == 'summary'
