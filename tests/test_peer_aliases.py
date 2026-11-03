from types import SimpleNamespace

from project_watch.studio import collect_job
from project_watch.studio.llm import ModelError
from project_watch.studio.peer_names import (
    headline_leads,
    peer_codes,
    peer_search_query,
    peers_for,
    usable_alias,
)

YL = '宜联生物（B7-H3 ADC YL201，授权罗氏）'
HS = '翰森制药（B7-H3 ADC HS-20093，与GSK合作）'
DS = '第一三共/默沙东（B7-H3 ADC ifinatamab deruxtecan，I-DXd）'
RC = '荣昌生物（HER2 ADC维迪西妥单抗/RC48，2021年国内首个上市）'


def test_codes_come_from_the_note_not_targets_or_partners():
    assert peer_codes(YL) == ['YL201']
    assert peer_codes(HS) == ['HS-20093', 'HS20093']
    assert peer_codes(DS) == ['I-DXd']
    assert peer_codes(RC) == ['RC48']
    assert peer_codes('明慧医药（B7-H3 ADC MHB-088C）') == ['MHB-088C', 'MHB088C']
    assert peer_codes('恒瑞医药（HER2 ADC瑞康曲妥珠单抗/SHR-A1811）') == ['SHR-A1811', 'SHRA1811']
    assert peer_codes('宜联生物') == []


def test_usable_alias_rejects_category_words_and_descriptions():
    assert usable_alias('Hansoh') and usable_alias('翰森製藥') and usable_alias('HS-20093')
    assert not usable_alias('ADC') and not usable_alias('GSK') and not usable_alias('未知')
    assert not usable_alias('宜联生物（B7-H3）') and not usable_alias('x' * 41) and not usable_alias(None)


def test_search_query_is_short_name_plus_own_code():
    assert peer_search_query({'name': YL}) == '宜联生物 YL201'
    assert peer_search_query({'name': DS}) == '第一三共 I-DXd'
    assert peer_search_query({'name': '某公司', 'aliases': ['ABC-123']}) == '某公司 ABC-123'
    assert peer_search_query({'name': YL, 'query': '宜联生物 鼻咽癌'}) == '宜联生物 鼻咽癌'


def test_latin_names_lead_case_insensitively():
    assert headline_leads('Ifinatamab Deruxtecan Granted Priority Review', ['ifinatamab deruxtecan'])
    assert headline_leads('HS-20093: NMPA Accepts SCLC NDA', ['HS-20093'])
    assert not headline_leads('HS-200931 trial starts', ['HS-20093'])


def test_peers_carry_codes_and_recorded_names_but_not_shared_or_own_ones():
    plan = {'competitors': [
        {'name': YL, 'aliases': ['MediLink', 'YL201', 'B7-H3 ADC']},
        {'name': HS, 'aliases': ['Hansoh', '翰森製藥', 'B7-H3 ADC']},
        {'name': '另一家（DB-1311）', 'aliases': ['映恩生物']},
    ]}
    peers = {p['name']: p for p in peers_for({'name': '映恩生物'}, plan, {})}
    assert peers[YL]['aliases'] == ['YL201', 'MediLink']
    assert peers[HS]['aliases'] == ['HS-20093', 'HS20093', 'Hansoh', '翰森製藥']
    assert peers['另一家（DB-1311）']['aliases'] == ['DB-1311', 'DB1311']


class _Store:
    def __init__(self, plan):
        self.plan = plan

    def get_plan(self, pid):
        return self.plan

    def put_plan(self, pid, plan):
        self.plan = plan


def _pipeline(plan, answer):
    calls = []

    def name_competitors(llm, project, names):
        calls.append(names)
        if isinstance(answer, Exception):
            raise answer
        return answer

    store = _Store(plan)
    return SimpleNamespace(store=store, tools=lambda: (None, None, SimpleNamespace(ready=True), None),
                           name_competitors=name_competitors), calls


def test_peer_names_are_recorded_once_and_filtered():
    plan = {'competitors': [{'name': YL, 'reason': 'r'}, {'name': HS, 'query': '翰森 B7-H3'}]}
    pipeline, calls = _pipeline(plan, {
        YL: {'short': '宜联生物', 'aliases': ['MediLink Therapeutics', 'ADC', '宜联生物（YL201）'], 'query': '宜联生物 YL201 新闻'},
        HS: {'short': '翰森', 'aliases': ['Hansoh'], 'query': '翰森制药 HS-20093'},
    })
    out = collect_job._current_peer_names(pipeline, 'p', {'name': '映恩生物'}, plan)
    yl, hs = out['competitors']
    assert yl['aliases'] == ['YL201', '宜联生物', 'MediLink Therapeutics', 'MediLink'] and yl['query'] == '宜联生物 YL201'
    assert hs['aliases'] == ['HS-20093', 'HS20093', '翰森', 'Hansoh'] and hs['query'] == '翰森 B7-H3'  # an existing query stays
    assert out['peer_names_version'] == collect_job.PEER_NAMES_VERSION and pipeline.store.plan is out
    assert collect_job._current_peer_names(pipeline, 'p', {}, out) is out and len(calls) == 1


def test_a_failed_naming_request_keeps_the_plan_and_is_not_resent_in_the_job():
    plan = {'competitors': [{'name': YL}]}
    pipeline, calls = _pipeline(plan, ModelError('unclear'))
    assert collect_job._current_peer_names(pipeline, 'p', {}, plan) is plan
    assert 'peer_names_version' not in pipeline.store.plan and len(calls) == 1


def test_an_empty_answer_changes_nothing_and_is_asked_again_later():
    plan = {'competitors': [{'name': YL}]}
    pipeline, _ = _pipeline(plan, {})
    assert collect_job._current_peer_names(pipeline, 'p', {}, plan) is plan
    assert 'peer_names_version' not in pipeline.store.plan


def test_naming_answers_are_matched_by_echoed_number():
    from project_watch.studio.pipeline import Pipeline

    class _LLM:
        def json(self, system, context, max_tokens):
            assert '1. ' + YL in context and '2. ' + HS in context
            return {'peers': [{'id': 'C2', 'short': '翰森', 'aliases': ['Hansoh', 3], 'query': '翰森 HS-20093'},
                              {'id': 'C9', 'short': 'x'}, {'name': YL, 'short': '宜联'}]}

    out = Pipeline.name_competitors(None, _LLM(), {'name': '映恩生物'}, [YL, HS])
    assert out == {HS: {'short': '翰森', 'aliases': ['Hansoh'], 'query': '翰森 HS-20093'}}
