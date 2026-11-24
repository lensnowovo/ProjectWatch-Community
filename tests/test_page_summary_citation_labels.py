"""Page summary citations show the source's title, not the local key it is stored under."""
from contextlib import closing

import test_collection_ingest as fixtures
from test_collection_ingest import trial

from project_watch.studio.page_summaries import _shown


def reference(label, url=''):
    return dict(code='S1', kind='public', label=label, text='', url=url, date='', source_id='e', source_name='', locator='')


def test_keys_become_titles_links_and_plain_labels(tmp_path):
    research, unit, ingest = fixtures.host.__wrapped__(tmp_path)
    ingest(unit, [trial()])
    pid = research.project_id
    with closing(research.store._connect()) as db:
        item_id, title = db.execute('SELECT id,title FROM studio_items WHERE project_id=?', (pid,)).fetchone()
        shown = _shown(db, pid, reference('item:' + item_id))
        assert shown['label'] == title and shown['url'].startswith('https://')
        assert _shown(db, pid, reference('competition:summary:abc'))['label'] == '同行研究摘要（本机整理）'
        profile = _shown(db, pid, reference('public-profile:https://synthetic.example/profile'))
        assert profile['label'] == '公开档案来源' and profile['url'] == 'https://synthetic.example/profile'
        ordinary = reference('Synthetic report title', 'https://synthetic.example/a')
        assert _shown(db, pid, ordinary) == ordinary  # a real title is left as it is
