"""Bounded ordinary collection, synthetic companies only; never real transport."""
import json
from contextlib import closing
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
import test_collection_supplements as fixtures

from project_watch.studio import (
    collection_dsh,
    collection_targets,
    google_patents,
    research_records,
    retrieval_queries,
)
from project_watch.studio.collection_pages import public_read_request
from project_watch.studio.http import Http, HttpError, Response
from project_watch.studio.research_sources import research_sources


def patent_html(number='US123456A1', applicant='Synthetic Institute', family='987654', current='Other Owner'):
    fields = dict(publicationNumber=number, applicationNumber='US2020000001', title='Synthetic protein disclosure',
        assigneeOriginal=applicant, assigneeCurrent=current, inventor='Synthetic Person',
        filingDate='2020-01-02', priorityDate='2019-01-02', publicationDate='2021-02-03', familyId=family)
    return '<html><dl>' + ''.join(
        f'<dd><time itemprop="{k}" datetime="{v}">{v}</time></dd>' if k.endswith('Date') else
        f'<dd itemprop="{k}">{v}</dd>' for k, v in fields.items()) + '</dl></html>'


def test_document_is_third_party_publication_not_grant_or_current_owner():
    record = google_patents.parse(patent_html(number='US123456B2'), 'US123456B2')
    assert record['applicants'] == ['Synthetic Institute'] and record['current_assignees'] == ['Other Owner']
    assert record['legal_status'] == 'unknown' and record['document_kind'] == 'B2'
    assert record['publication_date'] == '2021-02-03' and record['filing_date'] == '2020-01-02'
    meta = research_records.metadata({'source_record': record})
    assert meta['kind'] == 'patent' and meta['publication_date'] == '2021-02-03'
    assert meta['provider_kind'] == 'third_party_index' and meta['family_id'] == 'google-patents:987654'


@pytest.mark.parametrize('change', ['identity', 'applicant', 'ambiguous', 'invalid_date', 'challenge'])
def test_invalid_documents_fail_closed(change):
    html = patent_html()
    if change == 'identity':
        html = html.replace('US123456A1', 'US999999A1')
    elif change == 'applicant':
        html = html.replace('assigneeOriginal', 'assigneeCurrent')
    elif change == 'ambiguous':
        html = html.replace('</dl>', '<dd itemprop="publicationNumber">US999999A1</dd></dl>')
    elif change == 'invalid_date':
        html = html.replace('2021-02-03', 'Feb 2021')
    else:
        html = '<html>Verify that you are human</html>'
    with pytest.raises(HttpError, match='invalid_patent'):
        google_patents.parse(html, 'US123456A1')


def test_exact_public_read_route_only():
    root = 'https://patents.google.com/patent/US123456A1/en'
    assert public_read_request('GET', root, {}, None)
    for url in (root + '?q=other', root.replace('/en', '/pdf'), root.replace('patents.google.com', 'evil.invalid')):
        assert not public_read_request('GET', url, {}, None)
    assert not public_read_request('GET', root, {'Authorization': 'synthetic'}, None)
    with pytest.raises(ValueError):
        google_patents.publication_number('../US123456A1')


def setup(tmp_path, *, applicant='Synthetic Institute', failure=False, empty=False):
    loop, _ = fixtures.research.__wrapped__(tmp_path)
    calls = []
    disclosure = ('Synthetic Host uses the name Synthetic Institute and develops SYN-101 and SYN-202. '
                  'Synthetic Institute filed US123456A1 and US123456B2. '
                  'Other Host uses Unrelated Institute and develops OTH-101.')

    def send(method, url, headers, raw, timeout):
        calls.append(url)
        host = urlsplit(url).hostname
        if host == 'api.tavily.com':
            data = {'results': [dict(title='Synthetic disclosure', url='https://example.invalid/company', content=disclosure)]}
        elif host == 'www.ebi.ac.uk':
            data = {'hitCount': 1, 'resultList': {'result': [dict(source='MED', id='123456', title='Synthetic research',
                firstPublicationDate='2020-01-01', authorList={'author': [dict(fullName='Synthetic Author',
                    authorAffiliationDetailsList={'authorAffiliation': [dict(affiliation=applicant)]})]})]}}
            if empty:
                data = {'hitCount': 0, 'resultList': {'result': []}}
        elif host == 'patents.google.com':
            return Response(200, {}, ('<html>challenge</html>' if failure else patent_html(url.split('/')[-2], applicant)).encode())
        else:
            pytest.fail('unexpected source: ' + host)
        return Response(200, {}, json.dumps(data).encode())

    loop.pipeline.http_factory = lambda _: Http(send)
    loop.collectors = loop.pipeline.tools()[3]
    found = loop.tool('pw_search', dict(query='disclosure', category='company'))
    args = dict(source_id=found['sources'][0]['id'], quote=disclosure, reason='Public name and product hypothesis',
                subject='Synthetic Host', relationship='same_entity')
    return loop, args, calls


