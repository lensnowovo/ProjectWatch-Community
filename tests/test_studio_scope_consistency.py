"""Fictional, offline regression examples; not real-model accuracy acceptance."""

from copy import deepcopy
from datetime import UTC, date, datetime, timedelta

import pytest
from test_studio import _item
from test_studio_race import _row, build, ev

from project_watch.studio import events, race


def test_cluster_reads_older_quarter_reports_and_preserves_starred_event(tmp_path):
    from project_watch.repository import ProjectRepository
    from project_watch.studio.llm import LLM
    from project_watch.studio.pipeline import Pipeline
    from project_watch.studio.settings import ModelConfig, SettingsStore
    from project_watch.studio.sources import Http
    from project_watch.studio.store import StudioStore

    repo = ProjectRepository(tmp_path / 'db.sqlite3')
    project = repo.create_project('合成公司')
    store = StudioStore(repo.db_path)
    pipeline = Pipeline(repo, store, SettingsStore(tmp_path / 'settings.json'))
    llm = LLM(ModelConfig(), Http(lambda *a: pytest.fail('must not request a model')))
    today = datetime.now(UTC)
    for n, days in enumerate((40, 10)):
        title = '合成公司2026年第二季度ADAS销量10万台'
        store.add_items(project.id, 'company', [{'key': str(n), 'title': title,
            'url': f'https://source{n}.example/report', 'source': f'来源{n}', 'snippet': '',
            'published_at': (today - timedelta(days=days)).isoformat()}])
        item = store.untriaged(project.id)[0]
        store.save_triage([{'id': item['id'], 'relevant': True, 'importance': 'notable',
                           'summary': title, 'impact': ''}])
        pipeline.cluster(project.id, llm, lambda _: None)
        if n == 0:
            old = store.list_events(project_id=project.id)[0]
            store.set_event_status(old['id'], 'starred')
    [event] = store.list_events(project_id=project.id)
    assert event['id'] == old['id'] and event['status'] == 'starred'
    assert event['source_count'] == 2
    assert event['event_at'] == old['event_at']


def test_different_products_cannot_rank_entire_companies():
    rows = [_row('合成甲', step=4, day='2026-10-01', is_self=True),
            _row('合成乙', step=3, day='2026-10-01'), _row('合成丙', step=2, day='2026-10-01')]
    for n, row in enumerate(rows):
        row['stage']['scope'] = f'PROD-{100+n}'
    result = race.position(rows, date(2026, 10, 9))
    assert not result['ranked'] and '第 1' not in result['sentence']
    assert '量产' in result['sentence']


def test_financing_round_is_not_a_capital_strength_rank():
    rows = [_row('合成甲', rnd='A轮', fday='2026-10-01', is_self=True),
            _row('合成乙', rnd='C轮', fday='2026-10-01')]
    sentence = race.position(rows, date(2026, 10, 9))['sentence']
    assert 'A轮' in sentence and '资本面落后' not in sentence


@pytest.mark.parametrize('text,expected', [
    ('合成药业AB-123上市申请获得NMPA批准', 6),
    ('合成药业AB-123上市申请获受理', 5),
    ('合成药业AB-123 IND获得NMPA批准', 1),
    ('合成药业AB-123预计上市申请获批', None),
    ('合成药业AB-123上市申请未获批准', None),
])
def test_application_and_approval_are_distinct(text, expected):
    assert race.classify_step(text, 'pharma') == expected


@pytest.mark.parametrize('first,second', [
    ('肝癌适应证中国', '肺癌适应证中国'),
    ('肝癌适应证中国', '肝癌适应证美国'),
])
def test_product_code_alone_does_not_establish_matching_clinical_pace(first, second):
    data = build(project={'name':'合成药业','industry':'医药'}, peers=[], company_events=[
        ev(f'合成药业AB-123 {first}启动II期临床', '2026-01-01'),
        ev(f'合成药业AB-123 {second}启动III期临床', '2026-09-01')])
    assert data['rows'][0]['pace_months'] is None


def test_manual_relationship_types_are_preserved_and_not_guessed():
    project = {'name':'合成甲'}
    rel = {'items':[
        {'peer_name':'合成乙', 'origin':'user_override', 'relation_type':'substitute'},
        {'peer_name':'合成丙', 'origin':'user_override', 'relation_type':'adjacent'},
    ], 'exclusions':[{'company_wide':True, 'peer_names':['合成丁']}]}
    before = deepcopy(rel)
    peers = race.peers_for(project, {'competitors':[{'name':'合成戊'}, {'name':'合成丁'}]}, rel)
    assert [(p['name'], p['relation_type']) for p in peers] == [
        ('合成乙', 'substitute'), ('合成丙', 'adjacent'), ('合成戊', 'unknown')]
    assert rel == before


