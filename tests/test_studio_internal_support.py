"""Synthetic support and material chronology controls, never live services."""
import socket

import pytest

from project_watch.studio.pipeline import Pipeline

REF = {'source_id': 'synthetic', 'source_name': 'synthetic.txt', 'evidence_version_id': 'synthetic-version', 'part_index': 0}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('Internal support regressions cannot make live calls')
    monkeypatch.setattr(socket, 'create_connection', deny)
    monkeypatch.setattr(socket, 'getaddrinfo', deny)


@pytest.mark.parametrize(('claim', 'sentence'), [
    ('产品毛利率为62.4%。', '产品毛利率为62.4%。'),
    ('银行结余及现金为3.92亿元。', '银行结余及现金为3.92亿元。'),
    ('商保覆盖105个城市。', '商保覆盖105个城市。'),
    ('公司研发光学模组。', '公司主要研发光学模组。'),
])
def test_reference_binds_relevant_sentence_not_page_lead(claim, sentence):
    text = '公司持续坚持创新理念。' + sentence + '报告欢迎读者了解更多。'
    cite = Pipeline._internal_cite('F1', REF, text, claim=claim)
    assert cite['quote'] == sentence
    assert text[cite['start']:cite['end']] == sentence


@pytest.mark.parametrize('claim', ['毛利率为72.4%。', '收入为3.92万元。', '现金余额为8亿元。'])
def test_wrong_number_or_unit_cannot_borrow_page_lead(claim):
    text = '合成公司坚持创新。收入为3.92亿元。毛利率为62.4%。'
    cite = Pipeline._internal_cite('F1', REF, text, claim=claim)
    assert not cite.get('quote')
    assert cite['support_state'] == 'unmatched'


def test_no_context_does_not_invent_support_from_first_sentence():
    cite = Pipeline._internal_cite('F1', REF, '这是一份合成材料。后续有很多事实。')
    assert not cite.get('quote') and cite['support_state'] == 'unmatched'


@pytest.mark.parametrize('claim', ['合成产品通过年度目录形式审查。', '合成公司完成海外工厂并购。'])
def test_change_cannot_borrow_an_unrelated_point_from_same_page(tmp_path, claim):
    from test_studio_evidence_versions import sample
    from test_studio_material_quality import SUPPORT

    app, _ = SUPPORT['make_app'](tmp_path)
    p = app.state.studio_pipeline
    pid = p.repository.create_project('合成变化引用公司').id
    decoy = '合成公司已完成工厂设备备案。'
    supported = '合成产品通过年度目录形式审查。'
    version = p.evidence.save(sample(pid, decoy + supported))
    refs = {'F2': REF | {'source_id': 'synthetic-new', 'evidence_version_id': version['id']}}

    class Model:
        def json(self, *args, **kwargs):
            return {'changes': [{'kind': '资料补充', 'text': claim, 'refs': ['F2']}]}

    p._record_changes(pid, Model(), refs, {'synthetic-new'}, {'summary': '合成旧档案'},
                      [{'text': decoy, 'quote': decoy, 'ref': 'F2'},
                       {'text': supported, 'quote': supported, 'ref': 'F2'}])
    [row] = p.store.doc_changes(pid)[0]['texts']
    assert row['text'] == claim
    assert not any(ref.get('quote') == decoy for ref in row['refs'])
    if claim == supported:
        assert row['refs'][0]['quote'] == supported
        assert row['evidence_support'] == 'located_quote'
    else:
        assert row['evidence_support'] == 'source_only'


def test_invalid_explicit_quote_is_not_silently_replaced():
    cite = Pipeline._internal_cite('F1', REF, '现金为3亿元。', quote='现金为30亿元。')
    assert not cite.get('quote') and cite['support_state'] == 'unmatched'


def test_explicit_quote_keeps_exact_original_offsets():
    text = '前言。合成公司的毛利率为62.4%。后记。'
    cite = Pipeline._internal_cite('F1', REF, text, quote='合成公司的毛利率为62.4%。')
    assert cite['quote'] == '合成公司的毛利率为62.4%。'
    assert text[cite['start']:cite['end']] == cite['quote']


