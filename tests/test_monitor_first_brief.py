"""A first brief is a source-bound reading aid, never a confirmed impact."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from test_monitor_activation import activate, fixture

from project_watch.external_focus import external_reading
from project_watch.monitor_activation import MonitorActivationStore
from project_watch.monitoring import FeedItem, ManualSnapshotAdapter, MonitoringRunner


def collected(tmp_path):
    repo, packet, strategy = fixture(tmp_path)
    activate(MonitorActivationStore(repo), packet, strategy)
    project_id = packet['project_id']
    plan = next(p for p in repo.list_monitoring_plans(project_id) if 'Industrial' in p.target)
    now = datetime.now(UTC)
    news = FeedItem('one', 'Industrial vision systems new product launch',
                   'https://example.org/product', 'The launch is planned, not completed.', now)
    runner = MonitoringRunner(repo, {'rss': ManualSnapshotAdapter({plan.target: (news,)})})
    runner.run(plan.id, idempotency_key='first')
    return repo, packet, plan, runner


def test_brief_has_news_bp_link_question_and_no_invented_impact(tmp_path):
    repo, packet, _, _ = collected(tmp_path)
    brief = external_reading(repo, packet['project_id'])['briefing']
    item = brief['items'][0]
    assert item['category'] == 'sector'  # Industry news must never become a company fact.
    assert item['statement'] == 'Industrial vision systems new product launch'
    assert 'planned, not completed' in item['summary']
    assert item['relationship']['kind'] == 'monitoring_scope'
    assert item['relationship']['verified'] is False
    assert item['question'] == 'Which products are changing?'
    assert item['impact'] is None and item['occurred_at'] is None
    assert item['content_scope'] == 'headline_and_feed_summary_only'
    assert item['evidence_refs'][0]['quote'] in repo.list_fragments(packet['source_id'])[0].text
    assert item['url'] == 'https://example.org/product'
    assert item['unknowns'] and brief['window_days'] == 30
    assert repo.list_observations(packet['project_id']) == []


def test_same_name_stays_unverified_and_failure_not_no_change(tmp_path):
    repo, packet, plan, runner = collected(tmp_path)
    company = next(p for p in repo.list_monitoring_plans(packet['project_id']) if p.id != plan.id)
    runner.adapters = {'rss': ManualSnapshotAdapter({company.target: (
        FeedItem('two', 'Synthetic public company plans acquisition', 'https://example.org/company', '', datetime.now(UTC)),
    )})}
    runner.run(company.id, idempotency_key='company')
    runner.run(plan.id, idempotency_key='failure')
    brief = external_reading(repo, packet['project_id'])['briefing']
    item = next(i for i in brief['items'] if i['category'] == 'company')
    assert item['relationship']['verified'] is False
    assert any('同名' in s for s in item['unknowns'])
    assert next(c for c in brief['coverage'] if c['key'] == 'sector')['state'] == 'failed'
    assert len(brief['items']) == 2  # Failure preserves old readable news.


def test_stale_or_changed_bp_never_supports_current_relationship(tmp_path):
    repo, packet, _, _ = collected(tmp_path)
    source = repo.get_source(packet['source_id'])
    repo.add_source(packet['project_id'], kind='document', reference=source.reference, content_sha256='b'*64)
    item = external_reading(repo, packet['project_id'])['briefing']['items'][0]
    assert item['relationship']['kind'] == 'unresolved'
    assert not item['evidence_refs'] and not item['question']
    assert item['impact'] is None


def test_changed_fragment_and_foreign_signal_are_excluded(tmp_path):
    repo, packet, _, _ = collected(tmp_path)
    fragments = repo.list_fragments(packet['source_id'])
    repo.list_fragments = lambda _: [replace(f, text='changed') for f in fragments]
    item = external_reading(repo, packet['project_id'])['briefing']['items'][0]
    assert not item['evidence_refs']
    signals = repo.list_signals(packet['project_id'])
    repo.list_signals = lambda _: [replace(s, project_id='other') for s in signals]
    assert not external_reading(repo, packet['project_id'])['briefing']['items']


def test_old_news_not_new_and_unsafe_sources_not_briefed(tmp_path):
    repo, packet, _, _ = collected(tmp_path)
    signals = repo.list_signals(packet['project_id'])
    for patch in ({'published_at': datetime.now(UTC)-timedelta(days=60)},
                  {'url': 'javascript:alert(1)'}, {'url': 'http://127.0.0.1/private'}):
        repo.list_signals = lambda _, patch=patch: [replace(s, **patch) for s in signals]
        assert not external_reading(repo, packet['project_id'])['briefing']['items']


def test_no_messages_only_coverage_not_empty_category_cards(tmp_path):
    repo, packet, strategy = fixture(tmp_path)
    activate(MonitorActivationStore(repo), packet, strategy)
    brief = external_reading(repo, packet['project_id'])['briefing']
    assert not brief['items']
    assert len(brief['coverage']) == 5
    assert {c['state'] for c in brief['coverage']} == {'pending', 'not_configured'}


def test_price_commentary_is_deferred_without_hiding_business_results(tmp_path):
    repo, packet, plan, runner = collected(tmp_path)
    runner.adapters = {'rss': ManualSnapshotAdapter({plan.target: (
        FeedItem('price', 'Industrial vision systems 跌0.83%，成交额6亿元，后市有机会？',
                 'https://example.org/price', '', datetime.now(UTC)),
        FeedItem('results', 'Industrial vision systems 净利润下跌20%，股价下跌',
                 'https://example.org/results', '', datetime.now(UTC)),
        FeedItem('target', 'Industrial vision systems 上调目标价，维持买入评级',
                 'https://example.org/target', '', datetime.now(UTC)),
    )})}
    runner.run(plan.id, idempotency_key='more')
    reading = external_reading(repo, packet['project_id'])
    assert reading['briefing']['deferred_market_count'] == 2
    assert len(reading['briefing']['items']) == 2
    assert any('净利润' in i['statement'] for i in reading['briefing']['items'])
    assert sum(len(g['items']) for g in reading['categories']) == 4


def test_cross_category_dedup_does_not_report_false_zero_coverage(tmp_path):
    repo, packet, strategy = fixture(tmp_path)
    activate(MonitorActivationStore(repo), packet, strategy)
    plans = repo.list_monitoring_plans(packet['project_id'])
    news = FeedItem('same', 'Synthetic public company Industrial vision systems new product',
                   'https://example.org/shared', '', datetime.now(UTC))
    runner = MonitoringRunner(repo, {'rss': ManualSnapshotAdapter({p.target: (news,) for p in plans})})
    for p in plans:
        runner.run(p.id, idempotency_key='same')
    brief = external_reading(repo, packet['project_id'])['briefing']
    assert len(brief['items']) == 1
    assert next(c for c in brief['coverage'] if c['key'] == 'company')['item_count'] == 1
    assert next(c for c in brief['coverage'] if c['key'] == 'sector')['item_count'] == 1


def test_sector_projection_can_use_existing_named_scope_only_as_grouping_hint(tmp_path):
    from project_watch.monitor_briefing import build_external_briefing
    repo,packet,_,_=collected(tmp_path)
    project=packet['project_id']
    base=dict(project_id=project, freshness='current', status='new', published_at=datetime.now(UTC),
              title='示例器械新项目签约', summary='9月22日，示例器械的医用精密导管及介入器械制造项目在示例高新区签约。',
              plan_id='plan', match_term='介入器械', attention={})
    sector=[dict(base,id='a',url='https://example.org/a'),dict(base,id='b',url='https://example.org/b')]
    named=dict(base,id='named',url='https://example.org/a',match_term='示例器械')
    categories=[dict(key='sector',title='赛道',coverage='checked',plans=[],items=sector),
                dict(key='competitors',title='同行',coverage='checked',plans=[],items=[named])]
    brief=build_external_briefing(repo,project,categories,None)
    assert len(brief['report_groups'])==1
    assert brief['report_groups'][0]['status']=='candidate'
    assert all(i['category']=='sector' and i['impact'] is None for i in brief['items'])
    named['project_id']='foreign'
    assert len(build_external_briefing(repo,project,categories,None)['report_groups'])==2
