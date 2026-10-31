"""Preserve rich existing judgment axes during the collection/analysis split."""
from copy import deepcopy

import pytest

from project_watch.studio.insight import counts
from project_watch.studio.insight_generation import normalize


def complete_input():
    numbered = {code: dict(kind='public', text=text, context=text, title=text,
                           source='Synthetic', url='https://example.org/' + code)
                for code, text in [('N1', '公司产线仍在试用'), ('C1', '竞品已进入试用'),
                                   ('A1', '论文报告实验结果'), ('P1', '专利申请公开'),
                                   ('I1', '行业需求增加') ]}
    def point(text, ref):
        return dict(text=text, refs=[ref])
    ring = dict(confidence='low', summary='产线仍在试用',
                **{'for': [point('公司产线仍在试用', 'N1')],
                   'against': [point('竞品已进入试用', 'C1')]}, change_if=['量产证据出现'])
    data = dict(headline='合成研究摘要',
        position={**deepcopy(ring), 'verdict': 'behind', 'peers': ['合成竞品']},
        operations={**deepcopy(ring), 'verdict': 'steady'},
        chain={**deepcopy(ring), 'verdict': 'pressured'},
        technology=dict(route='合成路线', maturity='advanced', consensus='forming', confidence='low',
            summary='仍需实验验证', academic=[point('论文报告实验结果', 'A1')],
            industry=[point('专利申请公开', 'P1')], risks=[point('公司产线仍在试用', 'N1')]),
        industry=dict(verdict='growing', confidence='low', summary='需求增加但产线尚在试用',
            drivers=[point('行业需求增加', 'I1')], headwinds=[point('公司产线仍在试用', 'N1')]),
        gaps=['未收录公司经营报表'])
    return data, numbered


def test_complete_analysis_keeps_distinct_axes_and_counterevidence():
    data, numbered = complete_input()
    value = normalize(data, numbered)
    assert value['position']['verdict'] == 'behind'
    assert value['operations']['verdict'] == 'steady'
    assert value['chain']['verdict'] == 'pressured'
    assert value['industry']['verdict'] == 'growing'
    assert value['technology']['maturity'] == 'advanced'
    assert value['technology']['consensus'] == 'forming'
    assert value['technology']['academic'][0]['refs'][0]['code'] == 'A1'
    assert value['technology']['industry'][0]['refs'][0]['code'] == 'P1'
    for key in ('position', 'operations', 'chain'):
        assert value[key]['against'] and value[key]['change_if']
    assert value['industry']['headwinds'] and value['technology']['risks']
    assert value['gaps'] == data['gaps']
    assert {ref['code'] for ref in value['cited']} == set(numbered)


@pytest.mark.parametrize('section', ['position', 'operations', 'chain', 'industry', 'technology'])
def test_missing_section_is_a_gap_not_another_dimension_or_fixed_judgment(section):
    data, numbered = complete_input()
    del data[section]
    value = normalize(data, numbered)
    assert value[section]['confidence'] == 'low'
    assert value[section]['summary'] == ''
    assert value[section].get('verdict', value[section].get('maturity')) == 'unclear'


def test_source_counts_use_actual_kinds_and_ignore_array_order():
    _, numbered = complete_input()
    assert counts(numbered) == {'N': 1, 'C': 1, 'I': 1, 'A': 1, 'P': 1}
    assert counts(dict(reversed(list(numbered.items())))) == counts(numbered)
    del numbered['P1']
    assert 'P' not in counts(numbered)
