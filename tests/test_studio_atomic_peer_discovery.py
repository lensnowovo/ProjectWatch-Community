"""Automatic atomic identity only; not a live peer-quality benchmark."""
import pytest
from test_studio_competition_research import research_pipeline
from test_studio_evidence_versions import sample

from project_watch.studio.competition_research import discover_candidates, research_inputs
from project_watch.studio.execution import JobExecution
from project_watch.studio.peer_names import company_name


@pytest.mark.parametrize('name', ['Fictional Kite/Fictional Parent', '甲／乙', '甲\\乙', '甲、乙', '甲;乙', '甲；乙', '甲\n乙'])
def test_composite_automatic_label_does_not_create_company(tmp_path, name):
    pipeline, pid, calls, reads = research_pipeline(tmp_path)
    subject = pipeline.objects.ensure_subject(pid)
    text = f'{name} supplies a fictional product for drug screening.'
    evidence = pipeline.evidence.save(sample(pid, text).model_copy(update={
        'source_key': 'synthetic:composite', 'visibility': 'public'}))
    before = pipeline.objects.page(pid)
    report = {}
    assert discover_candidates(pipeline, pid, subject['id'], [evidence], {'candidates':[
        {'name': name, 'source': 'D1', 'quote': text}]}, report=report) == []
    assert report == {'ambiguous_actor_name': 1}
    assert pipeline.objects.page(pid) == before
    assert company_name(name), 'Do not tighten human/manual names globally'
    assert calls == [] and reads == []


@pytest.mark.parametrize('name', ['Fictional AT&T', 'Synthetic S&P Global', "Fictional Children's Hospital, Inc.",
    'Fictional Bio-Tech', '合成光学（上海）有限公司'])
def test_individual_unfamiliar_names_remain_eligible(tmp_path, name):
    pipeline, pid, _, _ = research_pipeline(tmp_path)
    subject = pipeline.objects.ensure_subject(pid)
    text = f'{name} supplies a fictional product for drug screening.'
    evidence = pipeline.evidence.save(sample(pid, text).model_copy(update={
        'source_key': 'synthetic:individual', 'visibility': 'public'}))
    rows = discover_candidates(pipeline, pid, subject['id'], [evidence], {'candidates':[
        {'name': name, 'source': 'D1', 'quote': text}]})
    assert len(rows) == 1 and rows[0]['name'] == name
    assert rows[0]['parent_id'] is None and rows[0]['aliases'] == []


def test_separately_proposed_source_names_do_not_create_a_group_or_alias(tmp_path):
    pipeline, pid, _, _ = research_pipeline(tmp_path)
    subject = pipeline.objects.ensure_subject(pid)
    text = 'Fictional Kite/Fictional Parent supply fictional CAR-T products.'
    evidence = pipeline.evidence.save(sample(pid, text).model_copy(update={
        'source_key': 'synthetic:two', 'visibility': 'public'}))
    rows = discover_candidates(pipeline, pid, subject['id'], [evidence], {'candidates':[
        {'name': name, 'source': 'D1', 'quote': text} for name in ('Fictional Kite', 'Fictional Parent')]})
    assert {r['name'] for r in rows} == {'Fictional Kite', 'Fictional Parent'}
    assert all(r['parent_id'] is None and not r['aliases'] for r in rows)


def test_atomic_name_does_not_repair_source_spelling_or_quote(tmp_path):
    pipeline, pid, _, _ = research_pipeline(tmp_path)
    subject = pipeline.objects.ensure_subject(pid)
    text = 'Fictional Ktie supplies a fictional CAR-T product.'
    evidence = pipeline.evidence.save(sample(pid, text).model_copy(update={
        'source_key': 'synthetic:spelling', 'visibility': 'public'}))
    assert discover_candidates(pipeline, pid, subject['id'], [evidence], {'candidates':[
        {'name': 'Fictional Kite', 'source': 'D1', 'quote': text}]}) == []


def test_old_discovery_checkpoint_stops_before_search_model_or_read(tmp_path):
    pipeline, pid, calls, reads = research_pipeline(tmp_path)
    pipeline.schedule_competition(pid)
    job = pipeline.store.claim()
    execution = JobExecution(pipeline.store, job)
    inputs = research_inputs(pipeline, pid)
    inputs['prompt_version'] = 'research-v16-query-coverage'
    execution.checkpoint('competition_inputs', inputs)
    execution.checkpoint('competition_candidates', [{'id': 'old', 'name': 'Fictional Kite/Fictional Parent'}])
    with pytest.raises(ValueError, match='old matching contract'):
        pipeline.run(job)
    assert calls == [] and reads == []


def test_normal_flow_retains_bounded_discovery_diagnostic_checkpoint(tmp_path):
    pipeline, pid, _, _ = research_pipeline(tmp_path)
    pipeline.schedule_competition(pid)
    job = pipeline.store.claim()
    pipeline.run(job)
    execution = JobExecution(pipeline.store, job)
    assert execution.read_checkpoint('competition_discovery_rejections') == {}
    assert pipeline.competition_research.saved(pid)['candidate_rejections'] == {}