def test_long_nonleading_support_window_contains_the_anchor():
    text = '合成背景说明' * 200 + '产品毛利率为62.4%。'
    cite = Pipeline._internal_cite('F1', REF, text, claim='产品毛利率为62.4%。')
    assert '62.4%' in cite['quote'] and len(cite['quote']) <= 300
    assert text[cite['start']:cite['end']] == cite['quote']


def test_code_substring_is_not_a_source_match():
    cite = Pipeline._internal_cite('F1', REF, 'MiniALPHA用于连接。', ['ALPHA'])
    assert not cite.get('quote')


@pytest.mark.parametrize('text', [
    '截至2025年12月31日止年度的合成年度报告。',
    '合成公司截至2025-12-31的报告。',
    'For the year ended 31 December 2025, synthetic company results.',
])
def test_material_cutoff_is_source_located_not_processing_time(text):
    cutoff = Pipeline._material_cutoff({'F1': REF}, {'F1': text})
    assert cutoff['date'] == '2025-12-31'
    cite = cutoff['ref']
    assert text[cite['start']:cite['end']] == cite['quote']


@pytest.mark.parametrize('text', [
    '预计截至2025年12月31日完成研发。',
    '截至2035年12月31日的预测。',
    '公司于2006年成立。',
])
def test_unknown_future_and_planned_cutoffs_are_not_material_baselines(text):
    assert Pipeline._material_cutoff({'F1': REF}, {'F1': text}) is None


def test_explicit_report_period_beats_a_later_incidental_shareholding_date():
    text = '截至2025年12月31日止年度报告。截至2026年3月20日董事持股为零。'
    cutoff = Pipeline._material_cutoff({'F1': REF}, {'F1': text})
    assert cutoff['date'] == '2025-12-31' and cutoff['basis'] == 'report_period'


def test_real_prior_material_period_not_late_processing_clock_gates_progress():
    quote = '2026年6月26日，合成公司完成产品验收。'
    prior = {'updated_at': '2026-10-13T10:00:00Z', 'material_cutoff': {'date': '2025-12-31'}}
    assert Pipeline._dated_progress({'event_date': '2026-06-26'}, prior, quote)
    assert not Pipeline._dated_progress({'event_date': '2026-06-26'}, {'updated_at': '2025-12-31'}, quote)


def test_traditional_completed_words_are_not_rejected_as_unknown_actions():
    prior = {'material_cutoff': {'date': '2025-12-31'}}
    assert Pipeline._dated_progress({'event_date': '2026-06-26'}, prior, '2026年6月26日，合成公司獲批產品上市。')
    assert not Pipeline._dated_progress({'event_date': '2026-06-26'}, prior, '2026年6月26日，合成公司計劃獲批產品上市。')


@pytest.mark.parametrize(('before', 'after', 'equal'), [
    ('成人经过二线或以上系统性治疗后。', '成人三线及以上治疗。', True),
    ('成人經過二線或以上系統性治療後。', '成人三線及以上治療。', True),
    ('成人经过二线或以上系统性治疗后。', '儿童三线及以上治疗。', False),
    ('成人经过二线或以上系统性治疗后。', '成人二线及以上治疗。', False),
    ('成人经过二线系统性治疗后。', '成人三线治疗。', True),
    ('成人经过二线或以上系统性治疗后。', '成人三线治疗。', False),
    ('合成产品X用于成人三线治疗。', '合成产品Y用于成人三线治疗。', False),
])
def test_only_controlled_line_expressions_are_normalized(before, after, equal):
    assert Pipeline._equivalent_internal_statement(before, after) is equal


def change(tmp_path, old, new, proposal):
    from test_studio_evidence_versions import sample
    from test_studio_material_quality import SUPPORT
    app, _ = SUPPORT['make_app'](tmp_path)
    p = app.state.studio_pipeline
    pid = p.repository.create_project('合成变化公司').id
    refs = {}
    for marker, text, sid in [('F1', old, 'synthetic-old'), ('F2', new, 'synthetic-new')]:
        version = p.evidence.save(sample(pid, text))
        refs[marker] = REF | {'source_id': sid, 'evidence_version_id': version['id']}
    class Model:
        def json(self, system, user, **kwargs):
            assert '整理日期' not in user
            return {'changes': [proposal]}
    p._record_changes(pid, Model(), refs, {'synthetic-new'},
                      {'summary': '合成旧资料', 'updated_at': '2026-10-13T10:00:00Z'},
                      [{'text': new, 'ref': 'F2'}])
    return p, pid, p.store.doc_changes(pid)[0]['texts'][0]


