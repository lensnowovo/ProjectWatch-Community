from datetime import date

from project_watch.studio import race, techroutes, track


def test_future_dates_and_legacy_storylines_do_not_rank_progress():
    from test_studio_race import build, ev
    assert not race._within('2027-01-01', date(2026, 10, 9), 90)
    assert build(company_events=[ev('合成视界实现量产', '2027-01-01')])['rows'][0]['stage'] is None
    assert build(threads=[{'support': 'legacy_unverified', 'kind': '产品', 'steps': [
        {'stage': '规模销售', 'date': '2026-10-01', 'title': '合成视界预计规模销售'}]}])['rows'][0]['stage'] is None
    assert race.template_for({'industry': '仓储物流'}, None) == 'other'
    assert race.classify_step('物流公司已交付', 'other') is None
    assert race.template_for({'industry': 'AI芯片'}, None) == 'hardware'


def test_missing_publication_date_is_not_discovery_date():
    e = {'event_at': '2026-10-09', 'sources': [{'published_at': None}]}
    assert race._event_day(e) == ''


def test_reader_resolved_official_host_and_original_url_are_preserved(tmp_path):
    from test_studio_structured_facts import prepare
    p, pid, _, _, _ = prepare(tmp_path)
    p.store.add_items(pid, 'company', [{'key': 'synthetic-link', 'title': '合成公司中标', 'url': 'https://news.example/redirect',
        'source': '某媒体', 'snippet': '', 'published_at': '2026-10-01'}])
    item = p.store.items_needing_body(pid)[0]
    p.store.save_body(item['id'], 'article_body', '合成公司中标。' * 30, resolved_url='https://www.ccgp.gov.cn/notice/1')
    p.store.save_triage([{'id': item['id'], 'relevant': True}])
    row = p.store.list_items(project_id=pid)[0]
    assert row['url'] == 'https://www.ccgp.gov.cn/notice/1'
    assert row['extra']['original_url'] == 'https://news.example/redirect'
    assert race.grade_of(row['url']) == '公示'


def test_patent_applicant_is_not_the_search_query_and_substring_host_is_rejected():
    from project_watch.studio.http import Http
    from project_watch.studio.settings import SearchConfig
    from project_watch.studio.sources import Collectors
    c = Collectors(Http(lambda *_: None), SearchConfig('bocha', 'synthetic-key'))
    c.web_search = lambda *_args, **_kw: [
        {'url': 'https://patents.google.com/patent/a', 'snippet': '申请人：合成专利甲；摘要：光学路线', 'extra': {}},
        {'url': 'https://patents.google.com/patent/b', 'snippet': '提到合成查询公司', 'extra': {}},
        {'url': 'https://patents.google.com.evil.example/patent/c', 'snippet': '申请人：恶意主体'}]
    rows = c.patents('合成查询公司')
    assert rows[0]['extra']['applicants'] == ['合成专利甲']
    assert 'applicants' not in rows[1]['extra'] and len(rows) == 2


def test_patent_entrants_and_partial_quarter_never_claim_overtaking():
    patents = [{'published_at': '2026-09-01', 'extra': {'applicants': ['合成专利甲']}, 'url': 'https://patents.example/1'}]
    assert techroutes.entrants(papers=[], patents=patents, deals_docs=[], known=[], today=date(2026, 10, 9))[0]['kind'] == '专利申请人'
    rows = [{'term': 'FMCW', 'own': True, 'values': [2, 2, 2, 2, 4, 1], 'total': 13},
            {'term': 'MEMS', 'own': False, 'values': [1, 1, 1, 1, 2, 50], 'total': 56}]
    assert '首次超过' not in techroutes.insight(rows)


def test_uncited_market_numbers_cannot_define_demand():
    result = track.demand({'metrics': [{'name': '市场规模', 'value': 100, 'refs': []}]}, [], date(2026, 10, 9), '合成甲')
    assert result['direction'] == '信息不足'


def test_recent_report_is_not_a_step_up_and_stock_prices_are_not_business_moves():
    from test_studio_race import build, ev
    data = build(company_events=[ev('合成视界实现量产', '2026-10-01')], competitor_events=[
        ev('合成竞品甲盘中涨幅达5%', '2026-10-08', importance='major'),
        ev('合成竞品甲发布新方案', '2026-09-01'), ev('合成竞品甲签约新客户', '2026-10-02')])
    assert '上了一级' not in data['position']['sentence']
    assert data['rows'][1]['move']['date'] == '2026-10-02'
    assert race.classify_step('合成药业完成III 期临床', 'pharma') == 4


def test_vendor_funding_does_not_get_replaced_by_a_newer_media_title():
    from test_studio_race import build, ev
    record = {'kind':'funding','company':'合成竞品甲','round':'B轮','amount':'数亿元',
              'date':'2026-08-01','provider':'xiniu'}
    row = build(commercial_records=[record], competitor_events=[ev('合成竞品甲完成C轮融资','2026-10-01')])['rows'][1]
    assert row['funding']['grade'] == '数据商' and row['funding']['round'] == 'B轮'


def test_brands_and_product_codes_are_not_technical_routes():
    assert techroutes._terms('SYN品牌 XG-500 产品 Example Roadmap，RAG 与 Agent 路线') == {'RAG','Agent'}


