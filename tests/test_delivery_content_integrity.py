"""Generic content regressions: no company-specific rules or provider requests."""
import pytest

from project_watch.studio import race, techroutes
from project_watch.studio.triage_impact import bound_impact


@pytest.mark.parametrize('text', [
    '合成企业资本热捧，量产未至', '合成企业尚未量产', '合成企业量产没有完成',
    '合成企业预计量产', '合成企业能否量产？', '合成企业量产仍需验证',
])
def test_stage_is_not_attained_by_a_keyword(text):
    assert race.classify_step(text, 'hardware') is None


def test_title_cannot_borrow_a_later_actors_stage():
    assert race.headline_step('合成甲融资完成，合成乙实现量产', ['合成甲'], 'hardware') is None
    assert race.headline_step('合成甲称合成乙实现量产', ['合成甲'], 'hardware') is None
    assert race.headline_step('合成甲XG-100实现量产', ['合成甲'], 'hardware') == 4
    assert race.headline_step('合成甲XG-100量产，尚未实现', ['合成甲'], 'hardware') is None
    assert race.headline_step('合成甲XG-100量产：尚未实现', ['合成甲'], 'hardware') is None
    assert race.headline_step('未来科技XG-100实现量产', ['未来科技'], 'hardware') == 4


def test_attributed_stage_precedes_title_keyword():
    candidates = [race._candidate(2, '2026-10-01', '媒体', 'attributed_fact', '样机试用', 'https://a.invalid'),
                  race._candidate(4, '2026-10-02', '媒体', 'title', '量产', 'https://b.invalid')]
    assert race._best_stage(candidates, race.TEMPLATES['hardware']['steps'])['index'] == 2


def test_generated_relationship_phrase_is_not_a_technical_route():
    docs = [{'title': '机器人研究进展', 'summary': '属同领域技术路线，虚构架构'}] * 4
    assert techroutes.suggest(industry=None, docs=docs, self_texts=[], exclude=[]) == []
    assert not techroutes._terms('属同领域技术路线')
    docs = [{'title': 'FMCW与MEMS路线对比'}] * 3
    assert {'FMCW', 'MEMS'} <= {r['term'] for r in techroutes.suggest(industry=None, docs=docs, self_texts=[], exclude=[])}


def test_unbound_impact_does_not_discard_news():
    row = {'summary': '竞品发布新产品。', 'impact': 'BP中收入预测的单价假设承压。'}
    result = bound_impact(row, '竞品发布新产品。', {})
    assert result['summary'] == row['summary'] and result['impact'] == ''


def test_internal_premise_requires_located_quote_not_just_an_id():
    quote = '2026年计划完成核心产品客户验收'
    evidence = {'I1': {'evidence_version_id': 'version', 'quote': quote}}
    row = {'impact': f'内部计划“{quote}”的验证周期可能延长。',
           'impact_internal_refs': [{'ref': 'I1', 'quote': quote}]}
    assert bound_impact(row, '', evidence)['impact'] == row['impact']
    assert bound_impact(row, '', {})['impact'] == ''
    assert bound_impact({**row, 'impact': '内部计划2027年实现收入百亿。'}, '', evidence)['impact'] == ''
    fabricated = {**row, 'impact': f'内部计划“{quote}”；公司另计划明年收入达到100亿元。'}
    assert bound_impact(fabricated, '', evidence)['impact'] == ''
    assert bound_impact({'impact': '2026年收入预测的单价假设承压。'}, '', {})['impact'] == ''


def test_public_report_can_quote_a_bp_without_user_upload():
    text = '报道披露其BP曾预计2025年收入增长。'
    assert bound_impact({'impact': text}, text, {})['impact'] == text


def test_internal_impact_checks_current_version_and_provider_permission(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from test_studio_evidence_versions import sample
    from test_studio_triage_split import _setup

    store, project, pipeline = _setup(tmp_path, 1)
    quote = '2026年计划完成核心产品客户验收'
    evidence = sample(project.id, quote).model_copy(update={'internal_source_id': 'source'})
    first = pipeline.evidence.save(evidence)
    profile = {'milestones': [{'refs': [{'evidence_version_id': first['id'], 'quote': quote}]}]}
    monkeypatch.setattr(pipeline, 'model_view', lambda _: SimpleNamespace(binding='provider', get_profile=lambda _: profile))
    monkeypatch.setattr(store, 'source_analysis_allowed', lambda pid, sid, binding: pid == project.id and sid == 'source' and binding == 'provider')
    assert pipeline._impact_evidence(project.id, None)['I1']['quote'] == quote
    monkeypatch.setattr(store, 'source_analysis_allowed', lambda *_: False)
    assert pipeline._impact_evidence(project.id, None) == {}
    monkeypatch.setattr(store, 'source_analysis_allowed', lambda *_: True)
    pipeline.evidence.save(evidence.model_copy(update={'extraction_version': 'new-version'}))
    assert pipeline._impact_evidence(project.id, None) == {}


def test_impact_audit_is_saved_without_removing_news(tmp_path):
    from test_studio_triage_split import _setup

    store, project, _ = _setup(tmp_path, 1)
    item = store.untriaged(project.id)[0]
    result = bound_impact({'id': item['id'], 'summary': item['title'], 'impact': 'BP中的收入预测承压。',
                           'triage_binding': {'version': 1, 'status': 'bound'}}, item['title'], {})
    store.save_triage([result])
    saved = store.list_items(project_id=project.id)[0]
    assert saved['summary'] == item['title'] and saved['impact'] == ''
    assert saved['extra']['triage_binding']['impact_status'] == 'unsupported_internal_premise'


@pytest.mark.parametrize('has_prior_call', [False, True])
def test_triage_protocol_is_frozen_before_collection_and_preserves_old_calls(tmp_path, has_prior_call):
    from test_studio_triage_split import _setup

    store, project, pipeline = _setup(tmp_path, 1)
    job = store.enqueue(project.id, 'collect')
    class Journal:
        def __init__(self):
            self.job, self.saved = job, {}
        def read_checkpoint(self, key):
            return self.saved.get(key)
        def checkpoint(self, key, value):
            self.saved[key] = value
    execution = Journal()
    def add_old_call():
        with store._connect() as db:
            db.execute('INSERT INTO studio_job_calls (job_id,call_key,kind,provider,status,created_at) VALUES (?,?,?,?,?,?)',
                       (job['id'], 'saved-call', 'model', 'synthetic', 'unknown', '2026-11-01T00:00:00+00:00'))
    if has_prior_call:
        add_old_call()
    expected = 1 if has_prior_call else 2
    assert pipeline._triage_protocol(execution) == expected
    if not has_prior_call:
        add_old_call()
    assert pipeline._triage_protocol(execution) == expected
    with store._connect() as db:
        assert db.execute('SELECT status FROM studio_job_calls WHERE job_id=?', (job['id'],)).fetchone()[0] == 'unknown'
