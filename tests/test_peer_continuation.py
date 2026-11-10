import json
from types import SimpleNamespace

import pytest
from test_studio_competition_research import research_pipeline

from project_watch.studio.http import Http, HttpError, Response


@pytest.mark.parametrize('crash', [False, True])
def test_unknown_peer_is_not_resent_and_other_peer_finishes(tmp_path, crash):
    pipeline, pid, _calls, _reads = research_pipeline(tmp_path)
    base = pipeline.http_factory(None).transport
    failed = []
    gamma = 'Gamma builds Organ B for drug screening; buyers are pharma; delivery is software.'
    beta = gamma.replace('Gamma', 'Beta')
    def transport(method, url, headers, body, timeout):
        data = json.loads(body)
        if 'api.tavily.com' in url:
            if data['query'].startswith('Gamma'):
                return Response(200, {}, json.dumps({'results': [{'title': 'Public Gamma product',
                    'url': 'https://gamma.test/product', 'content': gamma}]}).encode())
            response = base(method, url, headers, body, timeout)
            if '\u7ade\u54c1' in data['query']:
                response = Response(200, {}, json.dumps({'results': [
                    {'title': 'Public Beta product', 'url': 'https://beta.test/product', 'content': beta},
                    {'title': 'Public Gamma product', 'url': 'https://gamma.test/product', 'content': gamma}]}).encode())
            return response
        context = json.loads(data['messages'][1]['content'])
        if context.get('company') == 'Beta' and 'facts' not in context and not context['sources'][0]['source'].startswith('D'):
            failed.append(1)
            if crash:
                raise KeyboardInterrupt('synthetic worker crash')
            raise HttpError('connection_failed', detail='synthetic timed out', failure_kind='timeout')
        if context.get('sources', [{}])[0].get('source', '').startswith('D'):
            return Response(200, {}, json.dumps({'choices': [{'message': {'content': json.dumps({'candidates': [
                {'name': 'Beta', 'source': 'D1', 'quote': beta},
                {'name': 'Gamma', 'source': next(s['source'] for s in context['sources'] if gamma in s['text']), 'quote': gamma}]})}}]}).encode())
        response = base(method, url, headers, body, timeout)
        if context.get('company') == 'Gamma':
            return Response(200, {}, response.body.replace(b'Beta', b'Gamma'))
        return response
    pipeline.http_factory = lambda _: Http(transport)
    original = pipeline.article_reader.read
    pipeline.article_reader = SimpleNamespace(read=lambda url: {'status': 'article_body', 'text': gamma} if 'gamma.test' in url else original(url))
    pipeline.schedule_competition(pid)
    job = pipeline.store.claim()
    if crash:
        with pytest.raises(KeyboardInterrupt):
            pipeline.run(job)
    pipeline.run(job)
    saved = pipeline.competition_research.saved(pid)
    assert failed == [1]
    assert saved['status'] == 'partial'
    assert saved['researched_count'] == 1, {k:saved.get(k) for k in ('candidate_count','candidates','unresolved_peers','failures','candidate_rejections')}
    assert len(saved['unresolved_peers']) == 1
    assert saved['supported_count'] == 1
    with pipeline.store._connect() as db:
        unknown = [tuple(r) for r in db.execute("select * from studio_job_calls where status in ('unknown','started')")]
        summaries = db.execute("select input_version from studio_jobs where kind='page_summary'").fetchall()
        # Simulate restart after run's final checkpoint was lost; per-peer holds survive.
        db.execute("delete from studio_job_checkpoints where job_id=? and stage='dispatch_completed'", (job['id'],))
        db.commit()
    pipeline.run(job)
    assert failed == [1]
    assert len(summaries) == 1
    with pipeline.store._connect() as db:
        assert unknown == [tuple(r) for r in db.execute("select * from studio_job_calls where status in ('unknown','started')")]
        assert db.execute("select count(*) from studio_jobs where kind='page_summary'").fetchone()[0] == 1
    pipeline.store.finish(job['id'], lease_token=job['lease_token'])
    assert pipeline.store.jobs(pid)[-1]['status'] == 'failed'


def test_competition_summary_dispatch_targets_one_page(tmp_path, monkeypatch):
    from project_watch.studio import page_summaries
    pipeline, pid, _, _ = research_pipeline(tmp_path)
    pipeline.store.enqueue(pid, 'page_summary', input_version='competition-summary:synthetic')
    job = pipeline.store.claim()
    targets = []
    monkeypatch.setattr(page_summaries, 'run', lambda *args, **kw: targets.append(kw['targets']))
    pipeline.run(job)
    assert targets == [('competition',)]


def test_competition_summary_does_not_absorb_other_pages(tmp_path):
    pipeline, pid, _, _ = research_pipeline(tmp_path)
    s = pipeline.store
    competitor = s.enqueue(pid, 'page_summary', input_version='competition-summary:a', once_per_input=True)
    page = s.enqueue(pid, 'page_summary', input_version='page:papers')
    full = s.enqueue(pid, 'page_summary', input_version='analyze:a')
    fresh = s.enqueue(pid, 'page_summary', input_version='competition-summary:b', once_per_input=True)
    assert len({r['id'] for r in (competitor, page, full, fresh)}) == 4
    assert s.enqueue(pid, 'page_summary', input_version='competition-summary:b', once_per_input=True)['id'] == fresh['id']
