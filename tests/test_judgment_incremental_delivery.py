"""Incremental judgments exercise real publication storage, without provider calls."""
from contextlib import closing
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_section_analysis import host as host
from test_section_analysis import part, reviewer

from project_watch.studio import analyze_job as a
from project_watch.studio import insight
from project_watch.studio import judgment_delivery as delivery
from project_watch.studio.execution import JobYield
from project_watch.studio.llm import ModelError


def arrange(host, monkeypatch):
    research, context, sources = host
    execution = research.execution
    execution.checkpoint(a.REVIEW_PROTOCOL, {'version': 4})
    selected = {**context, 'coverage': {'fragments': 2}}
    monkeypatch.setattr(a, 'source_batches', lambda *_: iter([sources, sources]))
    def checked(system, payload):
        return reviewer(system, payload)
    def counter(payload):
        assert set(payload['evidence']) >= set(sources)
        return {'issues': [], 'receipts': []}
    checked.prepare = counter
    monkeypatch.setattr(a, '_reviewer', lambda *args: checked)
    return research, selected, sources, checked


def run(research, selected, sources, respond, targets=None):
    return delivery.deliver_batches(research.pipeline, research.execution,
        SimpleNamespace(json=respond), {}, sources, selected, targets or ['operations'], {}, lambda: None)


