"""Read-time prioritization must never become verification or a model dispatch."""
from copy import deepcopy

import pytest

from project_watch.external_intelligence import build_intelligence


def story(**changes):
    return dict(id='s1', project_id='p1', category='company', statement='合成甲公司订单延期',
                match_term='合成甲公司', summary='', url='https://example.org/news',
                published_at='2026-10-07T00:00:00+00:00', content_scope='headline_and_feed_summary_only',
                relationship={'kind': 'unresolved', 'verified': False}, evidence_refs=[],
                unknowns=['主体尚待核实'], attention={'level': 'read_first', 'label': '经营动态'}, **changes)


def result(assessment='unclear', **changes):
    return dict(id='a1', recorded_at='2026-10-07T00:00:00+00:00',
                packet={'project_id': 'p1', 'signal_id': 's1', 'references': [
                    {'ref_id': 'news:title', 'kind': 'external', 'text': '合成甲公司订单延期',
                     'source_version': 'a'*64, 'locator': 'headline'}]},
                analysis={'project_id': 'p1', 'signal_id': 's1', 'content_scope': 'article_excerpts',
                          'source_statements': [{'text': '报道提及订单延期', 'evidence': []}],
                          'relationship': {'assessment': assessment, 'text': '主体需核实', 'evidence': []},
                          'implications': [], 'unknowns': ['需核实主体'], 'review_status': 'unreviewed'}, **changes)


def view(items=None, results=None):
    return build_intelligence('p1', {'items': items or [story()], 'coverage': [], 'report_groups': []},
                              {'project_id': 'p1', 'items': results or [], 'stale_count': 0})


def test_unclear_named_operational_story_is_visible_but_not_verified():
    response = view(results=[result()])
    item = response['items'][0]
    assert item['placement']['section'] == 'priority'
    assert item['placement']['basis'] == 'named_company_headline'
    assert item['analysis']['relationship']['assessment'] == 'unclear'
    assert item['verification'] == {'event': 'not_verified', 'identity': 'not_verified',
                                     'project_impact': 'not_verified', 'policy_applicability': 'not_verified'}
    assert item['analysis']['implications'] == []


@pytest.mark.parametrize('assessment,section', [('potentially_relevant', 'priority'), ('not_relevant', 'background')])
def test_saved_analysis_overrides_rule_when_decisive(assessment, section):
    item = view(results=[result(assessment)])['items'][0]
    assert item['placement']['section'] == section
    assert item['placement']['basis'] == 'saved_analysis'
    assert item['verification']['project_impact'] == 'not_verified'


def test_peer_or_uncertain_name_does_not_get_company_priority():
    for changes in ({'category': 'competitors'}, {'match_term': '其他公司'}):
        item = story()
        item.update(changes)
        assert view([item])['items'][0]['placement']['section'] == 'unresolved'


def test_social_news_and_market_commentary_are_not_priority():
    item = story()
    item.update(statement='合成甲公司周年活动', attention={'level': 'all_news'})
    assert view([item])['items'][0]['placement']['section'] == 'unresolved'
    item['statement'] = '合成甲公司获券商上调目标价'
    assert view([item], [result('potentially_relevant')])['items'][0]['placement']['section'] == 'background'


def test_latest_current_result_is_used_once_no_cross_project_or_input_mutation():
    old, new, foreign = result('not_relevant'), result('potentially_relevant'), result()
    old.update(id='old', recorded_at='2026-10-06T00:00:00+00:00')
    foreign['packet']['project_id'] = 'other'
    foreign.update(id='foreign', recorded_at='2026-10-08T00:00:00+00:00')
    inputs = [old, foreign, new]
    snapshot = deepcopy(inputs)
    items = [story(), story()]
    other = story()
    other['project_id'] = 'other'
    response = view([*items, other], inputs)
    assert inputs == snapshot
    assert len(response['items']) == 1
    assert response['items'][0]['analysis_id'] == 'a1'
    assert response['counts'] == {'priority': 1, 'context': 0, 'background': 0, 'unresolved': 0}
    assert response['items'][0]['evidence'][0]['ref_id'] == 'news:title'


def test_sector_potential_is_context_not_direct_company_impact():
    item = story()
    item['category'] = 'sector'
    assert view([item], [result('potentially_relevant')])['items'][0]['placement']['section'] == 'context'
