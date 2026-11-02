"""Publication dates for search results, synthetic inputs only."""
import json
from contextlib import closing

from project_watch.studio import publication_dates as dates
from project_watch.studio.sources import _source_name
from project_watch.studio.store import StudioStore


def test_url_then_labelled_page_date_then_provider():
    assert dates.derive('https://example.invalid/news/2026-10-15/a.html', '', None) == ('2026-10-15T00:00:00+00:00', 'url')
    assert dates.derive('https://example.invalid/a', '# 标题  发布时间：2026-06-10  正文', '2026-06-09T16:00:00Z') == (
        '2026-06-10T00:00:00+00:00', 'page')
    assert dates.derive('https://example.invalid/a', '| 时间：2026年08月28日 08:00', None)[1] == 'page'
    assert dates.derive('https://example.invalid/a', '登记号 | CTR20251620 | 首次公示信息日期 | 2025-05-09 |', None) == (
        '2025-05-09T00:00:00+00:00', 'page')
    assert dates.derive('https://example.invalid/a', '正文没有日期', '2026-06-09T16:00:00Z') == ('2026-06-09T16:00:00+00:00', 'search')
    assert dates.derive('https://example.invalid/a', '', None) == (None, None)


def test_other_dates_on_the_page_are_not_publication_dates():
    for head in ('公司成立日期 ： 2019-07-03', '截止时间：2025-01-01', '2026年6月12日，公司递交申请'):
        assert dates.derive('https://example.invalid/a', head, None) == (None, None)
    assert dates.derive('https://example.invalid/id/20260828001936', '', None) == (None, None)  # an ID, not a date
    assert dates.derive('https://example.invalid/2099-01-01/a', '', None) == (None, None)  # future


def test_hex_site_names_decode_including_plain_english():
    assert _source_name('e4babae6b091e7bd91') == '人民网'
    assert _source_name('4153434f205075626c69636174696f6e73') == 'ASCO Publications'
    assert _source_name('deadbeef') == 'deadbeef'


def test_stored_items_get_dates_and_names_once(tmp_path):
    from project_watch.repository import ProjectRepository

    repo = ProjectRepository(tmp_path / 'studio.sqlite3')
    pid = repo.create_project('Synthetic Co').id
    path = repo.db_path
    store = StudioStore(path)
    with closing(store._connect()) as db, db:
        db.execute("DELETE FROM studio_state WHERE key='item_dates_v2'")
        db.execute("INSERT INTO studio_items (id,project_id,key,category,title,url,source,snippet,published_at,collected_at,extra) "
                   "VALUES ('i1',?,'k1','company','t','https://example.invalid/a','e4babae6b091e7bd91','',NULL,'2026-11-01T00:00:00+00:00',?)",
                   (pid, json.dumps({'search_content': '发布时间：2026-09-02 正文', 'provider_date': '2026-09-01T16:00:00Z'})))
    StudioStore(path)
    with closing(store._connect()) as db:
        row = db.execute("SELECT published_at,source,extra FROM studio_items WHERE id='i1'").fetchone()
    assert row['published_at'] == '2026-09-02T00:00:00+00:00' and row['source'] == '人民网'
    assert json.loads(row['extra'])['date_source'] == 'page'
