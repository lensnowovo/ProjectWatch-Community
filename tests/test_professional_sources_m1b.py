"""Professional sources through real collection/ingestion with synthetic wire."""

import pytest
from test_collection_pages import wire, work

from project_watch.public_article import extract_article
from project_watch.studio.collection_pages import openalex_page, public_read_request
from project_watch.studio.europepmc import BASE, open_body_url, page
from project_watch.studio.http import Http, HttpError, Response
from project_watch.studio.settings import SearchConfig
from project_watch.studio.sources import Collectors


@pytest.mark.parametrize('query', ['Example Biologics, Inc.', "O'Brien & Co. (Research), Ltd.", '研究机构，生物部'])
def test_legal_punctuation_shared_by_paged_and_legacy(query):
    calls = []
    http = wire({'results': [work()], 'meta': {'count': 1}}, calls)
    openalex_page(http, query, mode='affiliation', end='2026-10-29')
    Collectors(http, SearchConfig()).openalex(query, affiliation=True)
    assert calls[0]['filter'] == ['raw_affiliation_strings.search:' + query + ',to_publication_date:2026-10-29']
    assert calls[1]['filter'] == ['raw_affiliation_strings.search:' + query]


@pytest.mark.parametrize('query', ['Example,type:book', 'Example, type :book', 'A|B', '!Example', '', 'a\nb'])
def test_invalid_scope_fails_before_any_http(query):
    http = Http(lambda *_: pytest.fail('must not dispatch'))
    for action in (lambda: openalex_page(http, query, mode='affiliation'),
                   lambda: Collectors(http, SearchConfig()).openalex(query, affiliation=True)):
        with pytest.raises(HttpError, match='invalid_collection_query'):
            action()


@pytest.mark.parametrize('status,code', [(400, 'invalid_collection_query'), (403, 'http_source_access_denied'),
    (429, 'http_source_rate_limited'), (503, 'http_source_unavailable')])
def test_received_error_is_classified_once_without_echo(status, code):
    calls = []
    def send(*_):
        calls.append(1)
        return Response(status, {}, b'private echoed request')
    with pytest.raises(HttpError) as error:
        openalex_page(Http(send), 'Synthetic')
    assert error.value.code == code and error.value.status == status
    assert not error.value.detail and len(calls) == 1


def test_unknown_transport_is_not_reclassified():
    def send(*_):
        raise HttpError('request_outcome_unknown')
    with pytest.raises(HttpError, match='request_outcome_unknown'):
        openalex_page(Http(send), 'Synthetic')


def paper(n=1):
    return dict(id=str(n), source='MED', pmcid=f'PMC{n}', title='Synthetic biomedical paper',
        firstPublicationDate='2026-10-20', doi=f'10.1/{n}', abstractText='Synthetic abstract',
        isOpenAccess='Y', license='CC BY', authorList={'author': [{'fullName': 'Synthetic Author',
        'authorAffiliationDetailsList': {'authorAffiliation': [{'affiliation': 'Synthetic, Inc.'}]}}]})


def response(rows=None, **changes):
    return dict(hitCount=1, resultList={'result': [paper()] if rows is None else rows}, **changes)


def test_epmc_preserves_metadata_and_literal_scope():
    calls = []
    result = page(wire(response(), calls), 'Synthetic, Inc.', mode='affiliation', start='2026-01-01', end='2026-10-29')
    query = calls[0]['query'][0]
    assert query == 'AFFILIATION:"Synthetic, Inc." AND (SRC:MED OR SRC:PMC) AND FIRST_PDATE:[2026-01-01 TO 2026-10-29] sort_date:y'
    item = result['items'][0]
    assert item['extra']['source_identity'] == 'europepmc:MED:1'
    assert item['extra']['source_record'] == paper()
    assert item['extra']['affiliations'] == ['Synthetic, Inc.']
    assert open_body_url({'source': 'Europe PMC', 'source_record': paper()}) == BASE + 'PMC1/fullTextXML'
    assert not result['truncated']


