"""One report under another outlet's headline joins its event; different facts never do."""
import pytest

from project_watch.studio import events as ev

SAME = [
    ('科大讯飞：端云一体星火X2.6及系列产品初步预计将在明年上半年发布',
     '科大讯飞：端云一体星火X2.6及系列产品初步预计将在明年上半年发布有投资者在互动_财富号_东方财富网'),
    ('科大讯飞星火语音基座大模型SparkAudio 1.0上线API服务', '科大讯飞星火语音大模型SparkAudio上线API服务'),
    ('AI业务占比连续三季过半，百度向智能组织高速迈进', 'AI业务占比连续三季度过半，百度向智能组织高速迈进'),
    ('腾讯发文：已到生死存亡时刻，未来不一定要那么多管理岗位', '腾讯发文：已到生死存亡时刻'),
    ('华为申请注册韬定律关联商标-36氪', '华为申请注册韬定律关联商标'),
    ('Anthropic上市之路面临三重威胁 - 今日头条', 'Anthropic上市之路面临三重威胁'),
]

DIFFERENT = [
    # two products of one company
    ('讯飞晓医app-官方正版软件2026最新版本免费下载-应用宝官网', '讯飞听见app-官方正版软件2026最新版本免费下载-应用宝官网'),
    # two disclosures of one buyback
    ('科大讯飞：截至2026年10月31日公司累计回购股份157.12万股', '科大讯飞：截至2026年11月20日公司累计回购股份241.16万股'),
    # a figure behind the outlet-style dash still counts
    ('Plaud Note Pro can record and summarize your meetings – and it’s $41 off',
     'Plaud Note Pro can record and summarize your meetings – and it’s $42 off'),
    # a bare company name heads its profile, its annual report and its news alike
    ('科大讯飞股份有限公司', '科大讯飞股份有限公司2024年年度报告摘要 - 新浪财经'),
    # a quote page is not the fund-flow report
    ('虹软科技(688088)', '虹软科技（688088）11月16日主力资金净卖出624.01万元'),
    # a short phrase inside another article's headline
    ('让人工智能更好赋能教育变革', '如何让人工智能更好赋能教育变革？记者采访'),
    # a hyphen inside a product name is not an outlet separator
    ('商汤发布SenseNova Loop-9B模型', '商汤开源 SenseNova Loop-9B-Preview：探索以计算深度释放'),
]


@pytest.mark.parametrize('a,b', SAME)
def test_one_report_under_two_headlines_is_one_event(a, b):
    assert ev.identity_supported(a, b) and ev.identity_supported(b, a)


@pytest.mark.parametrize('a,b', DIFFERENT)
def test_different_facts_stay_apart(a, b):
    assert not ev.identity_supported(a, b) and not ev.identity_supported(b, a)


def item(n, title, when='2026-11-10T08:00:00+00:00'):
    return {'id': f'i{n}', 'title': title, 'summary': '', 'category': 'company', 'importance': 'notable',
            'published_at': when, 'collected_at': when}


def test_a_repost_joins_an_event_that_already_holds_a_differently_worded_report():
    # The stored event once took "新架构模型…" through a confirmed pair; a later exact repost of its
    # own headline used to start a new event because it did not match that other member.
    stored = {'id': 'e1', 'category': 'company', 'event_at': '2026-11-10T00:00:00+00:00',
              'created_at': '2026-11-10T00:00:00+00:00', 'title': '科大讯飞：端云一体星火X2.6及系列产品初步预计将在明年上半年发布',
              'summary': '', 'item_titles': '科大讯飞：新架构模型初步预计将在明年上半年发布', 'item_evidence': ''}
    repost = item(1, '科大讯飞：端云一体星火X2.6及系列产品初步预计将在明年上半年发布 - 证券之星')
    groups, _ = ev.plan_groups([repost], [stored])
    assert [g.event_id for g in groups if g.items] == ['e1']


def test_reports_join_one_event_but_a_later_disclosure_does_not():
    items = [item(1, '科大讯飞：截至2026年11月20日公司累计回购股份241.16万股'),
             item(2, '科大讯飞：截至2026年11月20日公司累计回购股份241.16万股 - 证券之星', '2026-11-10T09:00:00+00:00'),
             item(3, '科大讯飞：截至2026年11月25日公司累计回购股份251.16万股', '2026-11-11T09:00:00+00:00')]
    groups, _ = ev.plan_groups(items, [])
    assert sorted(len(g.items) for g in groups) == [1, 2]