@pytest.mark.parametrize(('after', 'expected'), [
    ('成人三线及以上治疗。', '资料补充'),
    ('儿童三线及以上治疗。', '不一致'),
])
def test_bilateral_located_line_comparison_preserves_other_qualifiers(tmp_path, after, expected):
    before = '成人经过二线或以上系统性治疗后。'
    _, _, row = change(tmp_path, '截至2025年12月31日止年度。' + before, after,
        {'kind': '不一致', 'text': '治疗条件表述出现变化', 'refs': ['F2'],
         'before_ref': 'F1', 'before_quote': before, 'after_ref': 'F2', 'after_quote': after})
    assert row['kind'] == expected
    assert row['comparison_state'] == ('equivalent_wording' if expected == '资料补充' else 'located_pair')
    assert [r['quote'] for r in row['refs']] == ([after] if expected == '资料补充' else [before, after])


@pytest.mark.parametrize('invalid', ['missing', 'wrong_quote', 'wrong_side'])
def test_one_sided_or_invented_comparison_is_not_a_supported_conflict(tmp_path, invalid):
    proposal = {'kind': '不一致', 'text': '治疗条件有冲突', 'refs': ['F2'],
                'before_ref': 'F1', 'before_quote': '成人三线治疗。',
                'after_ref': 'F2', 'after_quote': '儿童三线治疗。'}
    if invalid == 'missing':
        proposal.pop('before_quote')
    elif invalid == 'wrong_quote':
        proposal['before_quote'] = '虚构的成人一线治疗。'
    else:
        proposal['before_ref'] = 'F2'
    _, _, row = change(tmp_path, '截至2025年12月31日。成人三线治疗。', '儿童三线治疗。', proposal)
    assert row['kind'] == '资料补充' and row['comparison_state'] == 'missing_located_pair'


@pytest.mark.parametrize(('old_action', 'kind'), [
    ('2026年6月26日，合成公司完成产品验收。', '资料补充'),
    ('计划2026年6月26日，合成公司完成产品验收。', '新进展'),
])
def test_repeated_done_event_is_not_new_but_old_plan_can_be_completed(tmp_path, old_action, kind):
    quote = '2026年6月26日，合成公司完成产品验收。'
    _, _, row = change(tmp_path, '截至2025年12月31日。' + old_action, quote,
                        {'kind': '新进展', 'text': '完成验收', 'refs': ['F2'],
                         'event_date': '2026-06-26', 'event_ref': 'F2', 'event_quote': quote})
    assert row['kind'] == kind


@pytest.mark.parametrize('reply', [None, {}, {'changes': None}, {'changes': {}}, {'changes': ['not a row']}])
def test_malformed_change_reply_is_not_an_empty_success(tmp_path, reply):
    from test_studio_evidence_versions import sample
    from test_studio_material_quality import SUPPORT

    from project_watch.studio.llm import ModelError
    app, _ = SUPPORT['make_app'](tmp_path)
    p = app.state.studio_pipeline
    pid = p.repository.create_project('合成形状检查公司').id
    version = p.evidence.save(sample(pid, '合成资料中提到交付。'))
    refs = {'F1': REF | {'evidence_version_id': version['id']}}
    class Model:
        def json(self, *args, **kwargs):
            return reply
    with pytest.raises(ModelError) as exc:
        p._record_changes(pid, Model(), refs, {'synthetic'}, {'summary': '此前的认识'},
                          [{'text': '合成資料中提到交付。', 'ref': 'F1'}])
    assert exc.value.code == 'model_output_invalid_shape'
    assert p.store.doc_changes(pid) == []


