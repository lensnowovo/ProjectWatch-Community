import json
from types import SimpleNamespace

import pytest
from test_studio_agent_instance import make_service
from test_studio_bilateral_competition import setup_pair
from test_studio_evidence_versions import sample

from project_watch.studio.monitoring_contracts import ObjectInput


def test_memory_names_cannot_be_discovered_without_exact_public_source(tmp_path):
    from project_watch.studio.competition_research import discover_candidates
    service, pid = make_service(tmp_path / 'discover.sqlite3')
    target = service.objects.ensure_subject(pid)
    evidence = service.evidence.save(sample(pid, 'Beta sells Organ B').model_copy(update={'visibility': 'public'}))
    candidates = discover_candidates(service, pid, target['id'], [evidence], {'candidates': [
        {'name': 'Beta', 'source': 'D1', 'quote': 'Beta sells Organ B'},
        {'name': 'Model Memory Inc', 'source': 'D1', 'quote': 'Beta sells Organ B'},
        {'name': 'Beta', 'source': 'foreign', 'quote': 'Beta sells Organ B'}]})
    assert [r['name'] for r in candidates] == ['Beta']
    assert service.objects.get(pid, candidates[0]['id'])['identity_basis'] == 'candidate_with_evidence'
    assert discover_candidates(service, pid, target['id'], [evidence], {'candidates': [
        {'name': 'Beta', 'source': 'D1', 'quote': 'Beta sells Organ B'}]})[0]['id'] == candidates[0]['id']


def test_private_sources_never_enter_candidate_discovery(tmp_path):
    from project_watch.studio.competition_research import discover_candidates
    service, pid = make_service(tmp_path / 'private.sqlite3')
    target = service.objects.ensure_subject(pid)
    private = service.evidence.save(sample(pid, 'Secret company Product'))
    with pytest.raises(ValueError):
        discover_candidates(service, pid, target['id'], [private], {'candidates': []})


def test_peer_extraction_does_not_replace_target_or_merge_names(tmp_path):
    from project_watch.studio.business_profile import BusinessProfiles
    service, pid = make_service(tmp_path / 'peers.sqlite3')
    target = service.objects.ensure_subject(pid)
    peer = service.objects.save(pid, ObjectInput(kind='company', name='Beta', visibility='public'), actor='automatic')
    text = 'Beta builds Organ B for drug screening; buyers are pharma; delivery is software.'
    evidence = service.evidence.save(sample(pid, text).model_copy(update={'visibility': 'public'}))
    profiles = BusinessProfiles(service.store, service.objects)
    inputs = profiles.inputs(pid, binding='fixture', subject_id=peer['id'], evidence_ids=[evidence['id']])
    profiles.apply(pid, inputs, {'products': [{'company': 'Beta', 'name': 'Organ B', 'source': 'B1', 'quote': text,
        'use_case': 'drug screening', 'buyer_type': 'pharma', 'delivery': 'software', 'basis': 'actual', 'polarity': 'asserted'}]})
    assert profiles.get(pid) is None and profiles.get(pid, subject_id=peer['id'])['accepted'] == 1
    assert service.objects.get(pid, target['id'])['name'] == 'Synthetic Optics'


def test_target_queries_only_use_public_product_source_assertions(tmp_path):
    from project_watch.studio.competition_research import research_inputs
    service, pid, scope, _ = setup_pair(tmp_path)
    assert research_inputs(service, pid)['queries'] == []  # fixture products are internal
    assert scope['subject_id'] != scope['peer_id']


def test_saved_read_stage_is_replayed_without_opening_page_twice(tmp_path):
    from project_watch.studio.competition_research import read_page
    from project_watch.studio.execution import JobExecution
    service, pid = make_service(tmp_path / 'reads.sqlite3')
    service.store.enqueue(pid, 'internal')
    execution = JobExecution(service.store, service.store.claim(), budgets={'page': 1})
    calls = []
    reader = SimpleNamespace(read=lambda url: calls.append(url) or {'status': 'article_body', 'text': 'public product text'})
    assert read_page(execution, reader, 'https://example.test/a')['text'] == 'public product text'
    assert read_page(execution, reader, 'https://example.test/a')['text'] == 'public product text'
    assert len(calls) == 1 and execution.usage()['page']['cache_hits'] == 1
    from project_watch.studio.http import HttpError
    with pytest.raises(HttpError):
        read_page(execution, reader, 'https://example.test/b')
    assert len(calls) == 1


