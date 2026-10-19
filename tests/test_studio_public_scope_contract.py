"""Synthetic prompt delivery controls, not model semantic-accuracy tests."""
import socket
from types import SimpleNamespace

import pytest

from project_watch.studio.public_profile import build_public_profile


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError('public scope contract must remain offline')

    monkeypatch.setattr(socket, 'socket', denied)
    monkeypatch.setattr(socket, 'getaddrinfo', denied)


def request(excerpt, output, *, kind='business', date='', hits=None):
    calls = []

    class Model:
        ready = True

        def json(self, system, user, **kwargs):
            calls.append({'system': system, 'user': user, 'options': kwargs})
            return {'facts': [{'kind': kind, 'text': output, 'date': date,
                              'refs': [{'ref': 'P1', 'quote': excerpt}]}]}

    sources = hits if hits is not None else [{
        'title': '合成研究有限公司', 'url': 'https://synthetic.example/report',
        'snippet': excerpt, 'published_at': '2025-03-01T08:00:00Z',
    }]
    result = build_public_profile({'name': '合成研究', 'legal_name': '合成研究有限公司'},
        SimpleNamespace(has_search=False), Model(), sources)
    return result, calls


@pytest.mark.parametrize('required', [
    ('本条 refs', '未引用摘录', '独立', '创新型'),
    ('人物名单的列举顺序', '首任', '明确任命'),
    ('年龄', '治疗线数', '既往治疗', '复发条件'),
    ('唯一', '首个', '报道时点', '来源归属'),
    ('来源发布日期', '获批日', '任命日', '日期未知留空'),
])
def test_scope_requirements_are_delivered_in_the_actual_model_request(required):
    excerpt = '合成研究有限公司开发合成药物，名单列举甲乙，适用成人二线治疗后的患者。'
    result, calls = request(excerpt, '开发合成药物')
    assert len(calls) == 1
    assert all(term in calls[0]['system'] for term in required)
    assert excerpt in calls[0]['user']
    assert calls[0]['options'] == {'max_tokens': 6000}
    assert result['facts'][0]['text'] == '开发合成药物'


@pytest.mark.parametrize('excerpt,text,kind,date', [
    ('合成研究有限公司专注车载光学模组。', '专注车载光学模组', 'business', ''),
    ('合成研究有限公司明确任命甲为首任管理者，任命发生于2025年2月10日。',
     '甲为首任管理者，任命发生于2025年2月10日', 'person', '2025-02-10'),
    ('合成研究有限公司公告：截至2025年2月，合成药物适用于经过二线或以上治疗后成人复发患者。',
     '截至2025年2月，合成药物适用于经过二线或以上治疗后成人复发患者', 'product', ''),
])
def test_supported_synthetic_responses_keep_wording_scope_and_occurrence_date(excerpt, text, kind, date):
    result, calls = request(excerpt, text, kind=kind, date=date)
    assert len(calls) == 1
    assert result['facts'][0]['text'] == text
    assert result['facts'][0]['date'] == date
    assert result['facts'][0]['refs'][0]['quote'] == excerpt
    assert result['facts'][0]['refs'][0]['published_at'] == '2025-03-01T08:00:00Z'
    assert result['omitted_numeric_clauses'] == 0


def test_rule_delivery_does_not_bypass_numeric_citation_compilation():
    result, calls = request('合成研究有限公司收入32万元。', '收入32万元；亏损7.4万元')
    assert len(calls) == 1
    assert [row['text'] for row in result['facts']] == ['收入32万元']
    assert result['omitted_numeric_clauses'] == 1


def test_no_sources_means_no_model_request():
    result, calls = request('合成研究有限公司', '没有来源', hits=[])
    assert calls == [] and result['facts'] == []
