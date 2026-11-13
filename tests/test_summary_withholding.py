"""A summary that fails review is withheld; the reviewed section still publishes. Synthetic, offline."""
from types import SimpleNamespace

import pytest
import test_section_analysis as fixtures
from test_section_analysis import apply, json_text, judging, two_points

from project_watch.studio import analyze_job, section_analysis


@pytest.fixture
def host(tmp_path):
    return fixtures.host.__wrapped__(tmp_path)


def withholding(research, version=None):
    if version is not None:
        research.execution.checkpoint(analyze_job.REVIEW_PROTOCOL, {'version': version})
    research.execution.checkpoint(section_analysis.SUMMARY_POLICY, {'withhold': True})


def test_without_the_policy_an_unsupported_summary_still_rejects_the_section(host):
    result, _ = apply(host, dict(operations=two_points()), judging({'operations.summary'}))
    assert result['published'] == [] and result['sections']['operations']['status'] == 'rejected'


def test_an_unsupported_summary_is_withheld_and_the_reviewed_points_publish(host):
    research, _, _ = host
    withholding(research)
    result, view = apply(host, dict(operations=two_points()), judging({'operations.summary'}))
    outcome = result['sections']['operations']
    assert result['published'] == ['operations'] and outcome['status'] == 'approved'
    assert outcome['summary_withheld'] == 'synthetic'
    section = view['analysis']['operations']
    assert section['summary'] == '' and section['confidence'] == 'low'
    assert section['gaps'][0] == section_analysis.WITHHELD_NOTE
    assert [p['text'] for p in section['for']] == [p['text'] for p in two_points()['for']]


def test_a_withheld_summary_does_not_keep_an_unsupported_point(host):
    research, _, _ = host
    withholding(research)
    result, view = apply(host, dict(operations=two_points()),
                         judging({'operations.summary', 'operations.for.1.text'}))
    assert result['published'] == ['operations']
    assert [p['text'] for p in view['analysis']['operations']['for']] == ['合成材料记录招募状态。']


def test_a_blocked_scalar_field_still_rejects_the_section(host):
    research, _, _ = host
    withholding(research)
    raw = dict(technology=dict(route='合成路线', maturity='early', consensus='unclear', confidence='low',
                               summary='合成材料仍在招募阶段。', academic=[], risks=[],
                               industry=[dict(text='合成材料记录招募状态。', refs=['S1'])]))
    result, _ = apply(host, raw, judging({'technology.summary', 'technology.route'}))
    assert result['published'] == [] and result['sections']['technology']['status'] == 'rejected'


def test_no_reviewed_points_left_means_nothing_to_publish(host):
    research, _, _ = host
    withholding(research)
    result, _ = apply(host, dict(operations=two_points()),
                      judging({'operations.summary', 'operations.for.0.text', 'operations.for.1.text'}))
    assert result['published'] == []


def test_the_summary_is_rewritten_once_before_it_is_withheld(host):
    research, context, sources = host
    withholding(research, version=2)
    raw = dict(operations=two_points())
    raw['operations']['summary'] = '合成材料显示公司全面领先。'
    review = judging({'operations.summary'})  # the rewrite fails too
    rewrites = []
    llm = SimpleNamespace(ready=True, json=lambda system, user: rewrites.append(user) or {'summary': '另一句合成总结。'})
    outcomes = section_analysis.evaluate(research.execution, raw, sources, review, lambda: None)
    assert outcomes['operations']['status'] == 'approved' and raw['operations']['summary'] == ''
    outcomes = analyze_job.repair_summaries(research.pipeline.insight_generator, research.execution, llm, raw,
                                            sources, outcomes, review, lambda: None, None)
    assert len(rewrites) == 1 and 'synthetic' in rewrites[0]
    assert outcomes['operations']['status'] == 'approved'
    assert outcomes['operations']['repaired_summary']['status'] == 'approved'
    assert raw['operations']['summary'] == '' and outcomes['operations']['summary_withheld']
    result = section_analysis.publish(research.pipeline, research.execution, context, raw, sources, outcomes,
                                      lambda: None)
    assert result['published'] == ['operations']


def test_a_rewrite_that_passes_restores_the_summary(host):
    research, context, sources = host
    withholding(research, version=2)
    raw = dict(operations=two_points())
    raw['operations']['summary'] = '合成材料显示公司全面领先。'
    def review(system, payload):
        return judging({'operations.summary'} if '全面领先' in json_text(payload) else set())(system, payload)
    llm = SimpleNamespace(ready=True, json=lambda system, user: {'summary': '合成材料仍在招募阶段。'})
    outcomes = section_analysis.evaluate(research.execution, raw, sources, review, lambda: None)
    outcomes = analyze_job.repair_summaries(research.pipeline.insight_generator, research.execution, llm, raw,
                                            sources, outcomes, review, lambda: None, None)
    assert 'summary_withheld' not in outcomes['operations']
    result = section_analysis.publish(research.pipeline, research.execution, context, raw, sources, outcomes,
                                      lambda: None)
    assert result['published'] == ['operations']
    assert result['sections']['operations'].get('summary_withheld') is None


