"""Synthetic projection controls, never a general semantic-accuracy benchmark."""
from copy import deepcopy
from datetime import date
from types import SimpleNamespace

import pytest

from scripts.person_support_context import freeze_context


def context(body='合成研究公司列出的管理人员包括甲、乙。', other=None):
    sources = [{'ref': 'P1', 'title': '合成研究公司公开资料', 'body': body,
                'published_at': '2025-04-01', 'visibility': 'public', 'commercial': False}]
    if other:
        sources.append({'ref': 'P2', 'title': '合成研究公司另一来源', 'body': other,
                        'published_at': '2025-04-01', 'visibility': 'public', 'commercial': False})
    return freeze_context('合成研究公司', sources, today=date(2025, 4, 2))


def row(**changes):
    return {'subject': '合成研究公司', 'kind': 'person', 'label': '甲', 'relation': 'listed',
            'value': '管理人员', 'polarity': 'positive', 'time_scope': 'as_reported',
            'date': '', 'reported_on': '2025-04-01', 'text': '甲是公司首位管理者并任CEO。',
            'refs': [{'ref': 'P1', 'quote': '合成研究公司列出的管理人员包括甲、乙'}], **changes}


def project(ctx, rows):
    # Late import makes the red result explicit in each control, not collection only.
    from scripts.person_fact_projection import project_person_facts
    return project_person_facts(ctx, {'facts': rows})


def test_free_prose_cannot_add_first_role_or_ceo_to_a_listed_person():
    raw = {'facts': [row()]}
    before = deepcopy(raw)
    output = project(context(), raw['facts'])
    assert len(output['facts']) == 1
    assert output['facts'][0]['text'] == '据2025-04-01报道，甲列于管理人员名单。'
    assert '首位' not in output['facts'][0]['text'] and 'CEO' not in output['facts'][0]['text']
    assert output['raw_facts'] == raw['facts'] and raw == before


@pytest.mark.parametrize('polarity,quote,expected', [
    ('positive', '合成研究公司正式任命甲为首任总经理', '甲为首任总经理'),
    ('negative', '合成研究公司明确说明甲并不是首任总经理', '甲不是首任总经理'),
])
def test_explicit_first_role_and_negation_remain(polarity, quote, expected):
    out = project(context(quote + '。'), [row(relation='first_role', value='总经理',
        polarity=polarity, refs=[{'ref': 'P1', 'quote': quote}])])
    assert expected in out['facts'][0]['text']


@pytest.mark.parametrize('relation,value,body,quote', [
    ('first_role', '总经理', '合成研究公司管理人员有甲，甲任总经理。', '合成研究公司管理人员有甲，甲任总经理'),
    ('role', 'CEO', '合成研究公司聘请甲为外部顾问。同行公司由乙担任CEO。', '合成研究公司聘请甲为外部顾问'),
    ('role', 'CEO', '合成研究公司聘请甲为外部顾问。同行公司由乙担任CEO。', '同行公司由乙担任CEO'),
    ('appointment', '总经理', '合成研究公司报道甲为总经理。', '合成研究公司报道甲为总经理'),
    ('departure', '合成研究公司', '合成研究公司列出的管理人员包括甲。', '合成研究公司列出的管理人员包括甲'),
])
def test_relationship_or_value_without_final_quote_anchor_is_not_projected(relation, value, body, quote):
    output = project(context(body), [row(relation=relation, value=value, refs=[{'ref': 'P1', 'quote': quote}])])
    assert output['facts'] == [] and output['status'] == 'rejected'
    assert output['rejections'][0]['code'].startswith('projection_')


def test_person_and_role_cannot_be_joined_from_separate_quotes():
    ctx = context('合成研究公司管理人员包括甲。', '合成研究公司总经理暂缺。')
    output = project(ctx, [row(relation='role', value='总经理', refs=[
        {'ref': 'P1', 'quote': '合成研究公司管理人员包括甲'},
        {'ref': 'P2', 'quote': '合成研究公司总经理暂缺'}])])
    assert output['facts'] == []


def test_report_date_is_not_occurrence_date_and_other_body_date_cannot_be_borrowed():
    body = '合成研究公司任命甲为总经理。公司于2024年新建厂房。'
    output = project(context(body), [row(relation='appointment', value='总经理', date='2024',
        refs=[{'ref': 'P1', 'quote': '合成研究公司任命甲为总经理'}])])
    assert output['facts'] == [] and output['rejections'][0]['code'] == 'projection_occurrence'


def test_explicit_appointment_preserves_witness_date_and_report_date_separately():
    quote = '2024年5月10日，合成研究公司正式任命甲为总经理'
    output = project(context(quote + '。'), [row(relation='appointment', value='总经理',
        date='2024-05-10', refs=[{'ref': 'P1', 'quote': quote}])])
    fact = output['facts'][0]
    assert fact['date'] == '2024-05-10' and fact['reported_on'] == '2025-04-01'
    assert '2024年5月10日被任命' in fact['text'] and '据2025-04-01' in fact['text']


