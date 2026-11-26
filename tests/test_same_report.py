"""One report under another outlet's headline joins its event; different facts never do."""
import pytest

from project_watch.studio import events as ev

SAME = [
    ('科大讯飞：端云一体星火X2.6及系列产品初步预计将在明年上半年发布',
     '科大讯飞：端云一体星火X2.6及系列产品初步预计将在明年上半年发布 - 科大讯飞：端云一体星火X2.6及系列产品初步预'),
    ('科大讯飞: 关于回购公司股份的进展公告内容摘要-证券之星', '科大讯飞: 关于回购公司股份的进展公告内容摘要'),
    ('学而思发布全新培优AI家教及旗舰学习机T6系列__南方+_南方plus', '学而思发布全新培优AI家教及旗舰学习机T6系列'),
    ('华为申请注册韬定律关联商标-36氪', '华为申请注册韬定律关联商标'),
    ('Anthropic上市之路面临三重威胁 - 今日头条', 'Anthropic上市之路面临三重威胁'),
    ('Plaud launches a new AI pin and a desktop meeting notetaker | TechCrunch',
     'Plaud launches a new AI pin and a desktop meeting notetaker'),
    ('Plaud回归国内市场 AI录音设备卷至百元级_财经评论(cjpl)股吧_东方财富网股吧 - 页面加载中', 'Plaud回归国内市场 AI录音设备卷至百元级'),
]

DIFFERENT = [
    # two products of one company
    ('讯飞晓医app-官方正版软件2026最新版本免费下载-应用宝官网', '讯飞听见app-官方正版软件2026最新版本免费下载-应用宝官网'),
    ('Apple launches iPhone 16 in China', 'Apple launches iPhone 16 Pro in China'),
    ('OpenAI releases GPT-5 for Plus users', 'OpenAI releases GPT-5 mini for Plus users'),
    # two disclosures of one buyback
    ('科大讯飞：截至2026年10月31日公司累计回购股份157.12万股', '科大讯飞：截至2026年11月20日公司累计回购股份241.16万股'),
    # what happened is after the dash, not an outlet name
    ("OpenAI's Sam Altman - fired by board", "OpenAI's Sam Altman - returns as CEO"),
    ('Pfizer to buy Seagen - deal valued at $43 billion', 'Pfizer to buy Seagen - talks collapse'),
    ('智谱AI冲刺港股IPO | 估值超400亿元', '智谱AI冲刺港股IPO | 腾讯退出股东名单'),
    ('Plaud Note Pro can record and summarize your meetings – and it’s $41 off',
     'Plaud Note Pro can record and summarize your meetings – and it’s $42 off'),
    # one added word changes the fact
    ('国家药监局批准恒瑞医药HR070803上市', '国家药监局不予批准恒瑞医药HR070803上市'),
    ('腾讯收购Ubisoft旗下子公司25%股权', '腾讯拟收购Ubisoft旗下子公司25%股权'),
    ('字节跳动收购Moonshot AI部分股权', '传字节跳动收购Moonshot AI部分股权'),
    ('宁德时代：2024年三季度净利润同比增长25%', '宁德时代：2024年前三季度净利润同比增长25%'),
    ('Nvidia Q3 earnings preview', 'Nvidia Q3 earnings'),
    ('百度Apollo在武汉开启全无人驾驶测试', '百度Apollo在武汉重新开启全无人驾驶测试'),
    # a tail that only ends like an outlet name is still the news
    ('我国商业航天迎来里程碑 - 朱雀三号', '我国商业航天迎来里程碑 - 天龙三号'),
    ('阿里巴巴发布AI新产品 | 面向企业的智能体平台', '阿里巴巴发布AI新产品 | 面向开发者的模型平台'),
    ('中国移动发布年度业绩 | 5G用户破10亿 卫星互联网', '中国移动发布年度业绩 | 发力算力网'),
    ('China exports beat forecasts | record trade surplus', 'China exports beat forecasts | narrower trade surplus'),
    ('Disney launches streaming bundle - Hulu + Disney+', 'Disney launches streaming bundle - ESPN+'),
    ('realme发布全新旗舰手机GT7-Pro+', 'realme发布全新旗舰手机GT7-Plus'),
    ('国家电网数字化项目公示｜签约国家电网', '国家电网数字化项目公示｜签约南方电网'),
    # a bare name or quote page
    ('科大讯飞股份有限公司', '科大讯飞股份有限公司2024年年度报告摘要 - 新浪财经'),
    ('虹软科技(688088)', '虹软科技（688088）11月16日主力资金净卖出624.01万元'),
    ('让人工智能更好赋能教育变革', '如何让人工智能更好赋能教育变革？记者采访'),
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


def test_reports_never_chain_through_a_third():
    items = [item(1, 'DeepSeek releases V3 model update'), item(2, 'DeepSeek releases V3 and R1 model update'),
             item(3, 'DeepSeek releases R1 model update')]
    groups, _ = ev.plan_groups(items, [])
    assert len(groups) == 3