def test_first_verified_page_visible_before_next_draft_and_unknown_preserves_it(host, monkeypatch):
    research, selected, sources, _ = arrange(host, monkeypatch)
    calls = []
    def respond(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            view = insight.load(research.store, research.project_id)
            assert view['analysis']['operations']['summary'] == part()['summary']
            raise ModelError('synthetic lost draft', code='request_outcome_unknown')
        return {'operations': part()}
    result = run(research, selected, sources, respond)
    assert result['published'] == ['operations'] and result['status'] == 'partial'
    assert result['coverage']['blocked']
    run(research, selected, sources, respond)
    assert len(calls) == 2  # completed prefix and unknown both replay their receipts


def test_full_snapshot_counterevidence_blocks_early_candidate(host, monkeypatch):
    research, selected, sources, checked = arrange(host, monkeypatch)
    def counter(payload):
        assert set(payload['evidence']) >= set(sources)
        return {'issues': [{'field': 'operations.summary', 'code': 'evidence_contradicted',
                            'reason': 'synthetic tail contradicts summary'}], 'receipts': []}
    checked.prepare = counter
    monkeypatch.setattr(a, 'repair_summaries', lambda g, e, llm, raw, src, outcomes, *rest: outcomes)
    result = run(research, selected, sources, lambda *args, **kwargs: {'operations': part()})
    assert result['published'] == []
    with closing(research.store._connect()) as db:
        assert not db.execute("SELECT 1 FROM studio_insight_heads WHERE artifact_key LIKE 'snapshot-section:%'").fetchone()


def test_review_unknown_cannot_be_bypassed_with_later_candidate(host, monkeypatch):
    research, selected, sources, checked = arrange(host, monkeypatch)
    calls = []
    checked.prepare = lambda _: (_ for _ in ()).throw(ModelError('lost', code='request_outcome_unknown'))
    def respond(*args, **kwargs):
        calls.append(1)
        return {'operations': part()}
    for _ in range(2):
        with pytest.raises(ModelError, match='未重发'):
            run(research, selected, sources, respond)
    assert len(calls) == 1


def test_resume_after_published_checkpoint_does_not_duplicate_versions(host, monkeypatch):
    research, selected, sources, _ = arrange(host, monkeypatch)
    calls = []
    def respond(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise JobYield()
        return {'operations': part()}
    with pytest.raises(JobYield):
        run(research, selected, sources, respond)
    with closing(research.store._connect()) as db:
        before = db.execute('SELECT count(*) FROM studio_insight_versions').fetchone()[0]
    run(research, selected, sources, lambda *args, **kwargs: {'operations': part()})
    with closing(research.store._connect()) as db:
        assert db.execute('SELECT count(*) FROM studio_insight_versions').fetchone()[0] == before


def test_existing_review_keeps_frozen_legacy_path(host):
    research, _, _ = host
    research.execution.checkpoint(a.REVIEW_PROTOCOL, {'version': 4})
    research.execution.checkpoint('section-unit-v3:existing', {'outcome': {'status': 'blocked'}})
    assert not delivery.freeze_mode(research.execution)
    assert not delivery.enabled(research.execution)


def test_later_rejection_keeps_previous_published_head(host, monkeypatch):
    research, selected, sources, _ = arrange(host, monkeypatch)
    drafts = iter([{'operations': part()}, {'operations': part('N404')}])
    run(research, selected, sources, lambda *args, **kwargs: deepcopy(next(drafts)))
    assert insight.load(research.store, research.project_id)['analysis']['operations']['summary'] == part()['summary']


def test_later_page_checked_against_already_visible_peer(host, monkeypatch):
    research, selected, sources, checked = arrange(host, monkeypatch)
    consistency = []
    def review(system, payload):
        if payload.get('phase') == 'consistency':
            consistency.append(payload)
        return checked(system, payload)
    review.prepare = checked.prepare
    monkeypatch.setattr(a, '_reviewer', lambda *args: review)
    drafts = iter([{'operations': part()}, {'chain': part()}])
    run(research, selected, sources, lambda *args, **kwargs: next(drafts), ['operations', 'chain'])
    assert len(consistency) == 1
    assert any(k.startswith('operations.') for k in consistency[0]['fields'])
    assert any(k.startswith('chain.') for k in consistency[0]['fields'])


def test_repeated_candidate_cannot_rewind_consistency_peer(host, monkeypatch):
    research, selected, sources, checked = arrange(host, monkeypatch)
    monkeypatch.setattr(a, 'source_batches', lambda *_: iter([sources] * 4))
    first, second = part(), part()
    second['summary'] = '合成材料正在招募。'
    seen = []
    def review(system, payload):
        if payload.get('phase') == 'consistency':
            seen.append(payload['fields']['operations.summary'])
        return checked(system, payload)
    review.prepare = checked.prepare
    monkeypatch.setattr(a, '_reviewer', lambda *args: review)
    drafts = iter([{'operations': first}, {'operations': second}, {'operations': first}, {'chain': part()}])
    run(research, selected, sources, lambda *args, **kwargs: next(drafts), ['operations', 'chain'])
    assert seen[-1] == second['summary']


def test_same_batch_unknown_never_downgrades_to_legacy_path(host):
    from project_watch.studio.execution import JobExecution
    from project_watch.studio.http import HttpError
    research, _, _ = host
    execution = research.execution
    execution.checkpoint(delivery.MODE, {'enabled': True})
    with closing(research.store._connect()) as db, db:
        db.execute("UPDATE studio_jobs SET kind='analyze', input_version='collect:batch' WHERE id=?",
                   (execution.job['id'],))
    def lost():
        raise HttpError('connection_failed', transport_phase='response_body', failure_kind='transport_error')
    with pytest.raises(HttpError):
        execution.call('model', 'synthetic-unknown', lost, timeout=None)
    new = JobExecution(research.store, {'id': 'new-attempt', 'project_id': research.project_id,
                                       'kind': 'analyze', 'input_version': 'collect:batch'})
    with pytest.raises(ModelError, match='未切换'):
        delivery.freeze_mode(new)


def test_round_failure_result_keeps_incremental_publication_and_coverage(host, monkeypatch):
    research, context, _ = host
    e = research.execution
    e.checkpoint(a.PROTOCOL, {'version': 2})
    e.checkpoint(a.REVIEW_PROTOCOL, {'version': 4})
    e.checkpoint(a.SNAPSHOT, {'id': context['id']})
    e.checkpoint(delivery.CURRENT, {'published': ['operations'], 'sections': {}})
    e.checkpoint(a.COVERAGE, {'processed_fragments': 1, 'remaining_fragments': 1})
    monkeypatch.setattr(research.pipeline, 'tools', lambda: (None, None, SimpleNamespace(ready=True), None))
    monkeypatch.setattr(a, 'sections', lambda *args, **kwargs: (_ for _ in ()).throw(
        ModelError('synthetic unknown', code='unknown_not_resent')))
    monkeypatch.setattr(a, 'follow_through', lambda *args: {})
    result = a.run(research.pipeline, e)
    assert result['status'] == 'partial' and result['published'] == ['operations']
    assert result['coverage']['remaining_fragments'] == 1


def test_page_status_visible_at_publication_yield_boundary(host, monkeypatch):
    from project_watch.studio import section_progress
    research, selected, sources, _ = arrange(host, monkeypatch)
    def yield_after_publication():
        if research.execution.read_checkpoint(delivery.CURRENT):
            raise JobYield()
    monkeypatch.setattr(research.execution, 'yield_if_due', yield_after_publication)
    with pytest.raises(JobYield):
        run(research, selected, sources, lambda *args, **kwargs: {'operations': part()})
    with closing(research.store._connect()) as db:
        states, _ = section_progress.states(db, research.execution.job)
    assert states['operations'] == 'published'
    assert insight.load(research.store, research.project_id)['analysis']['operations']['summary'] == part()['summary']


def test_independent_page_unknown_does_not_block_next_page_or_retry_on_resume(host, monkeypatch):
    research, selected, sources, checked = arrange(host, monkeypatch)
    research.execution.checkpoint(delivery.MODE, {'enabled': True, 'independent_pages': True})
    counters = []
    def counter(payload):
        names = {field.split('.')[0] for field in payload['fields']}
        counters.append(names)
        if 'operations' in names:
            raise ModelError('synthetic lost review', code='request_outcome_unknown')
        assert set(payload['evidence']) >= set(sources)
        return {'issues': [], 'receipts': []}
    checked.prepare = counter
    result = run(research, selected, sources,
                 lambda *args, **kwargs: {'operations': part(), 'chain': part()}, ['operations', 'chain'])
    assert result['published'] == ['chain']
    assert result['sections']['operations']['status'] == 'blocked'
    assert insight.load(research.store, research.project_id)['analysis']['chain']['summary'] == part()['summary']
    before = len(counters)
    run(research, selected, sources, lambda *args, **kwargs: pytest.fail('must reuse draft'),
        ['operations', 'chain'])
    assert len(counters) == before


def test_independent_page_is_visible_before_sibling_review_and_conflict_keeps_old_head(host, monkeypatch):
    research, selected, sources, checked = arrange(host, monkeypatch)
    research.execution.checkpoint(delivery.MODE, {'enabled': True, 'independent_pages': True})
    def check(system, payload):
        if any(field.startswith('chain.') for field in payload['fields']):
            assert insight.load(research.store, research.project_id)['analysis']['operations']['summary'] == part()['summary']
        if payload.get('phase') == 'consistency':
            assert 'operations.summary' in payload['fields']
            return {'consistent': False, 'reason': 'synthetic contradiction', 'conflicts': [
                {'fields': ['operations.summary', 'chain.summary'], 'reason': 'incompatible statements'}]}
        return checked(system, payload)
    check.prepare = checked.prepare
    monkeypatch.setattr(a, '_reviewer', lambda *args: check)
    monkeypatch.setattr(a, 'repair_summaries', lambda g, e, llm, raw, src, outcomes, *rest: outcomes)
    result = run(research, selected, sources,
                 lambda *args, **kwargs: {'operations': part(), 'chain': part()}, ['operations', 'chain'])
    assert result['published'] == ['operations']
    assert result['sections']['chain']['status'] == 'rejected'


def test_existing_delivery_mode_does_not_silently_change_review_requests(host):
    research, _, _ = host
    research.execution.checkpoint(delivery.MODE, {'enabled': True})
    assert delivery.freeze_mode(research.execution)
    assert not delivery.independent_pages(research.execution)


def test_legacy_ambiguous_model_5xx_cannot_switch_to_independent_requests(host):
    from project_watch.studio.http import Response
    research, _, _ = host
    execution = research.execution
    execution.checkpoint(a.REVIEW_PROTOCOL, {'version': 4})
    execution.call('model', 'synthetic-old-500', lambda: Response(500, {}, b'{}'), timeout=None)
    with closing(research.store._connect()) as db, db:
        db.execute('UPDATE studio_job_calls SET retryable=0 WHERE job_id=?', (execution.job['id'],))
    assert not delivery.freeze_mode(execution)
    assert not delivery.independent_pages(execution)


def test_unknown_page_stays_fenced_across_changed_draft_and_publication_yield(host, monkeypatch):
    research, selected, sources, checked = arrange(host, monkeypatch)
    execution = research.execution
    execution.checkpoint(delivery.MODE, {'enabled': True, 'independent_pages': True})
    calls = []
    def counter(payload):
        names = {field.split('.')[0] for field in payload['fields']}
        calls.append(names)
        if names == {'operations'}:
            raise ModelError('synthetic uncertain page', code='request_outcome_unknown')
        return {'issues': [], 'receipts': []}
    checked.prepare = counter
    yielded = []
    def pause():
        current = execution.read_checkpoint(delivery.CURRENT)
        if current and 'operations' in current['sections'] and not yielded:
            assert execution.read_checkpoint('independent-page-blocked:operations') is None
            yielded.append(True)
            raise JobYield()
    monkeypatch.setattr(execution, 'yield_if_due', pause)
    changed = part()
    changed['summary'] = '合成材料正在招募。'
    drafts = iter([{'operations': part(), 'chain': part()}, {'operations': changed, 'chain': part()}])
    def respond(*args, **kwargs):
        return deepcopy(next(drafts))
    with pytest.raises(JobYield):
        run(research, selected, sources, respond, ['operations', 'chain'])
    result = run(research, selected, sources, respond, ['operations', 'chain'])
    assert result['published'] == ['chain']
    assert calls.count({'operations'}) == 1
    assert execution.read_checkpoint('independent-page-blocked:operations')


def test_independent_pages_review_first_result_and_final_not_every_draft(host, monkeypatch):
    research, selected, sources, checked = arrange(host, monkeypatch)
    research.execution.checkpoint(delivery.MODE, {'enabled': True, 'independent_pages': True})
    monkeypatch.setattr(a, 'source_batches', lambda *_: iter([sources] * 3))
    reviewed = []
    def counter(payload):
        reviewed.append(payload['fields']['operations.summary'])
        return {'issues': [], 'receipts': []}
    checked.prepare = counter
    drafts = []
    for summary in ['合成材料仍在招募阶段。', '合成材料处于招募。', '合成材料正在招募。']:
        candidate = part()
        candidate['summary'] = summary
        drafts.append({'operations': candidate})
    iterator = iter(drafts)
    result = run(research, selected, sources, lambda *args, **kwargs: next(iterator))
    assert reviewed == [drafts[0]['operations']['summary'], drafts[2]['operations']['summary']]
    assert result['published'] == ['operations']
    assert insight.load(research.store, research.project_id)['analysis']['operations']['summary'] == reviewed[-1]
    count = len(reviewed)
    run(research, selected, sources, lambda *args, **kwargs: pytest.fail('saved drafts must replay'))
    assert len(reviewed) == count
