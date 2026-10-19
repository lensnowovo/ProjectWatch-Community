"""Bounded person-order citation controls, not universal semantic entailment."""
import socket
from copy import deepcopy
from types import SimpleNamespace

import pytest

from project_watch.studio.public_profile import build_public_profile


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('person qualifier tests are offline')
    monkeypatch.setattr(socket, 'socket', denied)
    monkeypatch.setattr(socket, 'getaddrinfo', denied)


def compile_text(text, quote, *, label='甲', body=None, kind='person', extra=None, rows=None,
                 title='合成研究公司公告'):
    sources = [{'title': title, 'url': 'https://synthetic.example/first',
        'body': body or quote, 'published_at': '2025-04-01'}]
    if extra:
        sources.append({'title': '合成研究公司另一公告', 'url': 'https://synthetic.example/second',
            'body': extra, 'published_at': '2024-04-01'})
    raw = {'facts': rows if rows is not None else [{'kind': kind, 'label': label, 'text': text,
        'date': '', 'refs': [{'ref': 'P1', 'quote': quote}]}]}
    before, calls = deepcopy(raw), []
    class Model:
        ready = True
        def json(self, *args, **kwargs):
            calls.append(1)
            return raw
    result = build_public_profile({'name': '合成研究公司', 'legal_name': '合成研究公司'},
        SimpleNamespace(has_search=False), Model(), sources)
    assert raw == before and calls == [1]
    return result


@pytest.mark.parametrize('claim', [
    '甲是公司首位职业经理人阶段管理者之一', '甲为公司首任总经理', '甲为第一任董事长',
])
def test_names_list_is_not_evidence_for_first_role(claim):
    quote = '合成研究公司管理人员包括甲、乙、丙，名单不说明先后。'
    result = compile_text(claim, quote)
    assert result['facts'] == []
    assert result['outcome']['rejected_rows'] == [{'id': 'F01', 'reason': 'unsupported_person_order'}]


@pytest.mark.parametrize('text,quote', [
    ('甲为首任总经理', '合成研究公司正式任命甲为首任总经理。'),
    ('甲不是首任总经理', '合成研究公司说明甲并不是首任总经理。'),
    ('甲为首位管理者', '合成研究公司公告甲为首位管理者。'),
    ('甲是首席执行官', '合成研究公司公告甲为首席执行官。'),
    ('甲担任董事长兼CEO', '合成研究公司公告甲为董事长兼CEO。'),
])
def test_supported_first_negation_and_ordinary_roles_keep_original_wording(text, quote):
    result = compile_text(text, quote)
    assert [f['text'] for f in result['facts']] == [text]
    assert result['facts'][0]['refs'][0]['quote'] == quote


@pytest.mark.parametrize('text,quote', [
    ('甲为首任总经理', '合成研究公司明确说明甲并不是首任总经理。'),
    ('甲不是首任总经理', '合成研究公司明确任命甲为首任总经理。'),
    ('甲为首任CEO', '合成研究公司明确任命甲为首任总经理。'),
    ('甲为首任总经理', '合成研究公司明确任命乙为首任总经理。'),
])
def test_first_role_requires_matching_polarity_office_and_person(text, quote):
    assert compile_text(text, quote)['facts'] == []


def test_first_role_in_uncited_body_or_other_source_cannot_support_claim():
    quote = '合成研究公司管理人员包括甲、乙。'
    body = quote + '合成研究公司明确任命甲为首任总经理。'
    assert compile_text('甲为首任总经理', quote, body=body)['facts'] == []
    assert compile_text('甲为首任总经理', quote, extra=body)['facts'] == []


def test_independently_supported_prefix_survives_without_inventing_a_list_fact():
    quote = '合成研究公司甲任管理者，后来离职。'
    result = compile_text('甲曾任管理者并已离职，为公司首位职业经理人阶段管理者之一', quote)
    assert [f['text'] for f in result['facts']] == ['甲曾任管理者并已离职']
    assert result['outcome']['changed_rows'] == [
        {'id': 'F01', 'reasons': ['person_order_clauses_omitted']}]
    assert result['outcome']['status'] == 'partial'


def test_orphan_continuation_is_not_published_after_its_person_clause_is_removed():
    quote = '合成研究公司管理人员包括甲，负责经营。'
    assert compile_text('甲是首任总经理，负责经营', quote)['facts'] == []


def test_bad_first_role_keeps_sibling_and_stable_rejection_index():
    quote = '合成研究公司管理人员包括甲、乙。'
    rows = [
        {'kind': 'person', 'label': '甲', 'text': '甲为首任总经理', 'date': '',
         'refs': [{'ref': 'P1', 'quote': quote}]},
        {'kind': 'person', 'label': '乙', 'text': '乙列于管理人员名单', 'date': '',
         'refs': [{'ref': 'P1', 'quote': quote}]},
    ]
    result = compile_text('', quote, rows=rows)
    assert [f['label'] for f in result['facts']] == ['乙']
    assert result['outcome']['rejected_rows'][0]['id'] == 'F01'


def test_title_is_not_first_role_evidence():
    quote = '合成研究公司管理人员包括甲、乙。'
    assert compile_text('甲为首任总经理', quote,
                        title='合成研究公司首任总经理甲宣布新战略')['facts'] == []


def test_not_a_global_first_word_blacklist_for_products():
    quote = '合成研究公司推出首个合成光学模组。'
    result = compile_text('公司推出首个合成光学模组', quote, kind='product')
    assert result['facts'][0]['text'] == '公司推出首个合成光学模组'


def test_numeric_filter_remains_independent():
    quote = '合成研究公司明确任命甲为首任总经理。'
    result = compile_text('甲为首任总经理；甲持股87%', quote)
    assert result['facts'][0]['text'] == '甲为首任总经理'
    assert result['omitted_numeric_clauses'] == 1
