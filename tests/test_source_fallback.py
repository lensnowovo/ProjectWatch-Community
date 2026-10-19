from datetime import UTC, datetime, timedelta

import pytest

from project_watch.external_focus import ExternalFocusStore
from project_watch.monitoring import AdapterResult, FeedItem
from project_watch.repository import ProjectRepository
from project_watch.source_fallback import SourceFallbackStore


class Adapter:
    def __init__(self, result):
        self.result, self.calls = result, []

    def fetch(self, target):
        self.calls.append(target)
        return self.result


def setup(tmp_path):
    repo = ProjectRepository(tmp_path / 'watch_test.sqlite3')
    project = repo.create_project('Synthetic Project')
    plan = ExternalFocusStore(repo).create(project.id, category='sector', term='Synthetic product', provider='gdelt')
    plan = repo.set_monitoring_plan_enabled(plan.id, True)
    clock = [1000.0]
    store = SourceFallbackStore(repo, clock=lambda:clock[0])
    return repo, plan, store, clock


def test_explicit_fallback_and_durable_attempts(tmp_path):
    repo, plan, store, clock = setup(tmp_path)
    primary = Adapter(AdapterResult(unavailable_error='rate_limited', retry_after_seconds=120))
    alternate = Adapter(AdapterResult(items=(FeedItem('x','Synthetic product update','https://example.org/1','',datetime.now(UTC)),)))
    result = store.collect(plan, 'first', primary, alternate)
    assert result.unavailable_error == 'rate_limited'
    assert not alternate.calls
    store.approve(plan.id, consent=True, actor='local-test')
    result = store.collect(plan, 'second', primary, alternate)
    assert len(result.items) == 1 and len(primary.calls) == 1 and len(alternate.calls) == 1
    rows = store.attempts(plan.id, 'second')
    assert rows[0]['state'] == 'cooldown' and rows[1]['provider'] == 'bing'
    reopened = SourceFallbackStore(repo, clock=lambda:clock[0])
    assert reopened.attempts(plan.id, 'second') == rows
    clock[0] += 121
    reopened.collect(plan,'third',primary,alternate)
    assert len(primary.calls) == 2


def test_successful_empty_is_not_source_failure(tmp_path):
    _repo, plan, store, _clock = setup(tmp_path)
    store.approve(plan.id, consent=True, actor='local-test')
    primary, alternate = Adapter(AdapterResult()), Adapter(AdapterResult())
    assert store.collect(plan,'empty',primary,alternate).unavailable_error is None
    assert not alternate.calls


def test_both_fail_no_loop_and_paused_no_egress(tmp_path):
    repo, plan, store, _clock = setup(tmp_path)
    store.approve(plan.id, consent=True, actor='local-test')
    primary = Adapter(AdapterResult(unavailable_error='connection_failed'))
    alternate = Adapter(AdapterResult(unavailable_error='dns_failure'))
    assert store.collect(plan,'failed',primary,alternate).unavailable_error == 'dns_failure'
    assert len(primary.calls) == len(alternate.calls) == 1
    repo.set_monitoring_plan_enabled(plan.id, False)
    assert store.collect(plan,'paused',primary,alternate).unavailable_error == 'monitor_paused'
    assert len(primary.calls) == len(alternate.calls) == 1


def test_global_provider_cooldown_across_scopes(tmp_path):
    repo, plan, store, _clock = setup(tmp_path)
    other = ExternalFocusStore(repo).create(plan.project_id,category='industry',term='Another product',provider='gdelt')
    other = repo.set_monitoring_plan_enabled(other.id,True)
    primary = Adapter(AdapterResult(unavailable_error='rate_limited',retry_after_seconds=90))
    store.collect(plan,'a',primary)
    assert store.collect(other,'b',primary).unavailable_error == 'rate_limited'
    assert len(primary.calls) == 1


def test_retry_after_parsing_and_adapter_retention():
    from project_watch.feeds import (
        FeedError,
        GdeltProxyTransport,
        PublicFeedAdapter,
        retry_after_seconds,
    )
    from project_watch.public_search import gdelt_rss_target
    assert retry_after_seconds({'retry-after':'120'}) == 120
    assert retry_after_seconds({'Retry-After':'bad'}) == 60
    assert retry_after_seconds({'retry-after':'9999999'}) == 86400
    adapter = PublicFeedAdapter(GdeltProxyTransport('http://127.0.0.1:17891',
        connector=lambda *args:(429,{'retry-after':'90'},b'')))
    result = adapter.fetch(gdelt_rss_target('Synthetic product'))
    assert result.unavailable_error == 'rate_limited' and result.retry_after_seconds == 90
    assert str(FeedError('rate_limited',retry_after=90)) == 'rate_limited'