def test_page_receipt_survives_store_reopen_but_not_a_new_job(tmp_path):
    from project_watch.studio.competition_research import read_page
    from project_watch.studio.execution import JobExecution
    from project_watch.studio.store import StudioStore

    path = tmp_path / 'receipt-boundary.sqlite3'
    service, pid = make_service(path)
    service.store.enqueue(pid, 'internal')
    first = service.store.claim()
    calls = []
    reader = SimpleNamespace(read=lambda url: calls.append(url) or {'status': 'article_body', 'text': 'public body'})
    url = 'https://example.test/same-page'
    read_page(JobExecution(service.store, first), reader, url)
    reopened = StudioStore(path)
    assert read_page(JobExecution(reopened, first), reader, url)['text'] == 'public body'
    assert len(calls) == 1
    reopened.finish(first['id'], lease_token=first['lease_token'])
    reopened.enqueue(pid, 'internal')
    second = reopened.claim()
    assert second['id'] != first['id']
    read_page(JobExecution(reopened, second), reader, url)
    assert len(calls) == 2


def research_pipeline(tmp_path):
    from project_watch.studio.app import create_studio_app
    from project_watch.studio.http import Http, Response

    app = create_studio_app(tmp_path, start_worker=False)
    pipeline, store = app.state.studio_pipeline, app.state.studio_store
    pid = pipeline.repository.create_project('Synthetic Optics').id
    store.upsert_project(pid, {}, consent=True)
    pipeline.settings_store.update({'model': {'protocol': 'openai', 'base_url': 'https://synthetic.example/v1',
        'model': 'fixture', 'api_key': 'synthetic-key'}, 'search': {'provider': 'tavily', 'api_key': 'synthetic-search-key'}})
    company = pipeline.objects.ensure_subject(pid)
    target = 'Synthetic Optics builds Lens A for drug screening; buyers are pharma; delivery is software.'
    peer = 'Beta builds Organ B for drug screening; buyers are pharma; delivery is software.'
    def proposal(name, product, quote):
        return {'products': [{'company': name, 'name': product, 'source': 'B1', 'quote': quote,
            'use_case': 'drug screening', 'buyer_type': 'pharma', 'delivery': 'software', 'basis': 'actual', 'polarity': 'asserted'}]}
    seed = pipeline.evidence.save(sample(pid, target).model_copy(update={'visibility': 'public'}))
    inputs = pipeline.business_profiles.inputs(pid, binding=pipeline.model_binding(), subject_id=company['id'], evidence_ids=[seed['id']])
    pipeline.business_profiles.apply(pid, inputs, proposal('Synthetic Optics', 'Lens A', target))
    calls, reads = [], []
    def transport(method, url, headers, body, timeout):
        data = json.loads(body)
        calls.append(url)
        if 'api.tavily.com' in url:
            is_target = data['query'].startswith('Synthetic Optics')
            output = {'results': [{'title': 'Public product', 'url': 'https://alpha.test/product' if is_target else 'https://beta.test/product',
                                    'content': target if is_target else peer}]}
        else:
            context = json.loads(data['messages'][1]['content'])
            if 'facts' in context:
                answer = {'verdict': 'supported', 'reason': 'same documented use and buyer type', 'fact_ids': [f['id'] for f in context['facts']]}
            elif context['sources'][0]['source'].startswith('D'):
                answer = {'candidates': [{'name': 'Beta', 'source': 'D1', 'quote': peer}]}
            else:
                answer = proposal('Synthetic Optics', 'Lens A', target) if context['company'] == 'Synthetic Optics' else proposal('Beta', 'Organ B', peer)
            output = {'choices': [{'message': {'content': json.dumps(answer)}}]}
        return Response(200, {}, json.dumps(output).encode())
    pipeline.http_factory = lambda _: Http(transport)
    pipeline.article_reader = SimpleNamespace(read=lambda url: reads.append(url) or {'status': 'article_body', 'text': target if 'alpha.test' in url else peer})
    return pipeline, pid, calls, reads


def test_bounded_leased_pipeline_researches_both_originals_and_does_not_repeat(tmp_path):
    pipeline, pid, calls, reads = research_pipeline(tmp_path)
    queued = pipeline.schedule_competition(pid)
    assert queued['kind'] == 'competition'
    claimed = pipeline.store.claim()
    pipeline.run(claimed)
    baseline = len(calls), len(reads)
    pipeline.run(claimed)
    assert (len(calls), len(reads)) == baseline
    pipeline.store.finish(claimed['id'], lease_token=claimed['lease_token'])
    saved = pipeline.competition_research.saved(pid)
    assert saved['status'] == 'completed_bounded_batch' and saved['supported_count'] == 1
    assert not saved['coverage_complete'] and saved['usage']['page']['requests'] == 2
    assert pipeline.schedule_competition(pid) is None
    current = pipeline.relationships.view(pid, include_internal=False)['items']
    assert len(current) == 1 and current[0]['support'] == 'supported' and current[0]['status'] == 'current'
    assert len(reads) == 2 and all('test' in url for url in reads)