def test_normal_t0_t1_intake_uses_real_period_and_preserves_human_agent_history(tmp_path):
    import re

    from test_studio_question_materials import intake, setup

    from project_watch.studio.monitoring_contracts import LocatedRef
    app, web, p, pid, watches = setup(tmp_path)
    old = '截至2025年12月31日止年度。合成光电计划2026年6月26日完成产品验收。'
    new_quote = '2026年6月26日，合成光电完成产品验收。'
    new = '截至2026年6月30日止六个月。' + new_quote
    original = app.state.synthetic_transport.model_result
    calls = []
    def model(system, user):
        if '材料按片段给出' in system:
            points = [{'section': '经营进展', 'text': text.strip(), 'quote': text.strip(), 'ref': marker}
                      for marker, text in re.findall(r'\[(F\d+)\]\s*\([^\n]*?\) ([^\n]+)', user)]
            return {'points': points, 'metrics': [], 'milestones': [], 'financing': [], 'risks': []}
        if '公司一页纸档案' in system:
            marker = 'F2' if 'F2' in user else 'F1'
            text = new_quote if marker == 'F2' else '合成光电计划完成产品验收。'
            return {'summary': text, 'sections': [{'title': '经营进展', 'points': [{'text': text, 'refs': [marker]}]}]}
        if '新一批内部资料' in system:
            calls.append(user)
            return {'changes': [{'kind': '新进展', 'text': '产品验收完成', 'refs': ['F2'],
                    'event_ref': 'F2', 'event_quote': new_quote, 'event_date': '2026-06-26'}]}
        return original(system, user)
    app.state.synthetic_transport.model_result = model
    history = p.questions.history(watches[0]['id'])
    intake(app, web, p, pid, old)
    assert p.store.get_profile(pid)['material_cutoff']['date'] == '2025-12-31'
    intake(app, web, p, pid, new)
    profile = p.store.get_profile(pid)
    assert profile['material_cutoff']['date'] == '2026-06-30'
    assert len(calls) == 1 and '材料截至日期：2025-12-31' in calls[0]
    [row] = p.store.doc_changes(pid)[0]['texts']
    assert row['kind'] == '新进展' and row['text'] == new_quote
    cite = row['refs'][0]
    p.evidence.resolve(pid, LocatedRef(**{k: cite[k] for k in LocatedRef.model_fields}))
    p.evidence.resolve(pid, LocatedRef(**{k: profile['material_cutoff']['ref'][k] for k in LocatedRef.model_fields}))
    assert p.questions.history(watches[0]['id']) == history
    assert p.questions.history(watches[1]['id']) == []


def test_profile_and_doc_change_publication_roll_back_together(tmp_path):
    from test_studio_evidence_versions import sample
    from test_studio_material_quality import SUPPORT
    app, _ = SUPPORT['make_app'](tmp_path)
    p = app.state.studio_pipeline
    pid = p.repository.create_project('合成原子提交公司').id
    version = p.evidence.save(sample(pid, '合成新原文。'))
    p.store.put_profile(pid, {'summary': '此前档案'})
    previous = p.store.get_profile(pid)
    def broken(db):
        p.store.add_doc_change(pid, ['synthetic-new'], {'texts': []}, db=db)
        raise RuntimeError('synthetic failure after change insert')
    with pytest.raises(RuntimeError):
        p.store.put_compiled_profile(pid, {'summary': '新档案'}, [version['id']],
            expected_generation=p.evidence.dependencies.generation(pid), on_current=broken)
    assert p.store.get_profile(pid) == previous
    assert p.store.doc_changes(pid) == []


@pytest.mark.parametrize(('definition', 'equal'), [
    ('三线治疗（经过二线或以上系统性治疗后）。', True),
    ('三线LBCL 该产品用于治疗经过二线或以上系统性治疗后成人患者。', True),
    ('三线治疗不等于经过二线或以上系统性治疗后。', False),
    ('三线治疗不能用于未经二线或以上系统性治疗后的人群。', False),
    ('两句话没有定义关系。', False),
])
def test_bare_line_shorthand_needs_source_definition(definition, equal):
    assert Pipeline._equivalent_internal_statement('成人经过二线或以上系统性治疗后。',
        '成人三线治疗。', context=definition) is equal


def test_source_defined_shorthand_does_not_hide_a_changed_age_or_product():
    context = '三线治疗（经过二线或以上系统性治疗后）。'
    assert not Pipeline._equivalent_internal_statement('儿童经过二线或以上系统性治疗后。', '成人三线治疗。', context=context)
    assert not Pipeline._equivalent_internal_statement('ALPHA成人经过二线或以上系统性治疗后。', 'BETA成人三线治疗。', context=context)
