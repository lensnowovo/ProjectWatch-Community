"""Synthetic public-profile compiler regressions; no provider requests."""
import socket
from types import SimpleNamespace

import pytest
from test_studio_agent_instance import make_service

from project_watch.studio.public_profile import build_public_profile


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError('numeric support regression must remain offline')

    monkeypatch.setattr(socket, 'socket', denied)
    monkeypatch.setattr(socket, 'getaddrinfo', denied)


def compile_profile(tmp_path, text, excerpts, *, refs=None, located=True):
    service, pid = make_service(tmp_path / 'synthetic.sqlite3')
    project = {**service.project(pid), 'name': '合成光学', 'legal_name': '合成光学'}
    references = refs if refs is not None else [{'ref': 'P1', 'quote': '合成光学公布经营情况'}]

    class Model:
        ready = True

        def json(self, *_args, **_kwargs):
            return {'facts': [{'kind': 'business', 'label': '经营情况', 'text': text, 'refs': references}]}

    hits = [{'title': '合成光学', 'url': f'https://synthetic.example/{i}', 'snippet': excerpt}
            for i, excerpt in enumerate(excerpts)]
    return build_public_profile(project, SimpleNamespace(has_search=False), Model(), hits,
                                evidence=service.evidence if located else None), service, pid


@pytest.mark.parametrize('located', [False, True])
def test_valid_income_quote_does_not_allow_extra_uncited_loss(tmp_path, located):
    result, service, pid = compile_profile(tmp_path, '收入32百万元；亏损7.4万元，同比减少83.2%。',
        ['合成光学公布经营情况。收入32百万元。'], located=located)
    assert [f['text'] for f in result['facts']] == ['收入32百万元']
    assert result['omitted_numeric_clauses'] == 1
    if located:
        ref = result['facts'][0]['refs'][0]
        part = service.evidence.get(pid, ref['evidence_version_id'])['parts'][ref['part_index']]
        assert part['text'][ref['start']:ref['end']] == ref['quote']


@pytest.mark.parametrize('ref', [None, {'ref': 'P2', 'quote': '没有这段文字'}])
def test_uncited_or_invalid_source_cannot_supply_figures(tmp_path, ref):
    refs = [{'ref': 'P1', 'quote': '合成光学公布经营情况'}] + ([ref] if ref else [])
    result, _, _ = compile_profile(tmp_path, '收入32百万元；亏损7.4万元。',
        ['合成光学公布经营情况。收入32百万元。', '合成光学公布经营情况。亏损7.4万元。'], refs=refs)
    assert [f['text'] for f in result['facts']] == ['收入32百万元']
    assert result['omitted_numeric_clauses'] == 1


@pytest.mark.parametrize('text,source', [
    ('收入132万元', '收入32万元'),
    ('收入32万元', '收入32百万元'),
    ('收入32亿美元', '收入人民币32百万元'),
    ('增长32个百分点', '增长32%'),
    ('收入达到32万元', '收入未达到32万元'),
    ('利润-32万元', '利润32万元'),
    ('员工32人', '客户32家'),
])
def test_numeric_substrings_units_currency_and_negation_do_not_substitute(tmp_path, text, source):
    result, _, _ = compile_profile(tmp_path, text, ['合成光学公布经营情况。' + source])
    assert result['facts'] == []
    assert result['omitted_numeric_clauses'] == 1


@pytest.mark.parametrize('text,source', [
    ('收入3200万元', '收入32百万元'),
    ('收入3.2亿元', '收入32千万元'),
    ('收入1000万元', '收入10,000,000元'),
    ('收入32.00万元', '收入32万元'),
    ('收入32万美元', '收入0.32百万美元'),
    ('收入32港元', '收入32港元'),
    ('同比增长32％', '同比增长32%'),
    ('收入未达到32万元', '收入未达到32万元'),
    ('2025年收入32万元', '2025年收入32万元'),
    ('销售超6亿元（截至2025年7月）', '截至2025年7月，销售超6亿元'),
    ('合成光学代码2126', '合成光学代码02126.HK'),
    ('产品AX123投入研发', '产品AX123投入研发'),
])
def test_supported_full_context_units_and_product_codes_survive(tmp_path, text, source):
    result, _, _ = compile_profile(tmp_path, text, ['合成光学公布经营情况。' + source])
    assert [f['text'] for f in result['facts']] == [text]
    assert result['omitted_numeric_clauses'] == 0


def test_full_context_of_two_retained_sources_supports_composite_fact(tmp_path):
    result, _, _ = compile_profile(tmp_path, '收入32百万元；亏损7.4万元。',
        ['合成光学公布经营情况。收入32百万元。', '合成光学公布经营情况。亏损7.4万元。'],
        refs=[{'ref': f'P{i}', 'quote': '合成光学公布经营情况'} for i in [1, 2]])
    assert [f['text'] for f in result['facts']] == ['收入32百万元；亏损7.4万元。']
    assert len(result['facts'][0]['refs']) == 2 and result['omitted_numeric_clauses'] == 0


def test_dropped_fourth_reference_cannot_support_a_displayed_claim(tmp_path):
    result, _, _ = compile_profile(tmp_path, '收入32万元；员工87人。',
        ['合成光学公布经营情况。收入32万元。'] * 3 + ['合成光学公布经营情况。员工87人。'],
        refs=[{'ref': f'P{i}', 'quote': '合成光学公布经营情况'} for i in [1, 2, 3, 4]])
    assert [f['text'] for f in result['facts']] == ['收入32万元']
    assert len(result['facts'][0]['refs']) == 3 and result['omitted_numeric_clauses'] == 1


def test_commas_are_not_split_into_orphan_claims(tmp_path):
    result, _, _ = compile_profile(tmp_path, '收入32万元，亏损7.4万元。',
        ['合成光学公布经营情况。收入32万元。'])
    assert result['facts'] == [] and result['omitted_numeric_clauses'] == 1


def test_one_bad_fact_does_not_remove_a_separate_supported_fact(tmp_path):
    class Model:
        ready = True

        def json(self, *_args, **_kwargs):
            return {'facts': [{'kind': 'business', 'text': text,
                              'refs': [{'ref': 'P1', 'quote': '合成光学公布经营情况'}]}
                             for text in ['收入32万元', '亏损7.4万元']]}

    result = build_public_profile({'name': '合成光学', 'legal_name': '合成光学'}, SimpleNamespace(has_search=False), Model(),
        [{'title': '合成光学', 'url': 'https://synthetic.example/a',
          'snippet': '合成光学公布经营情况。收入32万元。'}])
    assert [f['text'] for f in result['facts']] == ['收入32万元']
    assert result['omitted_numeric_clauses'] == 1
