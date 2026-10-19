"""Synthetic retrieval fixtures, not a live coverage/competition benchmark."""

import json
from types import SimpleNamespace

import pytest
from test_studio_competition_research import research_pipeline
from test_studio_product_matching import product

from project_watch.studio.competition_research import research_inputs
from project_watch.studio.execution import JobExecution


def test_queries_retain_cited_product_and_use_without_inventing_buyer(tmp_path):
    from project_watch.studio.competition_research import original_query_context

    pipeline, pid, _, _ = research_pipeline(tmp_path)
    subject = pipeline.objects.ensure_subject(pid)
    inputs = research_inputs(pipeline, pid)
    assert 'Lens A' in inputs['queries'][0] and 'drug screening' in inputs['queries'][0]
    assert inputs['query_contexts'][0]['product_name'] == 'Lens A'
    target = original_query_context(inputs, subject['name'], target=True)
    peer = original_query_context(inputs, 'Beta', target=False)
    assert target['queries'] == ['Synthetic Optics Lens A drug screening 产品', 'Synthetic Optics Lens A drug screening 官网']
    assert peer['queries'] == ['Beta drug screening 产品', 'Beta drug screening 官网']
    assert peer['products'] == [] and peer['uses'] == ['drug screening']
    assert target['products'] == ['Lens A']
    assert all('客户 交付' not in q for q in target['queries'] + peer['queries'])


def test_context_queries_are_bounded_and_separate_product_contexts(tmp_path):
    from project_watch.studio.competition_research import original_query_context

    pipeline, pid, _, _ = research_pipeline(tmp_path)
    subject = pipeline.objects.ensure_subject(pid)
    for index in range(3):
        subject, _, _ = product(pipeline, pid, subject, f'Synthetic {index}',
            {'use_case': [f'use {index}']}, source=f'extra-{index}')
    inputs = research_inputs(pipeline, pid)
    assert len(inputs['queries']) == 3 and inputs['queries'][-1] == 'Synthetic Optics 竞争对手'  # two product-use queries plus the company query (research-v28)
    ctx = original_query_context(inputs, subject['name'], target=True)
    assert len(ctx['queries']) == 2
    assert all(sum(f'use {i}' in q for i in range(3)) <= 1 for q in ctx['queries'])
    assert '医院' not in ' '.join(ctx['queries'])


def test_single_context_searches_brand_and_cited_use_without_brand(tmp_path):
    pipeline, pid, _, _ = research_pipeline(tmp_path)
    inputs = research_inputs(pipeline, pid)
    assert len(inputs['query_contexts']) == 1
    assert inputs['queries'] == ['Lens A drug screening pharma 竞品', 'drug screening pharma 竞品', 'Synthetic Optics 竞争对手']
    assert inputs['discovery_queries'] == inputs['queries']


def test_complementary_query_does_not_displace_a_second_product(tmp_path):
    pipeline, pid, _, _ = research_pipeline(tmp_path)
    subject = pipeline.objects.ensure_subject(pid)
    product(pipeline, pid, subject, 'Synthetic Second', {'use_case': ['separate use']}, source='second')
    inputs = research_inputs(pipeline, pid)
    assert len(inputs['queries']) == 3 and inputs['queries'][-1] == 'Synthetic Optics 竞争对手'  # two product-use queries plus the company query (research-v28)
    assert any('Lens A' in q for q in inputs['queries'])
    assert any('Synthetic Second' in q and 'separate use' in q for q in inputs['queries'])


def test_repeated_located_context_still_uses_complementary_query(tmp_path):
    from test_studio_evidence_versions import sample

    from project_watch.studio.monitoring_contracts import FactInput, LocatedRef

    pipeline, pid, _, _ = research_pipeline(tmp_path)
    original = research_inputs(pipeline, pid)
    text = 'Synthetic Optics builds Lens A for drug screening; buyers are pharma.'
    evidence = pipeline.evidence.save(sample(pid, text).model_copy(update={
        'source_key': 'same-context-other-source', 'visibility': 'public'}))
    ref = LocatedRef(evidence_version_id=evidence['id'], part_index=0, start=0, end=len(text), quote=text)
    for predicate, value in (('use_case', 'drug screening'), ('buyer_type', 'pharma')):
        pipeline.objects.save_fact(pid, FactInput(subject_id=original['products'][0], predicate=predicate,
            value=value, text=value, visibility='public', basis='actual', subject_attribution='explicit', refs=(ref,)))
    inputs = research_inputs(pipeline, pid)
    assert len(inputs['query_contexts']) > 1
    assert len({c['product_id'] for c in inputs['query_contexts']}) == 1
    assert inputs['queries'] == ['Lens A drug screening pharma 竞品', 'drug screening pharma 竞品', 'Synthetic Optics 竞争对手']
    assert len({tuple(c['fact_ids']) for c in inputs['query_contexts']}) > 1
    from project_watch.studio.competition_research import original_query_context
    ctx = original_query_context(inputs, inputs['company'], target=True)
    assert len(ctx['queries']) == 2 and ctx['queries'][1].endswith('官网')


