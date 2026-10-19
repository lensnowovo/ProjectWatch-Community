"""Query precision from R44/R45 ledgers (synthetic): cheaper and less off-domain retrieval."""

from project_watch.studio import signals as sig
from project_watch.studio.competition_research import industry_core, query_name
from project_watch.studio.pipeline import competitor_news_query


def test_industry_core_drops_connector_fragments_and_keeps_the_domain_head():
    assert industry_core('激光雷达研发与制造') == '激光雷达'
    assert industry_core('汽车电子零部件制造（连接器、线缆组件、天线）') == '汽车电子零部件 连接器 线缆组件'
    assert industry_core('通信电子设备制造（天线、连接器相关）') == '通信电子设备 天线 连接器'


def test_two_character_short_name_is_replaced_by_a_distinctive_name():
    assert query_name('天籁', ['天籁通讯'], '深圳市天籁通讯器材有限公司') == '天籁通讯'
    assert query_name('天籁', [], '深圳市天籁通讯器材有限公司') == '天籁通讯器材'
    assert query_name('禾赛科技', [], '上海禾赛科技有限公司') == '禾赛科技'
    assert query_name('Aeva', [], 'Aeva Technologies Inc') == 'Aeva'


def test_competitor_news_query_adds_one_domain_word_unless_present():
    project = {'industry': '汽车电子零部件制造（连接器、线缆组件、天线）'}
    assert competitor_news_query({'name': '罗森伯格'}, project) == '罗森伯格 连接器'
    assert competitor_news_query({'name': '电连技术'}, {'industry': ''}) == '电连技术'
    # National classification labels are too broad to disambiguate (R46b: "罗森伯格 计算机").
    assert competitor_news_query({'name': '罗森伯格'}, {'industry': '计算机、通信和其他电子设备制造业'}) == '罗森伯格'
    assert competitor_news_query({'name': '罗森伯格'}, {'industry': '计算机、通信和其他电子设备制造业'},
                                 {'industry_queries': ['车载连接器 市场']}) == '罗森伯格 车载连接器'
    assert competitor_news_query({'name': '速腾聚创'}, {'industry': '激光雷达研发与制造'}) == '速腾聚创 激光雷达'
    # A model-provided query is used only when it names the competitor.
    assert competitor_news_query({'name': '罗森伯格', 'query': '罗森伯格 车载连接器'}, project) == '罗森伯格 车载连接器'
    assert competitor_news_query({'name': '罗森伯格', 'query': '台湾 政治'}, project) == '罗森伯格 连接器'


class Search:
    has_search = True

    def __init__(self):
        self.queries = []

    def web_search(self, query, *, news=False, count=10):
        self.queries.append(query)
        return []


def test_public_records_search_one_name_and_skip_drug_regulator_outside_biomed():
    search = Search()
    sig.public_records(search, ['上海禾赛科技有限公司', '上海禾赛科技'])
    assert all(q.endswith(' 上海禾赛科技') for q in search.queries)
    assert not any('nmpa.gov.cn' in q for q in search.queries)
    assert len(search.queries) == 6
    biomed = Search()
    sig.public_records(biomed, ['合成生物有限公司', '合成生物'], biomed=True)
    assert any('nmpa.gov.cn' in q for q in biomed.queries) and len(biomed.queries) == 7


def test_classification_industry_falls_back_to_plan_or_description_phrase():
    from project_watch.studio.competition_research import industry_phrase

    label = '计算机、通信和其他电子设备制造业'
    desc = '车载连接器、线缆组件及天线的研发、制造与销售，应用于智能网联汽车'
    assert industry_phrase(label, desc, {'industry_queries': ['车载连接器 市场 动态']}) == '车载连接器'
    assert industry_phrase(label, desc, {}) == '车载连接器'
    assert industry_phrase('激光雷达研发与制造', desc, {'industry_queries': ['x 市场']}) == '激光雷达'
    assert industry_phrase(label, '', {}) == '计算机 通信 电子设备'