def test_one_bad_row_retains_sibling_with_original_identifier():
    out = project(context(), [row(relation='first_role'), row(label='乙')])
    assert out['status'] == 'partial' and out['facts'][0]['id'] == 'F02'
    assert out['rejections'][0]['id'] == 'F01'


def test_empty_extraction_does_not_create_a_person_from_the_source():
    out = project(context(), [])
    assert out['facts'] == [] and out['coverage_status'] == 'needs_extraction'


def test_unknown_report_day_stays_unknown_not_today():
    out = project(context(), [row(reported_on='')])
    assert out['facts'][0]['text'].startswith('日期不明的来源列示，')


def test_predecessor_is_an_anchored_name_not_free_form_prose():
    quote = '合成研究公司甲接任乙的总经理工作'
    valid = project(context(quote + '。'), [row(relation='precedence', value='乙',
        refs=[{'ref': 'P1', 'quote': quote}])])
    assert '甲接任乙的相关工作' in valid['facts'][0]['text']
    invalid = project(context(quote + '。'), [row(relation='precedence', value='乙担任的所有重要职务',
        refs=[{'ref': 'P1', 'quote': quote}])])
    assert invalid['facts'] == []


def test_quote_copresence_cannot_transfer_another_persons_role():
    # Both anchors occur in one quote, but field assignment is semantically wrong.
    quote = '合成研究公司甲为顾问，乙为总经理'
    out = project(context(quote + '。'), [row(relation='role', value='总经理',
        refs=[{'ref': 'P1', 'quote': quote}])])
    assert out['facts'] == [] and out['semantic_verification'] == 'not_proven'


@pytest.mark.parametrize('quote,actor,value,polarity,relation,retained', [
    ('合成研究公司甲不是总经理，乙为总经理', '乙', '总经理', 'positive', 'role', True),
    ('合成研究公司甲为顾问，乙不是总经理', '甲', '总经理', 'negative', 'role', False),
    ('合成研究公司甲为顾问，乙从未担任总经理', '乙', '总经理', 'negative', 'role', True),
    ('合成研究公司甲不是首任总经理，乙为总经理', '甲', '总经理', 'negative', 'first_role', True),
    ('合成研究公司甲不是首任总经理，乙为总经理', '乙', '总经理', 'negative', 'first_role', False),
    ('合成研究公司甲为顾问，乙是首任总经理', '甲', '总经理', 'positive', 'first_role', False),
    ('甲正式出任合成研究公司的董事长兼CEO', '甲', '董事长', 'positive', 'role', True),
    ('合成研究公司正式任命甲为首任总经理', '甲', '总经理', 'positive', 'first_role', True),
    ('合成研究公司 A is an adviser; B is CEO', 'A', 'CEO', 'positive', 'role', False),
    ('合成研究公司 A is not CEO; B is CEO', 'B', 'CEO', 'positive', 'role', True),
    ('合成研究公司甲为总经理助理', '甲', '总经理', 'positive', 'role', False),
    ('合成研究公司甲乙为总经理', '甲', '总经理', 'positive', 'role', False),
    ('2024年5月10日正式任命甲为公司首任总经理', '甲', '首任总经理', 'positive', 'first_role', True),
    ('甲为本公司外部顾问', '甲', '外部顾问', 'positive', 'role', True),
    ('乙才是首任总经理', '乙', '首任总经理', 'positive', 'first_role', True),
    ('甲曾于2022年任产品主管', '甲', '产品主管', 'positive', 'role', True),
    ('现任总经理为乙', '乙', '总经理', 'positive', 'role', True),
    ('现任副总经理为乙', '乙', '总经理', 'positive', 'role', False),
    ('现任总经理为甲乙', '甲', '总经理', 'positive', 'role', False),
    ('甲为首任', '甲', '首任', 'positive', 'first_role', False),
    ('并非甲担任CEO，而是乙担任CEO', '甲', 'CEO', 'positive', 'role', False),
    ('并非甲担任CEO，而是乙担任CEO', '乙', 'CEO', 'positive', 'role', True),
])
def test_role_witness_binds_actor_scope_and_polarity(quote, actor, value, polarity, relation, retained):
    original = row(label=actor, value=value, polarity=polarity, relation=relation,
                   refs=[{'ref': 'P1', 'quote': quote}])
    before = deepcopy(original)
    out = project(context(quote + '。'), [original])
    assert bool(out['facts']) is retained
    assert original == before and out['raw_facts'] == [before]
    assert out['semantic_verification'] == 'not_proven'


def test_historical_founder_does_not_become_a_former_founder():
    quote = '创始人甲卸任董事长之后，乙为董事长'
    out = project(context(quote + '。'), [row(relation='role', value='创始人', time_scope='historical',
                   refs=[{'ref': 'P1', 'quote': quote}])])
    assert len(out['facts']) == 1
    assert '历史资料记载，甲为创始人' in out['facts'][0]['text']
    assert '曾任创始人' not in out['facts'][0]['text']


