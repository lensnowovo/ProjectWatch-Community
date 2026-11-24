"""An optional OpenAlex key moves paper reads off the budget shared per network IP."""
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from project_watch.studio.collection_pages import openalex_page, public_read_request
from project_watch.studio.settings import SettingsStore

KEY = 'synthetic-openalex-key-0001'


def test_the_key_is_saved_masked_kept_and_cleared(tmp_path):
    store = SettingsStore(tmp_path / 'settings.json')
    saved = store.update({'search': {'openalex_key': KEY}})
    assert saved.search.openalex_key == KEY
    view = SettingsStore.public_view(saved)
    assert KEY not in str(view) and view['search']['openalex_key']
    kept = store.update({'search': {'providers': ['deepseek'], 'keys': {'deepseek': 'synthetic-deepseek'}}})
    assert kept.search.openalex_key == KEY  # changing search services does not drop it
    assert store.update({'search': {'openalex_key': ''}}).search.openalex_key == KEY  # empty keeps it
    assert store.update({'search': {'clear_openalex_key': True}}).search.openalex_key == ''


def test_a_paper_page_carries_the_key_and_stays_a_public_read():
    seen = []

    def get_json(url):
        seen.append(url)
        return {'results': [], 'meta': {'count': 0}}
    openalex_page(SimpleNamespace(get_json=get_json), 'synthetic battery chemistry', api_key=KEY)
    assert parse_qs(urlsplit(seen[0]).query)['api_key'] == [KEY]
    assert public_read_request('GET', seen[0], {}, None)
    openalex_page(SimpleNamespace(get_json=get_json), 'synthetic battery chemistry')
    assert 'api_key' not in parse_qs(urlsplit(seen[1]).query)  # no key: unchanged request
