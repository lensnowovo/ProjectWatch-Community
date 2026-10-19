"""Dates/numbers alone do not make an otherwise unrelated material relevant."""
import socket

import pytest
from test_studio_question_materials import intake, setup

from project_watch.studio.questions import QuestionLedger


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('Synthetic question routing must not access network')
    monkeypatch.setattr(socket, 'create_connection', deny)
    monkeypatch.setattr(socket, 'getaddrinfo', deny)


@pytest.mark.parametrize('number', ['2025', '２０２５', '2025-10-13', '12345'])
def test_bare_number_or_date_cannot_be_a_topic_but_product_codes_remain(number):
    watch = {'label': '竞品动态', 'queries': [f'CAR-T 医保 {number}', 'JWCAR239 3D']}
    terms = QuestionLedger._material_terms(watch, {})
    assert number not in terms
    assert {'CAR-T', '医保', 'JWCAR239', '3D'} <= set(terms)


def test_year_only_material_does_not_reach_competitor_question(tmp_path):
    app, web, p, pid, watches = setup(tmp_path)
    watch = p.monitor.add_watchpoint(pid, '竞品的医保情况如何', '竞品动态',
        ['CAR-T 医保 谈判 2025'], '监测竞争产品', False, visibility='internal')
    before = p.questions.history(watches[0]['id'])
    intake(app, web, p, pid, '合成光电年度报告 2025。审计委员会成员简历。', run=False)
    assert p.questions.route_existing_materials(pid, [watch['id']]) == 0
    assert p.questions.new_materials(pid, watch['id'], limit=None) == []
    source = intake(app, web, p, pid, '2025 年新增 CAR-T 医保谈判信息。', run=False)
    assert p.questions.route_existing_materials(pid, [watch['id']]) == 1
    [hit] = p.questions.new_materials(pid, watch['id'], limit=None)
    assert hit['source_id'] == source
    assert '2025' not in hit['matched_terms'] and 'CAR-T' in hit['matched_terms']
    assert p.questions.history(watches[0]['id']) == before