def test_legacy_briefs_preserve_evidence_gaps_without_delegating_verification():
    from project_watch.studio.public_copy import brief_view
    original = {'summary':'数字口径不同，需核实。', 'watch':[{'text':'交付日期待核验。'}],
                'refs':[{'quote':'原文说需核实。'}]}
    view = brief_view(original)
    assert '系统依据不足，暂不判断' in view['summary']
    assert '待核验' not in view['watch'][0]['text']
    assert view['refs'] == original['refs'] and original['summary'].endswith('需核实。')


def test_relation_customers_are_scoped_to_their_company_and_original_public_evidence():
    relations = [{'visibility':'public','support':'source_assertion','subject_name':'合成竞品甲',
                  'peer_name':'合成客户乙','role':'pilot_partner','sources':[{'url':'https://press.example/1','source':'媒体'}]},
                 {'visibility':'internal','support':'source_assertion','subject_name':'合成竞品甲',
                  'peer_name':'保密客户','role':'customer'}]
    assert race.extras(['合成视界'],[],relations=relations)['customers'] == []
    customers = race.extras(['合成竞品甲'],[],relations=relations)['customers']
    assert len(customers) == 1 and customers[0]['who'] == '合成客户乙' and customers[0]['what'] == '试点合作方'


def test_bad_event_states_keep_original_links_and_only_current_window():
    from test_studio_red_flags import build
    recent = {'name':'合成产线','support':'source_assertion','state':'delayed','effective_at':'2026-10-01',
              'history':[{'subject_name':'合成视界'}], 'sources':[{'url':'https://www.ccgp.gov.cn/a','source':'公示原文'}]}
    old = {**recent,'effective_at':'2025-01-01'}
    data = build(event_states=[recent,old])
    assert len(data['company']) == 1
    assert data['company'][0]['grade'] == '公示' and data['company'][0]['url'] == recent['sources'][0]['url']


def test_strategy_and_research_reports_are_not_completed_milestones_or_peer_actions():
    from test_studio_race import build, ev
    assert race.classify_step('合成视界与客户合作，加速激光雷达规模化应用', 'hardware') is None
    data = build(competitor_events=[ev('合成竞品甲深度研究：第二增长曲线成色','2026-10-01')])
    assert data['rows'][1]['move'] is None
    terminal = build(company_events=[ev('合成视界完成规模化交付','2026-01-01')])
    assert '未见下一级' not in terminal['position']['sentence']


def test_clinical_breakthrough_does_not_establish_market_growth_or_cross_product_pace():
    from test_studio_track import build, cell, ev
    industry = {'metrics':[{'name':'合成药市场规模','value':187,'unit':'亿元',
                            'refs':[{'url':'https://press.example/1'}]}]}
    data = build(industry=industry,industry_events=[ev('合成药拟纳入突破性治疗','2026-10-01'),
                                                ev('合成药获突破性治疗认定','2026-09-01')])
    assert cell(data,'demand')['direction'] == '方向未披露'
    from test_studio_race import build as race_build
    from test_studio_race import ev as race_ev
    data = race_build(project={'name':'合成药业','industry':'医药'},peers=[],company_events=[
        race_ev('合成药业AB-123启动III 期临床','2026-04-01'),race_ev('合成药业CD-456上市申请获受理','2026-06-01')])
    assert 'previous' not in data['rows'][0]['stage'] and data['rows'][0]['pace_months'] is None


def test_margin_trades_and_dividends_are_not_primary_financing_or_product_launches():
    from project_watch.studio.landscape import classify_move
    assert classify_move('合成公司融资融券余额5亿元') == '交易行情'
    assert classify_move('合成公司半年度利润分配方案公告') == '财务披露'


def test_stockholder_sales_and_generic_disclosures_are_not_peer_business_threats():
    from test_studio_race import build, ev
    data = build(competitor_events=[ev('合成竞品甲创始人减持股份套现','2026-10-04'),
                                    ev('合成竞品甲：自愿公告－业务最新情况','2026-10-05'),
                                    ev('合成竞品甲发布股票交易异常波动公告','2026-10-03')])
    assert data['rows'][1]['move'] is None
    assert not any(p['kind'] == 'threat' for p in data['position']['parts'])
    disclosure = ev('合成竞品甲：自愿公告','2026-10-05')
    disclosure['summary'] = '合成竞品甲发布业务更新公告'
    assert build(competitor_events=[disclosure])['rows'][1]['move'] is None


def test_etf_market_commentary_and_future_publications_are_not_policy_evidence():
    from test_studio_track import build, cell, ev
    data = build(policy_events=[ev('创新药ETF获政策利好，指数大涨4%','2026-10-04'),
                                ev('国家支持创新药发展规划','2027-10-04')])
    assert cell(data,'policy')['direction'] == '信息不足'


def test_stage_sources_do_not_mix_different_products_dates_and_outlets():
    from test_studio_race import build, ev
    data = build(company_events=[ev('合成视界XG-500完成量产','2026-10-01',url='https://www.ccgp.gov.cn/a',source='公示'),
                                ev('合成视界XG-600完成量产','2026-09-01',source='媒体')])
    stage = data['rows'][0]['stage']
    assert stage['date'] == '2026-10-01' and stage['outlets'] == ['公示']


def test_commercial_agreements_are_valid_but_not_regulatory_approvals():
    import pytest
    from pydantic import ValidationError

    from project_watch.studio.detail_contracts import Event
    assert Event(kind='event',name='合成供货协议',channel='commercial',state='agreement',state_text='供货协议').state == 'agreement'
    with pytest.raises(ValidationError):
        Event(kind='event',name='合成审批',channel='regulatory',state='agreement',state_text='审批')
