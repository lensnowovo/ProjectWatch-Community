"""Declared query provenance is a bounded retrieval hint, not source truth."""
import hashlib
from copy import deepcopy

import pytest
from test_studio_readable_original_leads import prepare


def key(query):
    return hashlib.sha256(query.strip().encode()).hexdigest()


def lead(index, query=None, *, snippet='Synthetic Optics Organ A product'):
    result = {'url': f'https://publisher.test/{index}', 'title': 'Synthetic Optics Organ A', 'snippet': snippet}
    if query:
        result['_retrieval_query_keys'] = [key(query)]
    return result


def context():
    return {'names': ['Synthetic Optics'], 'products': ['Organ A'], 'uses': [],
            'queries': ['product demand', 'official manufacturing']}


def test_merge_interleaves_before_bound_and_retains_membership():
    from project_watch.studio.competition_research import merge_query_leads
    batches = [('product demand', [lead(i) for i in range(10)]),
               ('official manufacturing', [lead(i) for i in range(20, 30)])]
    before = deepcopy(batches)
    hits = merge_query_leads(batches)
    assert len(hits) == 8
    assert [h['url'] for h in hits[:4]] == [lead(i)['url'] for i in (0, 20, 1, 21)]
    assert hits[1]['_retrieval_query_keys'] == [key('official manufacturing')]
    assert batches == before


def test_shared_url_unions_only_local_query_memberships():
    from project_watch.studio.competition_research import merge_query_leads
    first = lead(0, 'untrusted collector claim')
    second = {**lead(0), 'title': 'Latest returned title'}
    hits = merge_query_leads([('product demand', [first]), ('official manufacturing', [second])])
    assert len(hits) == 1 and hits[0]['title'] == second['title']
    assert hits[0]['_retrieval_query_keys'] == [key('product demand'), key('official manufacturing')]
    assert first['_retrieval_query_keys'] == [key('untrusted collector claim')]


def test_equal_trimmed_queries_have_one_membership():
    from project_watch.studio.competition_research import merge_query_leads
    hits = merge_query_leads([('same', [lead(0)]), (' same ', [lead(0), lead(1)])])
    assert len(hits) == 2
    assert all(h['_retrieval_query_keys'] == [key('same')] for h in hits)


def test_shared_fragment_is_one_lead_with_both_query_memberships():
    from project_watch.studio.competition_research import merge_query_leads
    first = {**lead(0), 'url': lead(0)['url'] + '#section'}
    hits = merge_query_leads([('first', [first]), ('second', [lead(0)])])
    assert len(hits) == 1 and hits[0]['url'] == lead(0)['url']
    assert hits[0]['_retrieval_query_keys'] == [key('first'), key('second')]


def test_leased_pipeline_records_memberships_without_model_metadata(tmp_path):
    import json

    from test_studio_competition_research import research_pipeline

    from project_watch.studio.execution import JobExecution
    from project_watch.studio.http import Http

    pipeline, pid, calls, reads = research_pipeline(tmp_path)
    transport = pipeline.http_factory(None).transport
    model_payloads = []
    def record(method, url, headers, body, timeout):
        if 'api.tavily.com' not in url:
            model_payloads.append(json.loads(body)['messages'][1]['content'])
        return transport(method, url, headers, body, timeout)
    pipeline.http_factory = lambda _: Http(record)
    pipeline.schedule_competition(pid)
    job = pipeline.store.claim()
    pipeline.run(job)
    execution = JobExecution(pipeline.store, job)
    saved = execution.read_checkpoint('competition_target_search')
    assert len(saved) == 1 and len(saved[0]['_retrieval_query_keys']) == 2
    reading = pipeline.competition_research.saved(pid)['reading']['target']['pages'][0]
    assert reading['retrieval_query_keys'] == saved[0]['_retrieval_query_keys']
    assert model_payloads and all('retrieval_query_keys' not in payload for payload in model_payloads)
    before = len(calls), len(reads), len(model_payloads)
    pipeline.run(job)
    assert (len(calls), len(reads), len(model_payloads)) == before


def test_second_declared_context_gets_a_distinct_relevant_read(tmp_path):
    pipeline, pid, reads, execution = prepare(tmp_path)
    ctx = context()
    hits = [lead(i, ctx['queries'][0]) for i in range(4)] + [lead(9, ctx['queries'][1])]
    before = deepcopy(hits)
    report = []
    result = pipeline.competition_research.originals(pid, hits, execution=execution, context=ctx, report=report)
    assert reads == [lead(i)['url'] for i in (0, 9, 1)]
    assert len(result) == 3 and execution.usage()['page']['requests'] == 3
    assert report[1]['retrieval_query_keys'] == [key(ctx['queries'][1])]
    assert hits == before
    assert pipeline.competition_research.originals(pid, hits, execution=execution, context=ctx) == result
    assert len(reads) == 3


def test_shared_first_url_cannot_replace_a_distinct_second_context(tmp_path):
    pipeline, pid, reads, execution = prepare(tmp_path)
    ctx = context()
    shared = lead(0)
    shared['_retrieval_query_keys'] = [key(q) for q in ctx['queries']]
    hits = [shared, lead(1, ctx['queries'][0]), lead(2, ctx['queries'][0]), lead(9, ctx['queries'][1])]
    pipeline.competition_research.originals(pid, hits, execution=execution, context=ctx)
    assert reads == [lead(i)['url'] for i in (0, 9, 1)]


@pytest.mark.parametrize('mode', ['unknown', 'irrelevant', 'repeated'])
def test_bad_context_cannot_force_a_read_slot(tmp_path, mode):
    pipeline, pid, reads, execution = prepare(tmp_path)
    ctx = context()
    text = 'Synthetic Optics develops Organ A for measurement. ' * 8
    hits = [lead(0, ctx['queries'][0], snippet=text), lead(1, ctx['queries'][0]), lead(2, ctx['queries'][0])]
    extra = lead(9, 'unrecognized query' if mode == 'unknown' else ctx['queries'][1])
    if mode == 'irrelevant':
        extra.update(title='Furniture registry', snippet='Unrelated material')
    elif mode == 'repeated':
        extra['snippet'] = text
    hits.append(extra)
    pipeline.competition_research.originals(pid, hits, execution=execution, context=ctx)
    assert reads == [lead(i)['url'] for i in (0, 1, 2)]


def test_unlabelled_originals_keep_stable_lexical_order(tmp_path):
    pipeline, pid, reads, execution = prepare(tmp_path)
    hits = [lead(i) for i in range(5)]
    pipeline.competition_research.originals(pid, hits, execution=execution, context=context())
    assert reads == [h['url'] for h in hits[:3]]