def test_api_requires_explicit_same_term_authorization(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient

    from project_watch.api import create_app
    repo, plan, _store, _clock = setup(tmp_path)
    monkeypatch.setenv('PROJECT_WATCH_SETTINGS_TOKEN','x'*40)
    client = TestClient(create_app(repo.db_path))
    url=f'/api/workspace/projects/{plan.project_id}/monitoring/plans/{plan.id}/fallback'
    body={'consent':True,'public_term':'Synthetic product'}
    assert client.post(url,json=body).status_code == 403
    headers={'origin':'http://testserver','x-settings-token':'x'*40}
    # Host must be local, even when token is valid.
    assert client.post(url,json=body,headers=headers).status_code == 403
    client = TestClient(create_app(repo.db_path),base_url='http://127.0.0.1')
    headers['origin']='http://127.0.0.1'
    assert client.post(url,json={**body,'consent':False},headers=headers).status_code == 409
    other=repo.create_project('Other')
    assert client.post(url.replace(plan.project_id,other.id),json=body,headers=headers).status_code == 404
    assert client.post(url,json={**body,'public_term':'Private notes'},headers=headers).status_code == 409
    assert client.post(url,json=body,headers=headers).status_code == 200
    assert client.post(url,json=body,headers=headers).status_code == 200


def test_runner_restart_duplicate_and_source_attribution(tmp_path):
    from project_watch.external_focus import external_reading
    from project_watch.monitoring import MonitoringRunner
    repo, plan, store, clock = setup(tmp_path)
    store.approve(plan.id,consent=True,actor='test')
    primary=Adapter(AdapterResult(unavailable_error='connection_failed'))
    alternate=Adapter(AdapterResult(items=(FeedItem('1','Synthetic product launch','https://example.org/1','',datetime.now(UTC)),)))
    class Router:
        def collect(self,plan,key,adapter):
            return store.collect(plan,key,adapter,alternate)
    runner=MonitoringRunner(repo,{'rss':primary},source_router=Router())
    run=runner.run(plan.id,idempotency_key='run')
    assert run.new_count == 1
    assert runner.run(plan.id,idempotency_key='run').id == run.id
    assert len(primary.calls) == len(alternate.calls) == 1
    items=external_reading(ProjectRepository(repo.db_path),plan.project_id)['categories'][1]['items']
    assert items[0]['collection_sources'] == ['bing']
    clock[0] += 70
    assert runner.run(plan.id,idempotency_key='another').new_count == 0
    reading=external_reading(repo,plan.project_id)['categories'][1]
    assert reading['plans'][0]['source_checks'][-1]['provider'] == 'bing'
    assert reading['plans'][0]['source_checks'][-1]['state'] == 'succeeded'


def test_pause_after_primary_failure_stops_alternate(tmp_path):
    repo,plan,store,_clock=setup(tmp_path)
    store.approve(plan.id,consent=True,actor='test')
    class PausingAdapter:
        def fetch(self,target):
            repo.set_monitoring_plan_enabled(plan.id,False)
            return AdapterResult(unavailable_error='connection_failed')
    alternate=Adapter(AdapterResult())
    assert store.collect(plan,'pause-race',PausingAdapter(),alternate).unavailable_error == 'monitor_paused'
    assert not alternate.calls


def bing_setup(tmp_path):
    repo = ProjectRepository(tmp_path / 'bing_test.sqlite3')
    project = repo.create_project('Synthetic Project')
    plan = ExternalFocusStore(repo).create(project.id, category='sector', term='Synthetic product', provider='bing')
    plan = repo.set_monitoring_plan_enabled(plan.id, True)
    return repo, plan, SourceFallbackStore(repo)


@pytest.mark.parametrize('initial', ['empty','old','unknown','unrelated','future'])
def test_bing_no_current_match_gets_one_same_term_followup(tmp_path, initial):
    from project_watch.bing_news import bing_news_target
    repo, plan, store = bing_setup(tmp_path)
    now = datetime.now(UTC)
    dates = {'old':now-timedelta(days=40),'unknown':None,'unrelated':now,'future':now+timedelta(days=4)}
    items = () if initial == 'empty' else (FeedItem('old','Unrelated' if initial == 'unrelated' else 'Synthetic product','https://example.org/old','',dates[initial]),)
    class Sequence:
        def __init__(self): self.calls=[]
        def fetch(self,target):
            self.calls.append(target)
            return AdapterResult(items=items if len(self.calls)==1 else (FeedItem('new','Synthetic product update','https://example.org/new','',now),))
    adapter=Sequence()
    result=store.collect(plan,'one',adapter)
    assert adapter.calls == [plan.target,bing_news_target('Synthetic product',recent=True)]
    assert any(i.external_id=='new' for i in result.items)
    assert [a['request_variant'] for a in store.attempts(plan.id,'one')] == ['primary','recent']
    assert SourceFallbackStore(repo).attempts(plan.id,'one') == store.attempts(plan.id,'one')


def test_bing_current_result_or_primary_failure_does_not_supplement(tmp_path):
    _repo,plan,store=bing_setup(tmp_path)
    for key,result in [('current',AdapterResult(items=(FeedItem('1','Synthetic product launch','https://example.org/1','',datetime.now(UTC)),))),('failed',AdapterResult(unavailable_error='connection_failed'))]:
        adapter=Adapter(result)
        assert store.collect(plan,key,adapter)==result
        assert len(adapter.calls)==1


def test_failed_supplement_retains_primary_and_provider_backoff(tmp_path):
    from project_watch.external_focus import external_reading
    from project_watch.monitoring import MonitoringRunner
    repo,plan,store=bing_setup(tmp_path)
    old=FeedItem('old','Synthetic product old','https://example.org/old','',datetime.now(UTC)-timedelta(days=60))
    class Sequence:
        def __init__(self): self.calls=[]
        def fetch(self,target):
            self.calls.append(target)
            return AdapterResult(items=(old,)) if len(self.calls)==1 else AdapterResult(unavailable_error='rate_limited',retry_after_seconds=120)
    adapter=Sequence()
    runner=MonitoringRunner(repo,{'rss':adapter},source_router=store)
    run=runner.run(plan.id,idempotency_key='one')
    assert run.status=='completed' and repo.list_signals(plan.project_id)[0].freshness=='historical'
    assert store.attempts(plan.id,'one')[-1]['state']=='failed'
    category=external_reading(repo,plan.project_id)['categories'][1]
    assert category['coverage']=='failed'
    assert category['plans'][0]['source_checks'][-1]['request_variant']=='recent'
    assert store.collect(plan,'two',adapter).unavailable_error=='rate_limited'
    assert len(adapter.calls)==2


def test_bing_pause_between_requests_and_duplicate_runner(tmp_path):
    from project_watch.monitoring import MonitoringRunner
    repo,plan,store=bing_setup(tmp_path)
    class Pausing:
        calls=0
        def fetch(self,target):
            self.calls+=1
            repo.set_monitoring_plan_enabled(plan.id,False)
            return AdapterResult()
    adapter=Pausing()
    assert store.collect(plan,'pause',adapter).unavailable_error=='monitor_paused'
    assert adapter.calls==1
    plan=repo.set_monitoring_plan_enabled(plan.id,True)
    empty=Adapter(AdapterResult())
    runner=MonitoringRunner(repo,{'rss':empty},source_router=store)
    run=runner.run(plan.id,idempotency_key='empty')
    assert run.status=='no_results' and len(empty.calls)==2
    assert runner.run(plan.id,idempotency_key='empty').id==run.id and len(empty.calls)==2


def test_existing_attempt_schema_upgrade_preserves_history_and_guards(tmp_path):
    from contextlib import closing
    repo,plan,store,_clock=setup(tmp_path)
    # Recreate exactly the pre-upgrade empty table, then insert a historical receipt.
    with closing(repo._connect()) as db,db:
        db.execute('DROP TABLE monitor_source_attempts')
        db.execute('CREATE TABLE monitor_source_attempts(id INTEGER PRIMARY KEY,plan_id TEXT NOT NULL,run_key TEXT NOT NULL,provider TEXT NOT NULL,state TEXT NOT NULL,error_code TEXT,item_count INTEGER NOT NULL,observed_at REAL NOT NULL)')
        db.execute("INSERT INTO monitor_source_attempts VALUES(1,?,'old','bing','succeeded',NULL,0,1000)",(plan.id,))
    store=SourceFallbackStore(repo)
    row=store.attempts(plan.id,'old')[0]
    assert row['id']==1 and row['request_variant']=='primary'
    SourceFallbackStore(repo)  # Idempotent migration.
    with closing(repo._connect()) as db:
        with pytest.raises(Exception,match='immutable'):
            db.execute("UPDATE monitor_source_attempts SET item_count=3")
        with pytest.raises(Exception,match='immutable'):
            db.execute('DELETE FROM monitor_source_attempts')