@pytest.mark.parametrize('first,second', [
    ('2026年第二季度ADAS销量10万台', '2026年上半年ADAS销量10万台'),
    ('2026年第二季度ADAS销量10万台', '2026年第三季度ADAS销量10万台'),
    ('2026年第二季度ADAS销量10万台', '2025年第二季度ADAS销量10万台'),
    ('2026年第二季度ADAS销量10万台', '2026年第二季度机器人销量10万台'),
    ('2026年第二季度ADAS销量10万台', '2026年第二季度总销量10万台'),
    ('2026年第二季度ADAS销量10万台', '2026年第二季度ADAS销量20万台'),
    ('2026年第二季度ADAS销量10万台', '预计2026年第二季度ADAS销量10万台'),
    ('2026年第二季度中国ADAS销量10万台', '2026年第二季度美国ADAS销量10万台'),
    ('2026年第二季度XG-500 ADAS销量10万台', '2026年第二季度XG-600 ADAS销量10万台'),
    ('AB-123肝癌适应证上市申请获受理', 'AB-123肺癌适应证上市申请获受理'),
    ('AB-123上市申请获受理', 'AB-123上市申请获得NMPA批准'),
])
def test_differing_scopes_cannot_join_even_identical_headlines_or_model_pairs(first, second):
    rows = [_item(1, '合成公司公布业务进展', summary=first),
            _item(2, '合成公司公布业务进展', summary=second)]
    for ordered in (rows, list(reversed(rows))):
        groups, ambiguous = events.plan_groups(ordered, [])
        assert len(groups) == 2 and not ambiguous
        assert not groups[0].accepts(groups[1].items[0])


def test_same_quarter_sales_reports_join_across_publication_days_without_losing_sources():
    rows = [_item(1, '合成公司2026年第二季度ADAS销量10万台', when='2026-07-01T00:00:00+00:00'),
            _item(2, '合成公司披露2026年Q2 ADAS销量100000台', when='2026-08-01T00:00:00+00:00')]
    before = deepcopy(rows)
    groups, ambiguous = events.plan_groups(rows, [])
    assert len(groups) == 1 and not ambiguous and len(groups[0].items) == 2
    assert rows == before


@pytest.mark.parametrize('change', ['actor', 'missing_period', 'missing_metric', 'missing_quantity', 'too_late'])
def test_long_window_requires_exact_known_sales_scope(change):
    a = _item(1, '合成公司2026年第二季度ADAS销量10万台', when='2026-07-01T00:00:00+00:00')
    b = _item(2, a['title'], when='2026-08-01T00:00:00+00:00')
    if change == 'actor':
        b['title'] = b['title'].replace('合成公司', '合成另一公司')
    elif change == 'missing_period':
        b['title'] = b['title'].replace('2026年第二季度', '')
    elif change == 'missing_metric':
        b['title'] = b['title'].replace('ADAS销量', '交付')
    elif change == 'missing_quantity':
        b['title'] = b['title'].replace('10万台', '')
    else:
        b['published_at'] = b['collected_at'] = '2027-01-01T00:00:00+00:00'
    groups, ambiguous = events.plan_groups([a,b], [])
    assert len(groups) == 2 and not ambiguous


def test_existing_event_keeps_scope_guard_in_original_member_summaries():
    existing = [{'id':'old', 'title':'合成公司业务进展', 'summary':'业务更新', 'category':'company',
        'event_at':'2026-10-05T08:00:00+00:00',
        'item_evidence':'合成公司2026年第二季度ADAS销量10万台'}]
    rows = [_item(1, '合成公司业务进展', summary='2026年上半年ADAS销量10万台')]
    groups, ambiguous = events.plan_groups(rows, existing)
    assert len(groups) == 2 and not ambiguous


def test_financing_conflicts_still_do_not_join():
    for second in ('完成5亿元B轮融资', '完成2亿元C轮融资'):
        groups, ambiguous = events.plan_groups([
            _item(1,'合成公司融资进展',summary='完成2亿元B轮融资'),
            _item(2,'合成公司融资进展',summary=second)], [])
        assert len(groups) == 2 and not ambiguous


def test_same_sector_same_sales_but_different_exact_actors_do_not_join():
    groups, ambiguous = events.plan_groups([
        _item(1,'合成公司2026年第二季度ADAS销量10万台'),
        _item(2,'合成另一公司2026年第二季度ADAS销量10万台')], [])
    assert len(groups) == 2 and not ambiguous


def test_matching_product_indication_region_retains_observed_pace():
    data = build(project={'name': '合成药业', 'industry': '医药'}, peers=[], company_events=[
        ev('合成药业AB-123肝癌适应证中国启动II期临床', '2026-01-01'),
        ev('合成药业AB-123肝癌适应证中国启动III期临床', '2026-09-01')])
    assert data['rows'][0]['pace_months'] == 8
    assert data['rows'][0]['stage']['context'] == {'indication': ['肝癌'], 'region': ['CN']}


def test_extended_lookback_is_only_for_explicit_quarterly_quantity_reports():
    assert events.lookback_days([_item(1, '合成公司2026年第二季度ADAS销量10万台')]) == 120
    for text in ('合成公司2026年上半年ADAS销量10万台', '合成公司2026年度ADAS销量10万台',
                 '合成公司2026年第二季度营收10亿元', '合成公司预计2026年第二季度ADAS销量10万台',
                 '合成公司业务进展'):
        assert events.lookback_days([_item(1, text)]) == 21


def test_same_peer_stage_and_move_do_not_double_count_or_become_a_threat():
    me = _row('合成甲', is_self=True)
    peer = _row('合成乙', step=4, day='2026-10-01')
    peer['stage']['title'] = '合成乙XG-500实现量产交付'
    peer['move'] = {'title': peer['stage']['title'], 'date': '2026-10-01', 'importance': 'notable'}
    p = race.position([me, peer], date(2026, 10, 9))
    chip = p['chips'][-1]
    assert chip['label'] == '近 30 天同行进展' and chip['value'] == '1 家公司'
    assert chip['tone'] == '' and '最大' not in p['sentence']
    assert 'XG-500' in p['sentence']