@pytest.mark.parametrize('data', [{}, {'hitCount': True, 'resultList': {'result': []}},
    {'hitCount': 1, 'resultList': {'result': []}}, {'hitCount': 1, 'resultList': {'result': [{}]}}])
def test_epmc_malformed_is_not_zero(data):
    with pytest.raises(HttpError):
        page(wire(data), 'Synthetic')


def test_epmc_zero_and_pagination():
    empty = page(wire({'hitCount': 0, 'resultList': {'result': []}}), 'Synthetic')
    assert empty['items'] == [] and not empty['truncated']
    data = dict(hitCount=3, resultList={'result': [paper()]}, nextCursorMark='next')
    result = page(wire(data), 'Synthetic', per_page=1)
    assert result['next_cursor'] == 'next' and result['truncated']
    with pytest.raises(HttpError, match='invalid_collection_cursor'):
        page(wire(data), 'Synthetic', per_page=1, cursor='next')


def test_patent_record_is_structured_but_not_legal_status_or_publication_date():
    row = paper() | dict(id='US12345', source='PAT', affiliation='Synthetic University',
                        patentDetails={'typeCode': 'A1', 'countryCode': 'US',
                                       'application': {'applicationDate': '2026-10-20'}})
    result = page(wire(response([row])), 'Synthetic', mode='patent')
    item = result['items'][0]
    assert item['kind'] == 'patent' and item['published_at'] is None
    assert item['extra']['source_record']['patentDetails'] == row['patentDetails']
    assert item['extra']['legal_status'] == 'not_provided'
    assert item['extra']['family_status'] == 'not_provided'
    assert item['extra']['provider_affiliation'] == 'Synthetic University'
    assert not item['extra'].get('applicants')  # affiliation is not proven ownership
    assert result['coverage_kind'] == 'biomedical_patent_index'
    assert result['date_basis'] == item['extra']['date_basis'] == 'provider_first_date_unverified'


@pytest.mark.parametrize('change', [{'isOpenAccess': 'N'}, {'pmcid': 'PMC1/../../private'}, {'pmcid': None}])
def test_no_closed_or_arbitrary_fulltext_endpoint(change):
    assert open_body_url({'source': 'Europe PMC', 'source_record': paper() | change}) is None


def jats():
    return ('<article><front><article-meta><article-id pub-id-type="pmc">1</article-id>'
            '<permissions><license><license-p>CC BY synthetic.</license-p></license></permissions>'
            '</article-meta></front><body><sec><title>Synthetic findings</title><p>'
            + 'Synthetic methods and findings. ' * 20 + '</p></sec></body></article>').encode()


def test_xml_identity_license_entities_and_partial_scope():
    url = BASE + 'PMC1/fullTextXML'
    good = extract_article(jats(), 'application/xml', source_url=url)
    assert good['status'] == 'partial_body' and 'CC BY synthetic.' in good['text']
    bad = extract_article(jats(), 'application/xml', source_url=BASE + 'PMC2/fullTextXML')
    assert bad['error_code'] == 'paper_identity_mismatch' and not bad['text']
    entity = b'<!DOCTYPE article [<!ENTITY x "boom">]>' + jats()
    assert not extract_article(entity, 'application/xml', source_url=url)['text']
    assert not extract_article(jats().replace(b'<license>', b'<not-license>').replace(b'</license>', b'</not-license>'),
                               'application/xml', source_url=url)['text']
    assert extract_article(jats(), 'application/xml', source_url='https://example.com/body')['error_code'] == 'unsupported_media_type'




def test_public_endpoint_contract_does_not_extend_to_credentials_or_other_routes():
    url = BASE + 'search?query=Synthetic&format=json'
    assert public_read_request('GET', url, {}, None)
    assert not public_read_request('GET', url, {'Authorization': 'secret'}, None)
    assert not public_read_request('POST', url, {}, None)
    assert not public_read_request('GET', BASE + 'private', {}, None)