def test_counterevidence_against_only_the_summary_withholds_it_in_isolated_review(host):
    research, _, _ = host
    withholding(research, version=3)
    review = judging()
    def prepare(payload):
        return {'issues': [{'field': 'operations.summary', 'code': 'evidence_contradicted',
                            'reason': 'synthetic counterevidence'}], 'receipts': ['counter-receipt']}
    review.prepare = prepare
    result, view = apply(host, dict(operations=two_points()), review)
    outcome = result['sections']['operations']
    assert result['published'] == ['operations'] and outcome['summary_withheld'] == 'synthetic counterevidence'
    assert view['analysis']['operations']['summary'] == ''


def job(store, pid, input_version):
    import uuid
    from contextlib import closing

    from project_watch.studio.store import now
    job_id = uuid.uuid4().hex
    with closing(store._connect()) as db, db:
        db.execute("INSERT INTO studio_jobs(id,project_id,kind,status,stage,error,created_at,input_version,"
                   "lease_token,lease_until) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (job_id, pid, 'analyze', 'running', '', '', now(), input_version, 'lease-' + job_id,
                    '2999-01-01T00:00:00+00:00'))
    return {'id': job_id, 'project_id': pid, 'lease_token': 'lease-' + job_id}


def test_the_policy_is_frozen_once_and_a_retry_follows_its_earlier_sibling(host):
    from project_watch.studio.execution import JobExecution

    research, _, _ = host
    store, pid = research.store, research.project_id
    first = JobExecution(store, job(store, pid, 'collect:b1'))
    analyze_job.freeze_summary_policy(first, 'b1', fresh=False)
    assert first.read_checkpoint(section_analysis.SUMMARY_POLICY) == {'withhold': False}
    analyze_job.freeze_summary_policy(first, 'b1', fresh=True)  # never changes once decided
    assert first.read_checkpoint(section_analysis.SUMMARY_POLICY) == {'withhold': False}
    retry = JobExecution(store, job(store, pid, 'collect:b1'))
    analyze_job.freeze_summary_policy(retry, 'b1', fresh=True)
    assert retry.read_checkpoint(section_analysis.SUMMARY_POLICY) == {'withhold': False}
    fresh = JobExecution(store, job(store, pid, 'collect:b2'))
    analyze_job.freeze_summary_policy(fresh, 'b2', fresh=True)
    assert fresh.read_checkpoint(section_analysis.SUMMARY_POLICY) == {'withhold': True}


def test_change_conditions_alone_never_publish_without_a_summary(host):
    """Review finding: change_if/peers carry no source; a section left with only them must not publish."""
    research, _, _ = host
    withholding(research)
    raw = dict(operations=dict(verdict='steady', confidence='low', summary='合成材料显示收入为零。',
                               **{'for': [dict(text='合成材料显示收入为零。', refs=['S1'])], 'against': [],
                                  'change_if': ['合成条件一']}))
    result, _ = apply(host, raw, judging())  # review passes; the normalizer drops the undated point
    assert result['published'] == []


def test_withheld_summary_also_withholds_the_verdict(host):
    research, _, _ = host
    withholding(research)
    raw = dict(operations=two_points())
    raw['operations']['verdict'] = 'improving'
    result, view = apply(host, raw, judging({'operations.summary'}))
    assert result['published'] == ['operations'] and view['analysis']['operations']['verdict'] == 'unclear'


def test_a_withheld_draft_does_not_replace_this_jobs_reviewed_version(host):
    research, _, _ = host
    withholding(research)
    first, view = apply(host, dict(operations=two_points()), judging())
    assert first['published'] == ['operations'] and view['analysis']['operations']['summary']
    later = dict(operations=two_points())
    later['operations']['summary'] = '合成材料显示公司全面领先。'
    result, view = apply(host, later, judging({'operations.summary'}))
    assert result['published'] == [] and result['sections']['operations']['kept_earlier']
    assert view['analysis']['operations']['summary'] == two_points()['summary']


def test_a_job_that_already_sent_requests_keeps_the_old_rule(host):
    from contextlib import closing

    from project_watch.studio.execution import JobExecution
    from project_watch.studio.store import now

    research, _, _ = host
    store, pid = research.store, research.project_id
    started = job(store, pid, 'collect:b3')
    with closing(store._connect()) as db, db:
        db.execute("INSERT INTO studio_job_calls(job_id,call_key,kind,provider,status,created_at) "
                   "VALUES (?,?,?,?,?,?)", (started['id'], 'synthetic', 'model', 'p', 'completed', now()))
    execution = JobExecution(store, started)
    analyze_job.freeze_summary_policy(execution, 'b3', fresh=True)
    assert execution.read_checkpoint(section_analysis.SUMMARY_POLICY) == {'withhold': False}


def test_a_retry_after_a_withholding_sibling_withholds_too(host):
    from project_watch.studio.execution import JobExecution

    research, _, _ = host
    store, pid = research.store, research.project_id
    first = JobExecution(store, job(store, pid, 'collect:b4'))
    analyze_job.freeze_summary_policy(first, 'b4', fresh=True)
    retry = JobExecution(store, job(store, pid, 'redo:b4'))
    analyze_job.freeze_summary_policy(retry, 'b4', fresh=True)
    assert retry.read_checkpoint(section_analysis.SUMMARY_POLICY) == {'withhold': True}
