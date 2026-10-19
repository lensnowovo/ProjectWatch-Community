"""Topic navigation must not assert event completion, identity or investment impact."""
from datetime import UTC, datetime, timedelta

import pytest

from project_watch.external_attention import headline_attention
from project_watch.external_focus import external_reading
from project_watch.monitoring import FeedItem, ManualSnapshotAdapter, MonitoringRunner
from project_watch.repository import ProjectRepository


@pytest.mark.parametrize(('title', 'topic'), [
    ('示例公司拟融资，尚未签署协议', 'capital'),
    ('示例公司否认完成融资', 'capital'),
    ('Example announces share repurchase program', 'capital'),
    ('Example reports quarterly revenue', 'performance'),
    ('示例公司订单取消', 'commercial'),
    ('示例公司产品召回', 'regulatory'),
    ('Example receives FDA approval', 'regulatory'),
    ('示例公司新药获批上市', 'regulatory'),
    ('示例公司临床试验未达主要终点', 'regulatory'),
    ('Example appoints new CEO', 'leadership'),
    ('示例公司发布新产品', 'product'),
    ('示例公司回应光电融合：推进技术储备与产品布局', 'product'),
    ('示例公司披露研发进展', 'product'),
])
def test_headline_cues_are_topics_not_outcomes(title, topic):
    result = headline_attention(title, '')
    assert result['topic'] == topic
    assert result['level'] == 'read_first'
    assert result['cue'] in title
    assert result['basis'] == 'headline_topic_only'
    assert result['impact'] is None
    assert result['event_verified'] is False
    assert result['reason']


@pytest.mark.parametrize('title', [
    'Example celebrates employee stories',
    'Example announces scholarship recipients',
    'Example discusses return on investment in a podcast',
    'Example hosts an annual conference',
    '示例公司年度员工活动',
    'Example approves office decorations',
    'Example launches an internal book club',
])
def test_general_news_does_not_become_an_investment_event(title):
    assert headline_attention(title, '')['level'] == 'all_news'


def test_company_name_itself_is_not_a_topic_cue():
    assert headline_attention('融资科技举办周年活动', '融资科技')['level'] == 'all_news'
    assert headline_attention('CEO Holdings hosts a conference', 'CEO Holdings')['level'] == 'all_news'
    assert headline_attention('融资科技完成融资', '融资科技')['topic'] == 'capital'


def test_reading_attaches_transparent_priority_without_dropping_or_rewriting_news(tmp_path):
    repo = ProjectRepository(tmp_path / 'watch.sqlite3')
    project = repo.create_project('Example')
    other = repo.create_project('Other')
    plan = repo.create_monitoring_plan(project.id, kind='rss', target='https://example.org/feed', cadence_minutes=1440)
    now = datetime.now(UTC)
    items = (
        FeedItem('1', 'Example scholarship recipients', 'https://example.org/news', 'revenue mentioned only in body', now),
        FeedItem('2', 'Example denies fundraising report', 'https://example.org/news', 'No deal.', now-timedelta(days=1)),
        FeedItem('3', 'Example share repurchase', 'https://example.org/old', '', now-timedelta(days=60)),
    )
    MonitoringRunner(repo, {'rss':ManualSnapshotAdapter({plan.target:items})}).run(plan.id, idempotency_key='one')
    before = repo.list_signals(project.id)
    result = external_reading(repo, project.id)['categories'][0]['items']
    assert len(result) == 2  # Same generic URL, different headlines both retained.
    assert {s['title'] for s in result} == {i.title for i in items[:2]}
    by_title = {s['title']:s for s in result}
    assert by_title[items[0].title]['attention']['level'] == 'all_news'
    assert by_title[items[1].title]['attention']['level'] == 'read_first'
    assert repo.list_signals(project.id) == before
    assert all(not g['items'] for g in external_reading(repo, other.id)['categories'])
    assert external_reading(ProjectRepository(repo.db_path), project.id)['categories'][0]['items'] == result