def test_discovery_replay_does_not_downgrade_original_head(tmp_path):
    pipeline, pid, _, _ = research_pipeline(tmp_path)
    hit = {'url': 'https://beta.test/product', 'title': 'Beta', 'snippet': 'short summary'}
    body = pipeline.competition_research._snapshot(pid, hit, 'Beta original body', 'partial')
    pipeline.competition_research._snapshot(pid, hit)
    with pipeline.store._connect() as db:
        head = db.execute('SELECT evidence_version_id FROM studio_evidence_heads WHERE project_id=? AND source_key=?', (pid, body['source_key'])).fetchone()
    assert head[0] == body['id']


def test_research_scheduler_requires_search_and_respects_pause(tmp_path):
    pipeline, pid, _, _ = research_pipeline(tmp_path)
    pipeline.store.set_status(pid, 'paused')
    assert pipeline.schedule_competition(pid) is None
    pipeline.store.set_status(pid, 'active')
    pipeline.settings_store.update({'search': {'provider': 'none', 'api_key': ''}})
    assert pipeline.schedule_competition(pid) is None


def test_lease_replaced_during_model_call_cannot_write_profile(tmp_path):
    from project_watch.studio.execution import JobExecution
    pipeline, pid, _, _ = research_pipeline(tmp_path)
    queued = pipeline.schedule_competition(pid)
    claimed = pipeline.store.claim()
    execution = JobExecution(pipeline.store, claimed, budgets={'page': 12})
    pipeline.store.finish(queued['id'], lease_token=claimed['lease_token'])
    before = pipeline.evidence.page(pid)['items']
    hit = {'url': 'https://beta.test/product', 'title': 'Beta', 'snippet': 'public'}
    with pytest.raises(RuntimeError, match='lease'):
        pipeline.competition_research._snapshot(pid, hit, execution=execution)
    assert pipeline.evidence.page(pid)['items'] == before


def test_saved_discovery_models_are_not_reused_after_model_change(tmp_path):
    from project_watch.studio.execution import JobExecution
    pipeline, pid, calls, _ = research_pipeline(tmp_path)
    pipeline.schedule_competition(pid)
    claimed = pipeline.store.claim()
    execution = JobExecution(pipeline.store, claimed)
    execution.checkpoint('competition_model_binding', 'another-model')
    with pytest.raises(ValueError, match='another model'):
        pipeline.run(claimed)
    assert calls == []


def test_research_uses_three_distinct_original_pages_and_replays_all(tmp_path):
    pipeline, pid, calls, reads = research_pipeline(tmp_path)
    transport = pipeline.http_factory(None).transport

    def multi_page_transport(method, url, headers, body, timeout):
        reply = transport(method, url, headers, body, timeout)
        if 'api.tavily.com' not in url:
            return reply
        from project_watch.studio.http import Response
        data = json.loads(reply.body)
        hit = data['results'][0]
        data['results'] = [hit, dict(hit), *[
            {**hit, 'url': hit['url'] + f'/{i}'} for i in range(2, 5)]]
        return Response(200, {}, json.dumps(data).encode())

    from project_watch.studio.http import Http
    pipeline.http_factory = lambda _: Http(multi_page_transport)
    pipeline.schedule_competition(pid)
    claimed = pipeline.store.claim()
    pipeline.run(claimed)
    assert len(reads) == 6
    assert len(set(reads)) == 6
    assert not any(url.endswith('/4') for url in reads)
    baseline = len(calls), len(reads)
    pipeline.run(claimed)
    assert (len(calls), len(reads)) == baseline
    saved = pipeline.competition_research.saved(pid)
    assert saved['usage']['page']['requests'] == 6


def test_original_pages_continue_known_failure_but_hold_unknown(tmp_path):
    from project_watch.studio.execution import JobExecution
    from project_watch.studio.http import HttpError
    pipeline, pid, _, reads = research_pipeline(tmp_path)
    pipeline.schedule_competition(pid)
    claimed = pipeline.store.claim()
    execution = JobExecution(pipeline.store, claimed)
    hits = [{'title': 'Public product', 'url': f'https://alpha.test/{i}', 'snippet': 'search snippet'}
            for i in range(4)]

    def read(url):
        reads.append(url)
        if url.endswith('/0'):
            raise HttpError('http_failure', status=404)
        return {'status': 'article_body', 'text': 'Synthetic Optics publishes product specifications.'}

    pipeline.article_reader = SimpleNamespace(read=read)
    evidence = pipeline.competition_research.originals(pid, hits, execution=execution)
    assert len(reads) == 3
    assert len(evidence) == 2 and all(e['read_scope'] == 'partial' for e in evidence)
    assert all('search snippet' not in p['text'] for e in evidence for p in e['parts'])
    pipeline.article_reader = SimpleNamespace(read=lambda url: (_ for _ in ()).throw(HttpError('request_outcome_unknown')))
    with pytest.raises(HttpError, match='request_outcome_unknown'):
        pipeline.competition_research.originals(pid, [{**hits[0], 'url': 'https://alpha.test/new'}], execution=execution)
