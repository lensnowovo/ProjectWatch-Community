"""A public-site failure is not an ambiguous paid request. No live network."""

from types import SimpleNamespace

import pytest
from test_studio_external_round import first, followup, setup

from project_watch.feeds import FeedError, GuardedHTTPS
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import HttpError
from project_watch.studio.monitor import check_sites
from project_watch.studio.pipeline import Pipeline


@pytest.mark.parametrize('failure', ['http_url', 'dns_failure', 'connection_failed', 'host_guard'])
def test_unreadable_site_does_not_block_followup_and_plan(tmp_path, monkeypatch, failure):
    pipeline, pid, calls = setup(tmp_path, monkeypatch)
    # Restore the real official-site stage and use the actual public-IP guard.
    monkeypatch.setattr(pipeline, '_check_sites', Pipeline._check_sites.__get__(pipeline))
    url = 'http://synthetic.example/' if failure == 'http_url' else 'https://synthetic.example/'
    pipeline.store.upsert_project(pid, {'website': url}, consent=False)
    requests = []

    def resolve(host):
        requests.append(host)
        raise FeedError(failure)

    transport = GuardedHTTPS(resolver=resolve)
    if failure == 'host_guard':
        from project_watch.host_network import HostArticleTransport
        transport = HostArticleTransport('synthetic')
        monkeypatch.setattr(transport, 'get_response', lambda url: pytest.fail('unsafe host dispatch'))
    pipeline.article_reader = SimpleNamespace(transport=transport, read=lambda url: {
        'status': 'article_body', 'text': 'Synthetic evidence.', 'resolved_url': url})
    monkeypatch.setattr(pipeline, '_replan', lambda *a, **k: calls.append(('replan', '')))
    first(pipeline)
    job = followup(pipeline)
    pipeline.run(job)
    pipeline.store.finish(job['id'], lease_token=job['lease_token'])
    for entry in [('bing', 'Synthetic Peer'), ('bing', 'Synthetic industry'),
                  ('bing', 'Synthetic policy'), ('profile', ''), ('replan', '')]:
        assert entry in calls
    assert calls.index(('profile', '')) < calls.index(('replan', ''))
    assert pipeline.store.get_project(pid)['last_external_at']
    assert JobExecution(pipeline.store, job).usage()['site']['unknown'] == 0
    assert pipeline.store.get_plan(pid)['last_report']['site']['errors']
    assert pipeline.monitor.pages(pid)[0]['status'] == 'robots_disallowed'
    assert requests == ([] if failure in ('http_url', 'host_guard') else ['synthetic.example'])


def test_historical_unknown_site_is_not_silently_retried(tmp_path, monkeypatch):
    pipeline, pid, _ = setup(tmp_path, monkeypatch)
    job = pipeline.store.claim()
    execution = JobExecution(pipeline.store, job)

    class Broken:
        def get_response(self, url):
            raise RuntimeError('synthetic unexpected failure')

    with pytest.raises(HttpError, match='request_outcome_unknown'):
        check_sites(pipeline.monitor, Broken(), pid, 'https://synthetic.example/', execution=execution)
    class Forbidden:
        def get_response(self, url):
            pytest.fail('unapproved retry')
    with pytest.raises(HttpError, match='request_outcome_unknown'):
        check_sites(pipeline.monitor, Forbidden(), pid, 'https://synthetic.example/', execution=execution)


def test_first_plan_receives_new_public_products_after_site_failure(tmp_path, monkeypatch):
    from test_studio_plan_replan import BOOT, Boot

    from project_watch.studio import plan_replan

    pipeline, pid, _ = setup(tmp_path, monkeypatch)
    pipeline.store.upsert_project(pid, {'website': 'http://synthetic.example/'}, consent=False)
    monkeypatch.setattr(pipeline, '_check_sites', Pipeline._check_sites.__get__(pipeline))
    pipeline.article_reader = SimpleNamespace(
        transport=GuardedHTTPS(resolver=lambda _: pytest.fail('HTTP rejected before DNS')),
        read=lambda url: {'status': 'unavailable'})
    model = Boot(BOOT)
    pipeline.plan_reviser = plan_replan

    def dossier(_job):
        pipeline.store.put_public_profile(pid, {'facts': [
            {'kind': 'product', 'label': 'Synthetic product', 'text': 'Synthetic public product dossier'}]})

    monkeypatch.setattr(pipeline, 'run_public_profile', dossier)
    monkeypatch.setattr(pipeline, '_replan', lambda project_id, llm, stage, **kwargs:
                        Pipeline._replan(pipeline, project_id, model, stage, **kwargs))
    first(pipeline)
    job = followup(pipeline)
    pipeline.run(job)
    assert 'Synthetic public product dossier' in model.inputs[0]
    assert pipeline.monitoring_plan.has_plan(pid)
    assert len(pipeline.monitoring_plan.items(pid)) == 3


@pytest.mark.parametrize('mode', ['refused', 'budget'])
def test_site_backoff_and_site_budget_only_skip_site_reads(tmp_path, monkeypatch, mode):
    pipeline, pid, _ = setup(tmp_path, monkeypatch)
    job = pipeline.store.claim()
    execution = JobExecution(pipeline.store, job, budgets={'site': 1 if mode == 'budget' else 16})
    pipeline.monitor.add_page(pid, 'https://synthetic.example/news', 'News', 'news', 'user')
    pipeline.monitor.add_page(pid, 'https://synthetic.example/product', 'Products', 'product', 'user')
    requests = []

    def connect(host, ip, path):
        requests.append(path)
        return (200 if path == '/robots.txt' else 429), {}, b''

    report = check_sites(pipeline.monitor, GuardedHTTPS(resolver=lambda _: ['8.8.8.8'], connector=connect),
                         pid, None, execution=execution)
    assert report['errors'] and report['checked'] == 0
    assert len(requests) == (1 if mode == 'budget' else 2)
    assert execution.usage()['site']['unknown'] == 0
