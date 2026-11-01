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


def two_points():
    return dict(verdict='steady', confidence='medium', summary='合成材料仍在招募阶段。',
                **{'for': [dict(text='合成材料记录招募状态。', refs=['S1']),
                           dict(text='合成材料显示已完成全部入组。', refs=['S1'])], 'against': [], 'change_if': []})


def judging(unsupported=(), labels_seen=None, malformed_once=None):
    """Synthetic reviewer: listed paths are insufficient, every other field supported."""
    calls = []

    def review(system, payload):
        calls.append(sorted(payload['fields']))
        if 'conflicts' in system:
            return dict(consistent=True, reason='synthetic consistent', conflicts=[])
        if labels_seen is not None:
            labels_seen.update(p for p in payload['fields'] if p.split('.')[-1] in section_analysis.LABELS)
        if malformed_once is not None and not malformed_once:
            malformed_once.append(True)
            return dict(checks=[])  # misses every field: malformed
        return dict(checks=[dict(field=f, status='insufficient' if f in unsupported else 'supported',
                                 reason='synthetic', evidence=[dict(ref='S1', quote='RECRUITING')])
                            for f in payload['fields']])
    review.calls = calls
    return review


def test_an_unsupported_point_is_dropped_and_the_rest_published(host):
    result, view = apply(host, dict(operations=two_points()), judging({'operations.for.1.text'}))
    outcome = result['sections']['operations']
    assert result['published'] == ['operations'] and outcome['dropped'] == ['合成材料显示已完成全部入组。']
    texts = [p['text'] for p in view['analysis']['operations']['for']]
    assert texts == ['合成材料记录招募状态。'] and view['analysis']['operations']['confidence'] == 'low'


def test_an_unsupported_summary_still_rejects_the_section(host):
    result, _ = apply(host, dict(operations=two_points()), judging({'operations.summary'}))
    assert result['published'] == [] and result['sections']['operations']['status'] == 'rejected'


def test_labels_are_not_sent_for_quote_checking(host):
    seen = set()
    result, _ = apply(host, dict(operations=two_points()), judging(labels_seen=seen))
    assert result['published'] == ['operations'] and seen == set()


def test_a_malformed_review_is_reviewed_again_in_halves(host):
    review = judging(malformed_once=[])
    result, _ = apply(host, dict(operations=two_points()), review)
    assert result['published'] == ['operations']
    field_calls = [c for c in review.calls if c and not any('conflict' in x for x in c)]
    assert len(field_calls[0]) > 1 and sorted(field_calls[1] + field_calls[2]) == field_calls[0]


def test_supporting_and_opposing_points_are_not_a_conflict():
    conflict = dict(fields=['position.for.0.text', 'position.against.1.text'], code='evidence_cross_section_conflict')
    assert section_analysis._inherent(conflict)
    assert not section_analysis._inherent(dict(fields=['position.summary', 'operations.summary']))


def test_reviewer_quotes_are_compared_by_content():
    from project_watch.studio import judgment_review as review

    assert review.quoted_in('ＲＥＣＲＵＩＴＩＮＧ  status', 'Overall status: RECRUITING; status updated')
    assert review.quoted_in('Phase 3……met its primary endpoint', 'Phase 3 trial (n=800) met its primary endpoint.')
    assert not review.quoted_in('met its primary endpoint……Phase 3', 'Phase 3 trial (n=800) met its primary endpoint.')
    assert not review.quoted_in('failed its primary endpoint', 'Phase 3 trial met its primary endpoint.')
    aliases = review.ReviewFieldAliases({'fields': {'a.summary': 'x'},
                                         'evidence': {'S1': {'context': 'exact span', 'title': ''}}})
    decoded = aliases.decode({'checks': [{'field': 'F1', 'status': 'supported', 'reason': 'r',
                                          'evidence': [{'ref': 'S1', 'quote_id': 'Q1', 'quote': 'paraphrased echo'}]}]})
    assert decoded['checks'][0]['evidence'][0]['quote'] == 'exact span'


def test_a_whole_quote_containing_an_ellipsis_is_verbatim():
    from project_watch.studio import judgment_review as review

    source = 'Capacity reached 100GWh… with more lines planned in 2027.'
    assert review.quoted_in('100GWh… with', source)
    assert review.quoted_in('reached 100GWh...', 'Capacity reached 100GWh... in total.')


def test_an_unfound_citation_makes_only_that_field_unsupported():
    from project_watch.studio import judgment_review as review

    payload = {'fields': {'a.summary': 'x', 'a.for.0.text': 'y'},
               'evidence': {'S1': {'context': 'Enrollment is RECRUITING.', 'title': ''}}}
    checks = [dict(field='a.summary', status='supported', reason='r', evidence=[dict(ref='S1', quote='RECRUITING')]),
              dict(field='a.for.0.text', status='supported', reason='r',
                   evidence=[dict(ref='S1', quote='Enrollment completed')])]
    issues = review.validate_review({'checks': checks}, payload)
    assert [(i['field'], i['code']) for i in issues] == [('a.for.0.text', 'evidence_insufficient')]
    checks[1]['status'] = 'insufficient'  # a bad citation on a non-support verdict adds nothing
    assert [i['code'] for i in review.validate_review({'checks': checks}, payload)] == ['evidence_insufficient']


def test_quote_ids_take_their_span_source_and_unknown_ids_are_unverified():
    from project_watch.studio import judgment_review as review

    payload = {'fields': {'a.summary': 'x', 'a.for.0.text': 'y'},
               'evidence': {'S1': {'context': 'first span', 'title': ''}, 'S2': {'context': 'second span', 'title': ''}}}
    aliases = review.ReviewFieldAliases(payload)
    decoded = aliases.decode({'checks': [
        {'field': 'F1', 'status': 'supported', 'reason': 'r', 'evidence': [{'ref': 'S2', 'quote_id': 'Q1'}]},
        {'field': 'F2', 'status': 'supported', 'reason': 'r', 'evidence': [{'ref': 'S1', 'quote_id': 'Q99'}]}]})
    assert decoded['checks'][0]['evidence'] == [{'ref': 'S1', 'quote': 'first span'}]
    issues = review.validate_review(decoded, payload)
    assert [(i['field'], i['code']) for i in issues] == [('a.for.0.text', 'evidence_insufficient')]


def test_an_invented_citation_drops_only_its_point(host):
    def invented(system, payload):
        response = judging()(system, payload)
        for check in response.get('checks', []):
            if check['field'] == 'operations.for.1.text':
                check['evidence'] = [dict(ref='S1', quote='Invented unrelated quote')]
        return response
    result, _ = apply(host, dict(operations=two_points()), invented)
    assert result['published'] == ['operations'] and result['sections']['operations']['dropped'] == ['合成材料显示已完成全部入组。']