@pytest.mark.parametrize('applicant', ['Synthetic Institute', 'Unrelated Institute'])
def test_ordinary_specialist_path_retains_bridge_without_claiming_affiliation(tmp_path, applicant):
    loop, args, calls = setup(tmp_path, applicant=applicant)
    result = loop.tool('pw_collect', args | dict(adapter='europepmc_affiliation', query='Synthetic Institute'))
    assert result['collection']['outcome'] == 'success'
    [row] = research_sources(loop.store, loop.project_id, 'paper')['items']
    meta = research_records.evidence_metadata(loop.store, loop.project_id, row['evidence_version_id'])
    assert meta['affiliations'] == [applicant]
    assert meta['query_context']['relationship'] == 'same_entity'
    assert row['extra']['query_relationship']['basis'] == 'query_hypothesis_not_verified_relationship'
    assert not loop.store.list_events(project_id=loop.project_id)
    assert loop.execution.usage()['open_data']['requests'] == 1
    assert len(calls) == 2  # one disclosure search, one actual specialist request
    # Query echo and a Chinese/English name hypothesis never overwrite the
    # provider's actual author institution, even in the unrelated-company case.
    assert row['processing_status'] == 'pending'


def test_patent_document_family_keeps_all_versions_and_original_dates(tmp_path):
    loop, args, calls = setup(tmp_path)
    for number in ('US123456A1', 'US123456B2'):
        result = loop.tool('pw_collect', args | dict(adapter='google_patent', query=number, relationship='unverified'))
        assert result['collection']['outcome'] == 'success'
    page = research_sources(loop.store, loop.project_id, 'patent')
    assert page['total'] == 2 and page['family_count'] == 1
    assert all(r['family']['document_count'] == 2 and r['published_at'].startswith('2021-02-03') for r in page['items'])
    assert len({r['id'] for r in page['items']}) == 2
    assert not loop.store.list_events(project_id=loop.project_id)
    assert len(calls) == 3


def test_document_failure_preserves_sources_and_does_not_repeat(tmp_path):
    loop, args, calls = setup(tmp_path, failure=True)
    args |= dict(adapter='google_patent', query='US123456A1', relationship='unverified')
    result = loop.tool('pw_collect', args)
    assert result['collection']['outcome'] == 'failed'
    before = len(calls)
    loop.tool('pw_collect', args)
    assert len(calls) == before and collection_dsh.catalog(loop)['total'] >= 2
    assert research_sources(loop.store, loop.project_id, 'patent')['total'] == 0


def test_relationship_requires_literal_subject_and_query(tmp_path):
    loop, args, calls = setup(tmp_path)
    with pytest.raises(ValueError, match='shared public excerpt'):
        loop.tool('pw_collect', args | dict(adapter='europepmc_affiliation', query='Synthetic Institute', subject='Other Company'))
    assert len(calls) == 1 and not collection_targets.targets(loop)


