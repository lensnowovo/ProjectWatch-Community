"""Section publication uses real immutable storage and source withdrawal fences."""
from contextlib import closing

import pytest
import test_collection_ingest as fixtures
from test_collection_ingest import trial

from project_watch.studio import insight, material_snapshot, section_analysis


@pytest.fixture
def host(tmp_path):
    research, unit, ingest = fixtures.host.__wrapped__(tmp_path)
    ingest(unit, [trial()])
    saved = material_snapshot.capture(research.execution)
    context = material_snapshot.read(research.store, research.project_id, saved['id'])
    return research, context, section_analysis.numbered(context)


def part(ref='S1'):
    return dict(verdict='steady', confidence='low', summary='合成材料仍在招募阶段。',
                **{'for': [dict(text='合成材料记录招募状态。', refs=[ref])], 'against': [], 'change_if': []})


def reviewer(system, payload):
    if 'conflicts' in system:
        return dict(consistent=True, reason='synthetic consistent', conflicts=[])
    return dict(checks=[dict(field=field, status='supported', reason='synthetic source match',
                            evidence=[dict(ref='S1', quote='RECRUITING')]) for field in payload['fields']])


def apply(host, raw, review=reviewer):
    research, context, sources = host
    outcomes = section_analysis.evaluate(research.execution, raw, sources, review, lambda: None)
    result = section_analysis.publish(research.pipeline, research.execution, context, raw, sources, outcomes, lambda: None)
    return result, insight.load(research.store, research.project_id)


def test_bad_reference_rejects_only_its_section_and_preserves_sources(host):
    result, view = apply(host, dict(operations=part(), chain=part('N404')))
    assert result['status'] == 'partial' and result['published'] == ['operations']
    assert result['sections']['chain']['status'] == 'rejected'
    assert view['analysis']['operations']['summary'] == part()['summary']
    assert view['analysis']['chain']['verdict'] == 'unclear'
    assert view['counts'] == {'S': 1}
    assert view['analysis']['cited'][0]['label'] == '公开候选材料'
    research, context, _ = host
    assert material_snapshot.read(research.store, research.project_id, context['id']) == context
    assert not research.store.list_events(project_id=research.project_id)


def test_missing_new_section_retains_previous_version_and_date(host):
    _, first = apply(host, dict(operations=part()))
    result, second = apply(host, dict(chain=part()))
    assert result['sections']['operations']['status'] == 'missing'
    assert second['analysis']['operations'] == first['analysis']['operations']
    assert second['section_versions']['operations'] == first['section_versions']['operations']
    assert second['analysis']['chain']['summary']
    past = insight.history(host[0].store, host[0].project_id)
    assert past and past[0]['section_versions']['operations']['version_id'] == first['section_versions']['operations']['version_id']


def test_cross_section_conflict_blocks_only_affected_sections(host):
    def conflict(system, payload):
        if 'conflicts' in system:
            return dict(consistent=False, reason='synthetic conflict', conflicts=[dict(
                fields=['operations.summary', 'chain.summary'], reason='contradictory scope')])
        return reviewer(system, payload)
    result, view = apply(host, dict(operations=part(), chain=part()), conflict)
    assert result['published'] == [] and view is None
    assert all(result['sections'][name]['status'] == 'rejected' for name in ('operations', 'chain'))


def test_withdrawal_hides_section_but_keeps_version_history(host):
    _, view = apply(host, dict(operations=part()))
    research, context, _ = host
    version = view['section_versions']['operations']['version_id']
    research.store.evidence.dependencies.withdraw(research.project_id,
        context['materials'][0]['evidence_version_id'], 'Synthetic withdrawal', actor='test')
    after = insight.load(research.store, research.project_id)
    assert after['section_versions']['operations']['status'] == 'withdrawn'
    assert not after['analysis']['operations']['summary']
    with closing(research.store._connect()) as db:
        assert db.execute('SELECT 1 FROM studio_insight_versions WHERE id=?', (version,)).fetchone()


def test_invalid_review_quote_cannot_publish(host):
    def invalid(system, payload):
        response = reviewer(system, payload)
        response['checks'][0]['evidence'][0]['quote'] = 'Invented unrelated quote'
        return response
    result, view = apply(host, dict(operations=part()), invalid)
    assert result['published'] == [] and view is None


def test_settled_malformed_review_does_not_erase_independent_section(host):
    from project_watch.studio.llm import ModelError
    def malformed(system, payload):
        if any(field.startswith('operations.') for field in payload['fields']):
            raise ModelError('synthetic malformed review', code='model_output_invalid_shape')
        return reviewer(system, payload)
    result, view = apply(host, dict(operations=part(), chain=part()), malformed)
    assert result['published'] == ['chain']
    assert result['sections']['operations']['status'] == 'rejected'
    assert view['analysis']['chain']['summary']


def test_new_section_conflict_with_retained_baseline_preserves_old_version(host):
    _, old = apply(host, dict(operations=part()))
    research, context, sources = host
    version = old['section_versions']['operations']['version_id']
    baseline = {**context, 'baseline_analysis': {'snapshot-section:operations': version}}
    retained = section_analysis.retained_sections(research.store, research.project_id, baseline)
    def conflict(system, payload):
        if payload.get('phase') == 'consistency':
            assert 'operations.summary' in payload['fields']
            return dict(consistent=False, reason='synthetic mixed-version conflict', conflicts=[dict(
                fields=['operations.summary', 'chain.summary'], reason='incompatible statements')])
        return reviewer(system, payload)
    raw = dict(chain=part())
    outcomes = section_analysis.evaluate(research.execution, raw, sources, conflict, lambda: None, retained)
    result = section_analysis.publish(research.pipeline, research.execution, context, raw, sources, outcomes, lambda: None,
                                      targets=['chain'])
    assert result['status'] == 'partial' and not result['published']
    after = insight.load(research.store, research.project_id)
    assert after['section_versions']['operations']['version_id'] == version
    assert after['analysis']['operations'] == old['analysis']['operations']