def test_role_ownership_rejection_keeps_correct_sibling_and_original_ref():
    quote = '合成研究公司甲为顾问，乙为总经理'
    refs = [{'ref': 'P1', 'quote': quote}]
    out = project(context(quote + '。'), [row(relation='role', value='总经理', refs=refs),
                  row(label='乙', relation='role', value='总经理', refs=refs)])
    assert out['status'] == 'partial'
    assert out['facts'][0]['id'] == 'F02' and out['facts'][0]['refs'] == refs
    assert out['rejections'][0]['id'] == 'F01'


@pytest.mark.parametrize('changes', [
    {'subject': '异公司'}, {'refs': []}, {'refs': [{'ref': 'P2', 'quote': '不存在的逐字引用'}]},
])
def test_previous_contract_rejections_remain(changes):
    out = project(context(), [row(**changes)])
    assert out['facts'] == [] and out['rejections']


@pytest.mark.parametrize('relation,value,quote,polarity', [
    ('role', 'CEO', '合成研究公司甲从未担任CEO', 'positive'),
    ('role', 'CEO', '合成研究公司甲担任CEO', 'negative'),
    ('first_role', 'CEO', '合成研究公司 says A is not the first CEO', 'positive'),
])
def test_explicit_local_polarity_cannot_be_reversed(relation, value, quote, polarity):
    actor = 'A' if ' A ' in quote else '甲'
    out = project(context(quote + '。'), [row(label=actor, relation=relation, value=value,
        polarity=polarity, refs=[{'ref': 'P1', 'quote': quote}])])
    assert out['facts'] == []


def test_explicit_negative_role_is_preserved():
    quote = '合成研究公司甲从未担任CEO'
    out = project(context(quote + '。'), [row(relation='role', value='CEO', polarity='negative',
        refs=[{'ref': 'P1', 'quote': quote}])])
    assert '甲未担任CEO' in out['facts'][0]['text']


@pytest.mark.parametrize('quote,value', [
    ('合成研究公司甲并不是首任总经理', '总经理'),
    ('合成研究公司甲并非唯一总经理', '总经理'),
    ('合成研究公司 A is not the first CEO', 'CEO'),
])
def test_narrow_role_negation_cannot_be_widened_to_never_holding_the_role(quote, value):
    actor = 'A' if ' A ' in quote else '甲'
    out = project(context(quote + '。'), [row(label=actor, relation='role', value=value,
        polarity='negative', refs=[{'ref': 'P1', 'quote': quote}])])
    assert out['facts'] == []


def test_first_role_keeps_explicit_occurrence_time_in_the_display():
    quote = '2024年5月10日，合成研究公司正式任命甲为首任总经理'
    out = project(context(quote + '。'), [row(relation='first_role', value='总经理', date='2024-05-10',
        refs=[{'ref': 'P1', 'quote': quote}])])
    assert '2024年5月10日' in out['facts'][0]['text']
    assert out['facts'][0]['date'] == '2024-05-10'


def test_explicit_taking_office_is_kept_without_inventing_a_formal_appointment():
    quote = '合成研究公司甲正式出任总经理'
    out = project(context(quote + '。'), [row(relation='appointment', value='总经理',
        refs=[{'ref': 'P1', 'quote': quote}])])
    assert '甲出任总经理' in out['facts'][0]['text']
    assert '被任命' not in out['facts'][0]['text']


def test_explicit_departing_office_is_kept():
    quote = '合成研究公司甲在董事长岗位工作半年后离任'
    out = project(context(quote + '。'), [row(relation='departure', value='董事长',
        refs=[{'ref': 'P1', 'quote': quote}])])
    assert '甲已离任' in out['facts'][0]['text']


@pytest.mark.parametrize('body,relation,value,occurred', [
    ('合成研究公司列出的管理人员包括甲、乙。', 'listed', '管理人员', ''),
    ('2024年5月10日，合成研究公司正式任命甲为总经理。', 'appointment', '总经理', '2024-05-10'),
    ('2024/05/10，合成研究公司正式任命甲为总经理。', 'appointment', '总经理', '2024-05-10'),
    ('2024年5月10日，合成研究公司正式任命甲为首任总经理。', 'first_role', '总经理', '2024-05'),
])
def test_projection_can_pass_native_compiler_without_losing_supported_time(body, relation, value, occurred):
    from project_watch.studio.public_profile import build_public_profile
    quote = body.rstrip('。')
    out = project(context(body), [row(relation=relation, value=value, date=occurred,
        refs=[{'ref': 'P1', 'quote': quote}])])
    class Model:
        ready = True
        def json(self, *_args, **_kwargs):
            return {'facts': out['facts']}
    compiled = build_public_profile({'name': '合成研究公司', 'legal_name': '合成研究公司'},
        SimpleNamespace(has_search=False), Model(),
        [{'url': 'https://synthetic.example/company', 'title': '合成研究公司公开资料',
          'body': body, 'published_at': '2025-04-01'}])
    assert len(compiled['facts']) == 1
    assert compiled['facts'][0]['text'] == out['facts'][0]['text']
    assert compiled['facts'][0]['date'] == occurred
    if occurred == '2024-05':
        assert '2024年5月：' in compiled['facts'][0]['text']
        assert '10日' not in compiled['facts'][0]['text']