def test_original_context_keeps_distinct_product_after_duplicate_locations():
    from project_watch.studio.competition_research import original_query_context
    first = {'product_name': 'Product A', 'uses': ['use A']}
    second = {'product_name': 'Product B', 'uses': ['use B']}
    ctx = original_query_context({'query_contexts': [first, first, second]}, 'Synthetic Co', target=True)
    assert ctx['queries'] == ['Synthetic Co Product A use A 产品', 'Synthetic Co Product B use B 产品']


def test_current_public_alias_names_guide_selection_and_source_changes_fingerprint(tmp_path):
    from test_studio_business_identity import MAPPING, SHORT, extract, setup
    from test_studio_evidence_versions import sample

    from project_watch.studio.competition_research import original_query_context, selection_matches

    service, pid, _, profiles = setup(tmp_path)
    _, _, result = extract(service, pid, profiles, MAPPING + '合成光学研发 Lens A 用于 drug screening；购买方为 pharma；交付形式为 software。')
    assert result['accepted'] == 1
    inputs = research_inputs(service, pid)
    ctx = original_query_context(inputs, inputs['company'], target=True)
    assert SHORT in ctx['names']
    assert 'company' in selection_matches({'title': SHORT + ' 发布 Lens A'}, ctx)
    # Same source corrected: the definition must not stay as a discovery alias.
    service.evidence.save(sample(pid, 'Removed source definition').model_copy(update={
        'source_key': 'public-profile:alias-product', 'visibility': 'public'}))
    after = research_inputs(service, pid)
    assert after['input_version'] != inputs['input_version'] and SHORT not in after['company_names']


def test_original_selection_prefers_company_and_use_not_short_name_homonyms(tmp_path):
    pipeline, pid, _, reads = research_pipeline(tmp_path)
    pipeline.schedule_competition(pid)
    execution = JobExecution(pipeline.store, pipeline.store.claim())
    hits = [
        {'url': 'https://beta.test/furniture', 'title': 'Beta furniture', 'snippet': 'home furniture'},
        {'url': 'https://beta.test/registry', 'title': 'Beta directory', 'snippet': 'registered address'},
        {'url': 'https://elsewhere.test/target', 'title': 'Synthetic Optics Lens A', 'snippet': 'drug screening'},
        {'url': 'https://beta.test/biology', 'title': 'Beta Organ B', 'snippet': 'drug screening for pharma'},
        {'url': 'https://beta.test/product', 'title': 'Beta drug screening', 'snippet': 'product specifications'},
    ]
    pipeline.article_reader = SimpleNamespace(read=lambda url: reads.append(url) or {
        'status': 'article_body', 'text': 'Beta publishes its product specifications.'})
    report = []
    evidence = pipeline.competition_research.originals(pid, hits, execution=execution,
        context={'names': ['Beta'], 'products': [], 'uses': ['drug screening']}, report=report)
    assert reads[:2] == ['https://beta.test/biology', 'https://beta.test/product']
    assert len(reads) == 3 and len(hits) == 5 and len(evidence) == 3
    assert report[0]['title'] == 'Beta Organ B'
    assert report[0]['selection_matches'] == ['company', 'use']
    assert report[0]['status'] == 'article_body' and report[0]['evidence_id'] == evidence[0]['id']


