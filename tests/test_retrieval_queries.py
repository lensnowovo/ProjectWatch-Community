"""Bounded ordinary collection, synthetic companies only; never real transport."""
import json
import re
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


def patent_html(number='US123456A1', applicant='Synthetic Institute', family='987654', current='Other Owner',
                versions=('US999998A1',)):
    """Synthetic values in the live result-page layout.

    Layout observed on public US B2, WO A1 and TW A pages (2026-10-30); no
    provider HTML is committed. One top-level article item; the title is a bare
    span; the first dl closes early, so owners and dates are bare dd rows. Nested
    items (other versions, events, family) repeat other numbers, dates and
    owners as decoys. The live pages carry no familyId; family is optional here.
    """
    def day(key, value):
        return f'<dd><time itemprop="{key}" datetime="{value}">{value}</time></dd>'
    kind = re.search(r'[A-Z][0-9]*$', number)[0]
    current_row = f'<dt>Current Assignee</dt><dd itemprop="assigneeCurrent" repeat>\n  {current}\n</dd>' if current else ''
    return ('<html><head><title>' + number + ' - Google Patents</title></head><body>'
        '<article class="result" itemscope itemtype="http://schema.org/ScholarlyArticle">'
        f'<h1 itemprop="pageTitle">{number} - Synthetic protein disclosure - Google Patents</h1>'
        '<span itemprop="title">Synthetic protein disclosure\n  </span>'
        f'<dl><dt>Publication number</dt><dd itemprop="publicationNumber">{number}</dd>'
        f'<meta itemprop="kindCode" content="{kind}"><span>{number}</span>'
        '<dt>Legal status</dt><dd itemprop="legalStatusIfi" itemscope><span itemprop="status">Active</span></dd></dl>'
        '<dt>Application number</dt><dd itemprop="applicationNumber">US2020000001</dd>'
        '<dt>Other versions</dt>' + ''.join('<dd itemprop="directAssociations" itemscope repeat>'
        f'<a href="/patent/{v}/en"><span itemprop="publicationNumber">{v}</span>'
        '(<span itemprop="primaryLanguage">en</span>)</a></dd>' for v in versions) +
        '<dt>Inventor</dt><dd itemprop="inventor" repeat>Synthetic Person</dd>'
        + current_row +
        f'<dt>Original Assignee</dt><dd itemprop="assigneeOriginal" repeat>{applicant}</dd>'
        + day('priorityDate', '2019-01-02') + day('filingDate', '2020-01-02') + day('publicationDate', '2021-02-03')
        + (f'<dt>Family</dt><dd itemprop="familyId">{family}</dd>' if family else '') +
        '<dd itemprop="events" itemscope repeat><time itemprop="date" datetime="2018-05-05">2018-05-05</time>'
        '<span itemprop="title">Application filed by Decoy Holder</span></dd>'
        '<section itemprop="family" itemscope><h1>Family</h1><span itemprop="priorityDate">2017-07-07</span>'
        '<span itemprop="assigneeOriginal">Decoy Holder</span><span itemprop="publicationNumber">US999997A1</span></section>'
        '</article></body></html>')


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


def test_live_layout_fields_come_only_from_the_documents_own_item():
    """Title outside h1/dl, owners/dates outside dl; nested items are decoys."""
    record = google_patents.parse(patent_html(family=None), 'US123456A1')
    assert record['title'] == 'Synthetic protein disclosure'
    assert record['applicants'] == ['Synthetic Institute'] and record['current_assignees'] == ['Other Owner']
    assert record['application_number'] == 'US2020000001' and record['inventors'] == ['Synthetic Person']
    assert (record['priority_date'], record['filing_date'], record['publication_date']) == (
        '2019-01-02', '2020-01-02', '2021-02-03')
    # Listed other versions are kept as numbers only; they never become this
    # document's identity. Events and the family section never leak in.
    assert record['other_versions'] == ['US999998A1'] and record['publication_number'] == 'US123456A1'
    flat = json.dumps({k: v for k, v in record['field_evidence'].items() if k != 'otherVersion'})
    for decoy in ('US999998A1', 'US999997A1', 'Decoy Holder', '2017-07-07', '2018-05-05', 'Application filed'):
        assert decoy not in flat
    # No familyId on the live page: the document stays independent, not guessed.
    assert record['family_id'] is None
    assert google_patents.metadata(record) == record


