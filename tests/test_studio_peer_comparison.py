from copy import deepcopy
from datetime import date

import pytest
from test_studio_agent_instance import make_service
from test_studio_race import PROJECT, build, ev

from project_watch.studio import race
from project_watch.studio.landscape import complete_competitors


@pytest.mark.parametrize('name', ['未知', ' 待补充 ', 'N/A', 'ＵＮＫＮＯＷＮ', '暂无', '—'])
def test_placeholder_peers_do_not_enter_projections_or_plans(tmp_path, name):
    service, pid = make_service(tmp_path / 'names.sqlite3')
    plan = {'competitors': [{'name': name}, {'name': '未知科技'}, {'name': 'AI'}]}
    saved = deepcopy(plan)
    assert [p['name'] for p in service.objects.scoped_plan(pid, plan)['competitors']] == ['未知科技', 'AI']
    assert [p['name'] for p in service.relationships.public_plan(pid, plan)['competitors']] == ['未知科技', 'AI']
    assert [p['name'] for p in race.peers_for(PROJECT, plan, {})] == ['未知科技', 'AI']
    comp = complete_competitors({'profiles': [{'name': name, 'cells': {}}]}, [name, 'AI'], [])
    assert [p['name'] for p in comp['candidates']] == ['AI']
    assert comp['profiles'] == []
    assert plan == saved


def quantity(name, value='10万', *, quarter='Q1', product='激光雷达', region='中国', metric='出货量', segment='ADAS'):
    return ev(f'{name}2026年{quarter} {region}{segment}“{product}”产品{metric}{value}台',
              '2026-09-01', url=f'https://news.example/{name}/{value}')


def numbers(own, peers):
    return build(company_events=own, competitor_events=peers)['quantities']


def test_equal_explicit_scopes_pair_exact_scaled_values_without_ranking():
    result = numbers([quantity('合成视界')], [quantity('合成竞品甲', '90000')])
    assert len(result['groups']) == 1
    rows = result['groups'][0]['records']
    assert [r['company'] for r in rows] == ['合成视界', '合成竞品甲']
    assert [r['value'] for r in rows] == ['100000', '90000']
    assert all(r['url'] and r['quote'] in r['title'] for r in rows)
    assert result['ranked'] is False


@pytest.mark.parametrize('change', [dict(quarter='Q2'), dict(product='汽车连接器'), dict(region='美国'),
                                  dict(metric='销量'), dict(metric='交付量'), dict(segment='机器人'), dict(region=''), dict(product='')])
def test_mismatched_or_missing_scopes_preserve_individual_numbers(change):
    result = numbers([quantity('合成视界')], [quantity('合成竞品甲', **change)])
    assert result['groups'] == []
    assert len(result['records']) == 2


def test_conflicting_values_same_company_do_not_choose_latest_or_largest():
    result = numbers([quantity('合成视界'), quantity('合成视界', '12万')], [quantity('合成竞品甲')])
    assert result['groups'] == [] and len(result['records']) == 3


def test_forecasts_multi_metric_and_model_summary_cannot_make_quantity_records():
    forecast = quantity('合成视界')
    forecast['sources'][0]['title'] = forecast['title'] = forecast['title'].replace('出货量', '预计出货量')
    ambiguous = quantity('合成竞品甲')
    ambiguous['sources'][0]['title'] += '，销量8万台'
    summary_only = ev('合成竞品甲发布产品', '2026-09-01')
    summary_only['summary'] = quantity('合成竞品甲')['title']
    assert numbers([forecast], [ambiguous, summary_only])['records'] == []


def test_duplicate_sources_do_not_multiply_comparable_companies():
    result = numbers([quantity('合成视界')] * 2, [quantity('合成竞品甲')] * 3)
    assert len(result['groups']) == 1 and len(result['groups'][0]['records']) == 2


def test_source_date_not_collection_date_controls_future_filter():
    future = quantity('合成视界')
    future['sources'][0]['published_at'] = '2027-01-01'
    assert numbers([future], [])['records'] == []


def test_open_period_keeps_report_but_cannot_pair_completed_quantities():
    result = numbers([quantity('合成视界', quarter='Q4')], [quantity('合成竞品甲', quarter='Q4')])
    assert len(result['records']) == 2 and not result['groups']


def test_unknown_discovery_is_not_saved_even_when_source_mentions_it(tmp_path):
    from test_studio_evidence_versions import sample

    from project_watch.studio.competition_research import discover_candidates
    service, pid = make_service(tmp_path / 'discovery.sqlite3')
    target = service.objects.ensure_subject(pid)
    source = service.evidence.save(sample(pid, '未知公司拥有激光雷达产品。').model_copy(update={'visibility': 'public'}))
    before = service.objects.page(pid)
    assert discover_candidates(service, pid, target['id'], [source], {'candidates': [
        {'name': '未知公司', 'source': 'D1', 'quote': '未知公司拥有激光雷达产品。'}]}) == []
    assert service.objects.page(pid) == before


def test_missing_quantitative_market_direction_is_not_inferred_from_headline_votes():
    from project_watch.studio import track
    reports = [track._ev(ev('激光雷达装车量增长', '2026-09-01'))] * 3
    industry = {'metrics': [{'name': '装车量', 'value': 10, 'unit': '万台', 'period': '2026Q1',
                            'refs': [{'url': 'https://news.example/metric'}]}]}
    cell = track.demand(industry, reports, date(2026, 10, 9), '合成视界')
    assert cell['tone'] == 'unknown' and not cell['for_us']


def test_direct_race_build_also_filters_placeholder_peers():
    assert len(build(peers=[{'name': '未知'}])['rows']) == 1


def test_report_count_is_not_a_market_growth_signal():
    from project_watch.studio import track
    reports = [track._ev(ev('激光雷达装车量增长', '2026-09-01')),
               track._ev(ev('激光雷达出货量增长', '2026-09-02'))]
    cell = track.demand(None, reports, date(2026, 10, 9), '合成视界')
    assert cell['tone'] == 'neutral' and cell['direction'] == '报道信号'
    assert not cell['for_us']