@pytest.mark.parametrize('empty', [True, False])
def test_product_fallback_requires_same_subject_receipt_and_one_dimension(tmp_path, empty):
    loop, args, _ = setup(tmp_path, empty=empty, applicant='Unrelated Institute')
    role = SimpleNamespace(parent=loop, execution=loop.execution)
    request = args | dict(adapter='europepmc_topic', query='SYN-101', relationship='product')
    with pytest.raises(ValueError, match='requires_unconfirmed'):
        retrieval_queries.require_product_fallback(role, request)
    # The other subject's successful receipt cannot unlock this product.
    loop.tool('pw_collect', args | dict(adapter='europepmc_affiliation', query='Unrelated Institute', subject='Other Host'))
    with pytest.raises(ValueError, match='requires_unconfirmed'):
        retrieval_queries.require_product_fallback(role, request)
    loop.tool('pw_collect', args | dict(adapter='europepmc_affiliation', query='Synthetic Institute'))
    retrieval_queries.require_product_fallback(role, request)
    retrieval_queries.require_product_fallback(role, request)
    with pytest.raises(ValueError, match='one_product_dimension'):
        retrieval_queries.require_product_fallback(role, request | dict(query='SYN-202'))


def test_matching_affiliation_prevents_product_expansion_without_asserting_ownership(tmp_path):
    loop, args, _ = setup(tmp_path)
    loop.tool('pw_collect', args | dict(adapter='europepmc_affiliation', query='Synthetic Institute'))
    with pytest.raises(ValueError, match='requires_unconfirmed'):
        retrieval_queries.require_product_fallback(SimpleNamespace(parent=loop, execution=loop.execution),
            args | dict(adapter='europepmc_topic', query='SYN-101', relationship='product'))
    assert not loop.store.list_events(project_id=loop.project_id)


def test_patent_time_fields_positions_and_nested_unrelated_records():
    html = patent_html().replace('</dl>', '''
        <table itemprop="backwardReferences"><tr><td><time itemprop="publicationDate" datetime="2030-01-01">2030</time>
        <span itemprop="assigneeOriginal">Wrong Owner</span></td></tr></table></dl>
        <section itemprop="claims"><dl><dd itemprop="publicationNumber">US999999A1</dd></dl></section>''')
    record = google_patents.parse(html, 'US123456A1')
    assert record['publication_date'] == '2021-02-03'
    assert record['applicants'] == ['Synthetic Institute']
    assert all(html[s['start']:s['end']] == s['html'] for s in record['field_spans'])
    [date] = [s for s in record['field_spans'] if s['field'] == 'publicationDate']
    assert date['attribute'] == 'datetime' and date['tag'] == 'time'
    assert google_patents.metadata(record) == record
    record['current_assignees'] = ['Synthetic Institute']
    with pytest.raises(ValueError, match='fields changed'):
        google_patents.metadata(record)


def test_nested_citation_cannot_fill_missing_primary_date():
    html = patent_html().replace('<time itemprop="publicationDate" datetime="2021-02-03">2021-02-03</time>', '')
    html = html.replace('</dl>', '<table><tr><td><time itemprop="publicationDate" datetime="2030-01-01">2030</time></td></tr></table></dl>')
    record = google_patents.parse(html, 'US123456A1')
    assert record['publication_date'] is None and record['date_basis'] == 'unknown'


def test_query_bridge_is_bound_to_independent_review_and_withdrawal(tmp_path):
    loop, args, _ = setup(tmp_path)
    result = loop.tool('pw_collect', args | dict(adapter='europepmc_affiliation', query='Synthetic Institute'))
    source = result['sources'][0]
    meta = research_records.evidence_metadata(loop.store, loop.project_id, source['evidence_version_id'])
    from project_watch.studio.dsh_record_review import payload_for
    version = loop.store.evidence.get(loop.project_id, source['evidence_version_id'], include_internal=False)
    ref = dict(evidence_version_id=version['id'], part_index=0, start=0, end=10, quote=version['parts'][0]['text'][:10])
    row = dict(kind='paper', subject='Synthetic Host', label='Synthetic research', refs=[ref],
               research_metadata=meta, research_relation='author_affiliation')
    payload = payload_for(loop, {'record:one': row})
    assert len(payload['bound_evidence']['record:one']) == 2
    bridge = meta['query_context']['ref']['evidence_version_id']
    with closing(loop.store._connect()) as db, db:
        db.execute("INSERT INTO studio_evidence_lifecycle(project_id,evidence_version_id,status,reason,actor) VALUES (?,?,'withdrawn','test','synthetic')",
                   (loop.project_id, bridge))
    with pytest.raises((KeyError, ValueError)):
        payload_for(loop, {'record:one': row})