def test_application_is_not_promoted_to_grant_and_missing_owner_stays_missing():
    record = google_patents.parse(patent_html(number='WO2099123456A1', current=None, family=None), 'WO2099123456A1')
    assert record['document_kind'] == 'A1' and record['legal_status'] == 'unknown'
    assert record['current_assignees'] == []  # absent field is not filled from the original or a decoy
    granted = google_patents.parse(patent_html(number='US1234567B2', family=None), 'US1234567B2')
    assert granted['document_kind'] == 'B2' and granted['legal_status'] == 'unknown'


def test_second_top_level_item_cannot_supply_or_override_fields():
    html = patent_html(family=None).replace('<article', '<div itemscope itemtype="http://schema.org/WebSite">'
        '<span itemprop="title">Other page item</span><dd itemprop="publicationNumber">US999999A1</dd></div><article', 1)
    with pytest.raises(HttpError, match=r'invalid_patent_document_(publicationNumber|identity)'):
        google_patents.parse(html, 'US123456A1')
    # When the foreign item follows the document, it is ignored outright.
    later = patent_html(family=None).replace('</body>', '<div itemscope><dd itemprop="assigneeOriginal">Wrong Owner</dd>'
                                             '<dd itemprop="publicationNumber">US999999A1</dd></div></body>')
    record = google_patents.parse(later, 'US123456A1')
    assert record['applicants'] == ['Synthetic Institute']


def test_owner_named_only_in_a_nested_citation_is_not_the_applicant():
    """A citation row naming the monitored company does not make it the applicant."""
    html = patent_html(applicant='Unrelated Applicant Co', current=None, family=None).replace(
        '</article>', '<table><tr itemprop="backwardReferencesFamily" itemscope><td>'
        '<span itemprop="assigneeOriginal">Synthetic Institute</span></td></tr></table></article>')
    record = google_patents.parse(html, 'US123456A1')
    assert record['applicants'] == ['Unrelated Applicant Co'] and record['current_assignees'] == []


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


def test_refused_product_fallback_is_not_recorded_and_allowed_one_is(tmp_path):
    """A refused, never-dispatched query keeps no action slot or receipt-less started action."""
    from project_watch.studio import bootstrap_collection

    loop, args, calls = setup(tmp_path, empty=True)
    role = bootstrap_collection.Collection(loop)
    role.retrieval_contract = 'evidence-query-v2'
    product = args | dict(adapter='europepmc_topic', query='SYN-101', relationship='product')
    before = len(calls)
    with pytest.raises(ValueError, match='product_fallback_requires'):
        role.tool('pw_collect', product)  # no same-subject institution receipt yet
    assert role.data['actions'] == {} and len(calls) == before
    assert role.execution.read_checkpoint('collection_product_dimension_v2') is None
    # Same subject's institution query returns zero results: the fallback opens once.
    role.tool('pw_collect', args | dict(adapter='europepmc_affiliation', query='Synthetic Institute'))
    result = role.tool('pw_collect', product)
    assert result['collection']['outcome'] == 'success'
    assert [a['status'] for a in role.data['actions'].values()] == ['complete', 'complete']
    assert role.execution.read_checkpoint('collection_product_dimension_v2')['query'] == 'SYN-101'
    # A second product dimension is refused before recording, too.
    with pytest.raises(ValueError, match='one_product_dimension'):
        role.tool('pw_collect', args | dict(adapter='europepmc_topic', query='SYN-202', relationship='product'))
    assert len(role.data['actions']) == 2


def test_listed_versions_are_validated_numbers_with_provenance():
    html = patent_html(versions=('US123456A1', 'US123456B2', 'not-a-number', 'US123456B2'), family=None)
    record = google_patents.parse(html, 'US123456A1')
    assert record['other_versions'] == ['US123456B2']  # self, malformed and repeated numbers are dropped
    assert google_patents.metadata(record) == record
    tampered = dict(record, other_versions=['US123456B2', 'US777777A1'])
    with pytest.raises(ValueError, match='versions changed'):
        google_patents.metadata(tampered)


