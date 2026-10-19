"""Bounded synthetic witnesses, not general semantic entailment."""
import pytest

from tests.test_person_fact_projection import context, project, row


@pytest.mark.parametrize('actor', ['甲', '乙', '丙'])
def test_explicit_history_list_keeps_each_actor(actor):
    quote = '合成研究公司进入了职业经理人的管理阶段，已经历了甲、乙和丙三位管理者'
    out = project(context(quote), [row(label=actor, value='管理者', refs=[{'ref': 'P1', 'quote': quote}])])
    assert len(out['facts']) == 1
    assert '列于管理者名单' in out['facts'][0]['text']


@pytest.mark.parametrize('quote', [
    '合成研究公司已经历了甲乙、丙三位管理者',
    '另一合成公司已经历了甲、乙和丙三位管理者',
    '合成研究公司未来预计经历甲、乙和丙三位管理者',
    '合成研究公司未经历甲、乙和丙三位管理者',
    '合成研究公司已经历了乙、丙和丁三位管理者，甲为顾问',
])
def test_history_list_does_not_borrow_actor_or_company(quote):
    out = project(context(quote), [row(value='管理者', refs=[{'ref': 'P1', 'quote': quote}])])
    assert out['facts'] == []


@pytest.mark.parametrize('quote', [
    '合成研究公司公告新董事长是服务合成研究公司多年、技术出身的甲',
    '合成研究公司公告新董事长是技术出身的甲',
])
def test_described_office_is_bound_to_the_named_actor(quote):
    out = project(context(quote), [row(relation='role', value='新董事长', refs=[{'ref': 'P1', 'quote': quote}])])
    assert len(out['facts']) == 1


@pytest.mark.parametrize('quote', [
    '合成研究公司公告新董事长是技术出身的乙，甲为顾问',
    '另一合成公司公告新董事长是技术出身的甲',
    '合成研究公司公告新董事长并非技术出身的甲',
    '合成研究公司计划新董事长是技术出身的甲',
    '合成研究公司公告新董事长是技术出身的甲乙',
])
def test_described_office_rejects_wrong_actor_scope_negation_and_future(quote):
    out = project(context(quote), [row(relation='role', value='新董事长', refs=[{'ref': 'P1', 'quote': quote}])])
    assert out['facts'] == []


def test_founder_is_identity_not_an_office_to_hold():
    quote = '创始人甲卸任董事长之后，乙为董事长'
    out = project(context(quote), [row(relation='role', value='创始人', refs=[{'ref': 'P1', 'quote': quote}])])
    assert '甲为创始人' in out['facts'][0]['text']
    assert '担任创始人' not in out['facts'][0]['text']


def test_departure_keeps_the_explicit_office_in_display():
    quote = '合成研究公司甲卸任董事长之后，乙为董事长'
    out = project(context(quote), [row(relation='departure', value='董事长', refs=[{'ref': 'P1', 'quote': quote}])])
    assert '董事长' in out['facts'][0]['text']


def test_departure_does_not_add_an_uncited_office():
    quote = '合成研究公司甲离职'
    out = project(context(quote), [row(relation='departure', value='董事长', refs=[{'ref': 'P1', 'quote': quote}])])
    assert out['facts'] == []


@pytest.mark.parametrize('quote', [
    '合成研究公司甲离职，乙为董事长',
    '合成研究公司乙卸任董事长，甲为顾问',
    '合成研究公司甲卸任董事长助理',
])
def test_departure_office_cannot_be_borrowed_from_neighbour(quote):
    out = project(context(quote), [row(relation='departure', value='董事长', refs=[{'ref': 'P1', 'quote': quote}])])
    assert out['facts'] == []