def test_reader_receipts_distinguish_empty_whitespace_and_useful_bodies(tmp_path):
    pipeline, pid, _, reads = research_pipeline(tmp_path)
    pipeline.schedule_competition(pid)
    execution = JobExecution(pipeline.store, pipeline.store.claim())
    responses = [
        {'status': 'summary_only', 'text': 'a search snippet'},
        {'status': 'article_body', 'text': '   '},
        {'status': 'partial_body', 'text': 'Beta builds Organ B for drug screening.'},
    ]
    hits = [{'url': f'https://beta.test/{i}', 'title': f'Page {i}', 'snippet': 'lead'} for i in range(3)]
    pipeline.article_reader = SimpleNamespace(read=lambda url: reads.append(url) or responses[int(url[-1])])
    report = []
    evidence = pipeline.competition_research.originals(pid, hits, execution=execution, report=report)
    assert len(evidence) == 1
    assert [r['body_available'] for r in report] == [False, False, True]
    assert [r['status'] for r in report] == ['summary_only', 'article_body', 'partial_body']
    repeated = []
    pipeline.competition_research.originals(pid, hits, execution=execution, report=repeated)
    assert repeated == report and len(reads) == 3


def test_leased_queries_and_read_outcomes_survive_replay(tmp_path):
    from project_watch.studio.http import Http

    pipeline, pid, calls, reads = research_pipeline(tmp_path)
    transport = pipeline.http_factory(None).transport
    queries = []
    def record(method, url, headers, body, timeout):
        if 'api.tavily.com' in url:
            queries.append(json.loads(body)['query'])
        return transport(method, url, headers, body, timeout)
    pipeline.http_factory = lambda _: Http(record)
    pipeline.schedule_competition(pid)
    job = pipeline.store.claim()
    pipeline.run(job)
    assert 'Synthetic Optics Lens A drug screening 产品' in queries
    assert 'Beta drug screening 产品' in queries
    saved = pipeline.competition_research.saved(pid)
    assert saved['reading']['target']['body_count'] == 1
    assert saved['reading']['peers'][0]['body_count'] == 1
    before = len(calls), len(reads), list(queries)
    pipeline.run(job)
    assert (len(calls), len(reads), queries) == before
    assert pipeline.competition_research.saved(pid)['reading'] == saved['reading']


def test_short_name_token_prefix_is_not_a_company_selection_match():
    from project_watch.studio.competition_research import selection_matches

    context = {'names': ['Beta'], 'uses': ['drug screening'], 'products': []}
    assert selection_matches({'title': 'BetaCorp drug screening'}, context) == ['use']
    assert selection_matches({'title': 'Beta drug screening'}, context) == ['company', 'use']


def test_source_change_after_search_stops_before_model_and_page_reads(tmp_path):
    from project_watch.studio.http import Http

    pipeline, pid, calls, reads = research_pipeline(tmp_path)
    transport = pipeline.http_factory(None).transport
    evidence = pipeline.evidence.page(pid)['items'][0]
    def change(method, url, headers, body, timeout):
        response = transport(method, url, headers, body, timeout)
        pipeline.evidence.dependencies.withdraw(pid, evidence['id'], 'synthetic change', actor='test')
        return response
    pipeline.http_factory = lambda _: Http(change)
    pipeline.schedule_competition(pid)
    job = pipeline.store.claim()
    with pytest.raises(ValueError, match='inputs changed'):
        pipeline.run(job)
    assert len(calls) == 1 and 'tavily' in calls[0] and reads == []


@pytest.mark.parametrize('change', ['withdraw', 'pause'])
def test_changed_source_or_permission_before_retrieval_stops_dispatch(tmp_path, change):
    pipeline, pid, calls, reads = research_pipeline(tmp_path)
    pipeline.schedule_competition(pid)
    job = pipeline.store.claim()
    execution = JobExecution(pipeline.store, job)
    inputs = research_inputs(pipeline, pid)
    execution.checkpoint('competition_inputs', inputs)
    if change == 'pause':
        pipeline.store.set_status(pid, 'paused')
    else:
        evidence = pipeline.evidence.page(pid)['items'][0]
        pipeline.evidence.dependencies.withdraw(pid, evidence['id'], 'synthetic correction', actor='test')
    if change == 'withdraw':
        with pytest.raises(ValueError, match='inputs changed'):
            pipeline.run(job)
    else:
        from project_watch.studio.http import HttpError

        with pytest.raises(HttpError, match='project_paused'):
            pipeline.run(job)
        assert execution.read_checkpoint('dispatch_completed') is None
    assert calls == [] and reads == []