def test_application_and_grant_group_only_through_their_own_listed_versions(tmp_path):
    """No family ID on the page: link A1/B2 by listed versions, never by title or applicant."""
    loop, args, calls = setup(tmp_path)
    pages = {'US123456A1': patent_html('US123456A1', family=None, versions=('US123456B2',)),
             'US123456B2': patent_html('US123456B2', family=None, versions=('US123456A1',))}

    def send(method, url, headers, raw, timeout):
        calls.append(url)
        if urlsplit(url).hostname != 'patents.google.com':
            pytest.fail('unexpected source: ' + url)
        return Response(200, {}, pages[url.split('/')[-2]].encode())

    loop.pipeline.http_factory = lambda _: Http(send)
    loop.collectors = loop.pipeline.tools()[3]
    for number in pages:
        result = loop.tool('pw_collect', args | dict(adapter='google_patent', query=number, relationship='unverified'))
        assert result['collection']['outcome'] == 'success'
    page = research_sources(loop.store, loop.project_id, 'patent')
    groups = {r['extra']['patent_number']: r['family'] for r in page['items']}
    assert groups['US123456A1']['key'] == groups['US123456B2']['key'] == 'versions:US123456A1'
    assert groups['US123456A1']['basis'] == 'listed_versions' and groups['US123456B2']['document_count'] == 2
    assert page['family_count'] == 1 and page['total'] == 2  # both documents are kept, not merged
    assert all(r['extra']['legal_status'] == 'unknown' for r in page['items'])
    assert not loop.store.list_events(project_id=loop.project_id)


def test_unlinked_documents_and_provider_family_keep_their_own_grouping():
    from project_watch.studio.research_sources import _patent_groups

    def item(i, number, versions=(), family=None):
        extra = {'patent_number': number, 'other_versions': list(versions)}
        if family:
            extra['family_id'] = family
        return {'id': i, 'extra': extra}

    keys = _patent_groups([
        item('a', 'US1000001A1', ['US9999999B2']),            # lists a document that was not collected
        item('b', 'US2000002A1'),                              # same applicant/title would not matter
        item('c', 'EP3000003A1', ['US1000001A1'], family='google-patents:42'),  # provider family wins
        item('d', 'WO4000004A1', ['CN5000005A']), item('e', 'CN5000005A')])     # one-sided listing links
    assert keys['a'] == ('document:a', 'document') and keys['b'] == ('document:b', 'document')
    assert keys['c'] == ('google-patents:42', 'family_id')
    assert keys['d'] == keys['e'] == ('versions:CN5000005A', 'listed_versions')


def test_validation_refusal_is_forgotten_but_a_journaled_request_is_not(tmp_path):
    from project_watch.studio import bootstrap_collection

    loop, args, calls = setup(tmp_path)
    role = bootstrap_collection.Collection(loop)
    bad = args | dict(adapter='europepmc_affiliation', query='Synthetic Institute', quote='not in the excerpt')
    before = len(calls)
    with pytest.raises(ValueError, match='exact public quote'):
        role.tool('pw_collect', bad)
    assert role.data['actions'] == {} and len(calls) == before  # refused before dispatch: no slot, no started

    original = loop.tool

    def sent_then_failed(name, tool_args):
        # A request reaches the journal, then local processing fails.
        loop.execution.call('open_data', 'synthetic-sent', lambda: Response(200, {}, b'{}'), provider='synthetic')
        raise ValueError('post-dispatch processing failed')

    loop.tool = sent_then_failed
    try:
        with pytest.raises(ValueError, match='post-dispatch'):
            role.tool('pw_collect', args | dict(adapter='europepmc_affiliation', query='Synthetic Institute'))
    finally:
        loop.tool = original
    assert [a['status'] for a in role.data['actions'].values()] == ['started']  # conservative, as before
