"""Provider contracts against trimmed REAL public responses (tests/fixtures/studio_live).

Synthetic fixtures alone let the patent page parser pass every test while
failing every live page, and let one blank-titled record discard a whole real
Europe PMC page. These tests run the production adapters on captured shapes.
No network: each fixture body is served back to the adapter as its response.
"""
import json
from copy import deepcopy
from pathlib import Path

import pytest

from project_watch.studio import europepmc, google_patents
from project_watch.studio.http import Http, Response

LIVE = Path(__file__).parent / 'fixtures' / 'studio_live'


def fixture(name):
    return json.loads((LIVE / name).read_text(encoding='utf-8'))


def serve(body, fmt='json'):
    raw = body.encode('utf-8') if fmt in ('xml', 'html') else json.dumps(body).encode('utf-8')
    return Http(lambda *_args: Response(200, {}, raw))


@pytest.mark.parametrize('path', sorted(LIVE.glob('*.json')), ids=lambda p: p.name)
def test_every_live_fixture_satisfies_the_release_rule(path):
    from scripts.release_audit import _public_studio_fixture

    assert _public_studio_fixture('tests/fixtures/studio_live/' + path.name, path.read_bytes())


def test_europepmc_affiliation_real_page():
    data = fixture('moderna-europepmc.json')
    page = europepmc.page(serve(data['response']['body']), 'ModernaTX', mode='affiliation')
    assert len(page['items']) == data['provenance']['records'] and page['dropped_records'] == 0
    for item in page['items']:
        extra = item['extra']
        assert item['kind'] == 'paper' and extra['source_identity'].startswith('europepmc:')
        assert extra['date_basis'] == 'firstPublicationDate' and item['published_at']
    # Provider affiliations are kept verbatim; nothing is inferred from the query.
    assert any(any('Moderna' in a for a in item['extra']['affiliations']) for item in page['items'])


def test_europepmc_patent_real_page_keeps_kind_and_never_asserts_legal_status():
    data = fixture('moderna-europepmc_patent.json')
    page = europepmc.page(serve(data['response']['body']), 'Moderna', mode='patent')
    assert len(page['items']) == data['provenance']['records']
    for item in page['items']:
        extra = item['extra']
        assert item['kind'] == 'patent' and extra['legal_status'] == 'not_provided'
        assert extra['patent_kind'] and item['published_at'] is None  # first date is not a news day


def test_one_malformed_record_is_dropped_and_counted_not_the_page():
    body = deepcopy(fixture('moderna-europepmc_patent.json')['response']['body'])
    body['resultList']['result'][1]['title'] = ' '  # as returned live for one record
    page = europepmc.page(serve(body), 'Moderna', mode='patent')
    assert len(page['items']) == len(body['resultList']['result']) - 1 and page['dropped_records'] == 1
    # A structurally broken page still fails closed.
    broken = deepcopy(body)
    broken['resultList'] = {'result': 'not-a-list'}
    with pytest.raises(Exception, match='invalid_collection_response'):
        europepmc.page(serve(broken), 'Moderna', mode='patent')


def test_google_patents_real_page():
    data = fixture('moderna-google_patent.json')
    html = data['response']['body']
    record = google_patents.parse(html, 'WO2012019168A2')
    assert record['applicants'] == ['Moderna Therapeutics Inc'] and record['legal_status'] == 'unknown'
    assert record['document_kind'] == 'A2' and record['publication_date'] == '2012-02-09'
    assert record['other_versions'] == ['WO2012019168A3']  # same application, listed by the provider
    assert google_patents.metadata(record) == record
