"""Synthetic offline acceptance for bounded page work and resumable verdicts."""
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
import test_page_summaries as fixtures
from test_page_summaries import draft, reviewer

from project_watch.studio import page_summaries as pages
from project_watch.studio.execution import JobYield
from project_watch.studio.llm import ModelError


@pytest.fixture
def host(tmp_path):
    return fixtures.host.__wrapped__(tmp_path)


def install(host, monkeypatch, generate, check, batches=2):
    research, context, sources = host
    monkeypatch.setattr(pages, 'select_context', lambda *args: context)
    monkeypatch.setattr(pages.analyze_job, 'source_batches', lambda *args: iter([sources] * batches))
    monkeypatch.setattr(research.pipeline, 'tools', lambda **kw: (None, None, SimpleNamespace(json=generate), None))
    monkeypatch.setattr(pages.analyze_job, '_reviewer', lambda *args: check)
    return research


def test_failed_page_leaves_later_draft_contract_and_all_failed_stops(host, monkeypatch):
    sent = []
    def generate(system, payload, **kwargs):
        data = json.loads(payload)
        sent.append(data)
        return {page: draft() for page in data['page_contracts']}
    def check(system, payload):
        if payload['page_contract'] == pages.CONTRACTS['trials']:
            raise ModelError('synthetic rejected page', code='model_content_rejected')
        return reviewer(system, payload)
    research = install(host, monkeypatch, generate, check, batches=3)
    result = pages.run(research.pipeline, research.execution, targets=('trials', 'papers'))
    assert result['failures'] == {'trials': 'model_content_rejected'}
    assert list(sent[0]['page_contracts']) == ['trials', 'papers']
    assert all(set(row['page_contracts']) == {'papers'} and 'trials' not in row['previous_draft'] for row in sent[1:])
    assert 'papers' in result['published']


def test_all_pages_failed_stops_shared_drafts(host, monkeypatch):
    sent = []
    def generate(*a, **k):
        sent.append(1)
        return {'trials': draft()}
    def check(*a):
        raise ModelError('synthetic rejected page', code='model_content_rejected')
    research = install(host, monkeypatch, generate, check, batches=8)
    result = pages.run(research.pipeline, research.execution, targets=('trials',))
    assert len(sent) == 1 and result['published'] == []
    assert result['status'] == 'partial'


def test_unsupported_candidate_never_traverses_full_corpus(host, monkeypatch):
    calls = []
    def check(system, payload):
        calls.append('support')
        answer = reviewer(system, payload)
        for row in answer['checks']:
            row.update(status='insufficient', evidence=[])
        return answer
    check.prepare = lambda _: pytest.fail('unsupported candidate must not scan counterevidence')
    research = install(host, monkeypatch, lambda *a, **k: {'trials': draft()}, check, batches=3)
    result = pages.run(research.pipeline, research.execution, targets=('trials',))
    assert result['published'] == [] and calls == ['support']


def test_exact_candidate_support_and_tail_counter_checked_once(host, monkeypatch):
    calls = []
    def check(system, payload):
        calls.append('support')
        return reviewer(system, payload)
    def counter(payload):
        calls.append('counter')
        assert set(payload['evidence']) == set(host[2])
        return {'issues': []}
    check.prepare = counter
    research = install(host, monkeypatch, lambda *a, **k: {'trials': draft()}, check, batches=3)
    result = pages.run(research.pipeline, research.execution, targets=('trials',))
    assert result['published'] == ['trials']
    assert calls == ['support', 'counter']


def test_later_new_fact_is_reviewed_and_published_after_early_success(host, monkeypatch):
    first, later = draft(), draft()
    later['sentences'][1]['text'] = '研究继续招募，材料补充了新的观察信息。'
    generated = iter([{'trials': first}, {'trials': later}])
    reviewed = []
    def check(system, payload):
        reviewed.append(tuple(payload['fields'].values()))
        return reviewer(system, payload)
    research = install(host, monkeypatch, lambda *a, **k: next(generated), check)
    pages.run(research.pipeline, research.execution, targets=('trials',))
    assert len(reviewed) == 2
    assert pages.read(research.store, research.project_id)['trials']['text'] == ''.join(r['text'] for r in later['sentences'])


def test_completed_candidate_survives_safe_yield_without_repeat(host, monkeypatch):
    counts = {'draft': 0, 'review': 0, 'yield': 0}
    def generate(*a, **k):
        counts['draft'] += 1
        return {'trials': draft()}
    def check(system, payload):
        counts['review'] += 1
        return reviewer(system, payload)
    research = install(host, monkeypatch, generate, check)
    def interrupt():
        counts['yield'] += 1
        if counts['yield'] == 1:
            assert 'trials' in pages.read(research.store, research.project_id)
            raise JobYield()
    monkeypatch.setattr(research.execution, 'yield_if_due', interrupt)
    with pytest.raises(JobYield):
        pages.run(research.pipeline, research.execution, targets=('trials',))
    result = pages.run(research.pipeline, research.execution, targets=('trials',))
    assert result['published'] == ['trials']
    assert counts['draft'] == 2 and counts['review'] == 1


def test_legacy_targets_keep_original_request_and_review_order(host, monkeypatch):
    seen = []
    def check(system, payload):
        seen.append('support')
        return reviewer(system, payload)
    def counter(payload):
        seen.append('counter')
        return {'issues': []}
    check.prepare = counter
    research = install(host, monkeypatch, lambda *a, **k: {'trials': draft()}, check, batches=1)
    research.execution.checkpoint(pages.PROTOCOL + ':targets', ['trials'])
    research.execution.checkpoint(pages.PROTOCOL + ':retain-candidates', True)
    research.execution.checkpoint(pages.PROTOCOL + ':progressive', True)
    pages.run(research.pipeline, research.execution, targets=('trials',))
    assert research.execution.read_checkpoint(pages.EXECUTION_PROTOCOL) is None
    assert seen[:2] == ['counter', 'support']


def test_candidate_cache_invalidation_and_guard(host):
    research, context, sources = host
    calls = []
    def check(system, payload):
        calls.append(1)
        return reviewer(system, payload)
    def run(ctx, candidate, evidence, binding='binding', guard=lambda: None):
        return pages.candidate_review(research.execution, ctx, 'trials', candidate, evidence, check, binding, guard)
    assert run(context, draft(), sources)[1]
    assert not run(context, draft(), sources)[1]
    changed = draft()
    changed['sentences'][0]['text'] = 'Synthetic study 尚未开始招募。'
    assert run(context, changed, sources)[1]
    assert run({**context, 'id': 'different-snapshot'}, draft(), sources)[1]
    evidence = deepcopy(sources)
    evidence['S1']['title'] += ' changed'
    assert run(context, draft(), evidence)[1]
    assert run(context, draft(), sources, binding='different-provider')[1]
    def revoked():
        raise RuntimeError('permission revoked')
    with pytest.raises(RuntimeError, match='permission revoked'):
        run(context, draft(), sources, guard=revoked)
    assert len(calls) == 5


def test_unknown_does_not_create_completed_candidate_cache(host):
    research, context, sources = host
    def check(*args):
        raise ModelError('synthetic unknown held', code='unknown_not_resent')
    with pytest.raises(ModelError, match='unknown held'):
        pages.candidate_review(research.execution, context, 'trials', draft(), sources, check, 'binding', lambda: None)
    from contextlib import closing
    with closing(research.store._connect()) as db:
        assert db.execute('SELECT count(*) FROM studio_job_checkpoints WHERE job_id=? AND stage LIKE ?',
                          (research.execution.job['id'], pages.PROTOCOL + ':candidate:%')).fetchone()[0] == 0


def test_uncited_tail_contradiction_is_not_removed_by_support_first(host):
    research, context, sources = host
    sources = {**sources, 'S2': {**sources['S1'], 'context': 'Synthetic later correction: recruitment was cancelled.'}}
    seen = []
    def check(system, payload):
        seen.append('support')
        return reviewer(system, payload)
    def counter(payload):
        seen.append('counter')
        assert set(payload['evidence']) == {'S1', 'S2'}
        assert all('S2' not in claim['refs'] for claim in payload['analysis'].values())
        return {'issues': [{'field': field, 'code': 'evidence_contradicted'} for field in payload['fields']]}
    check.prepare = counter
    outcome, fresh = pages.candidate_review(research.execution, context, 'trials', draft(), sources, check, 'binding', lambda: None)
    assert fresh and outcome['status'] == 'rejected' and seen == ['support', 'counter']


def test_existing_model_journal_without_targets_keeps_legacy_protocol(host, monkeypatch):
    from contextlib import closing
    research = install(host, monkeypatch, lambda *a, **k: {'trials': draft()}, reviewer, batches=1)
    with closing(research.store._connect()) as db, db:
        db.execute('INSERT INTO studio_job_calls (job_id,call_key,kind,provider,status,created_at) VALUES (?,?,?,?,?,?)',
                   (research.execution.job['id'], 'old-unknown', 'model', 'synthetic', 'unknown', '2026-01-01T00:00:00+00:00'))
    pages.run(research.pipeline, research.execution, targets=('trials',))
    assert research.execution.read_checkpoint(pages.EXECUTION_PROTOCOL) is None
    with closing(research.store._connect()) as db:
        assert db.execute('SELECT status FROM studio_job_calls WHERE call_key=?', ('old-unknown',)).fetchone()[0] == 'unknown'


def test_later_drafts_wait_for_final_review_after_first_early_attempt(host, monkeypatch):
    generated, inspected = [], []
    def generate(*a, **k):
        value = draft()
        value['sentences'][0]['text'] = f'Synthetic study 的第{len(generated) + 1}批材料仍在招募。'
        generated.append(value)
        return {'trials': value}
    def check(system, payload):
        return reviewer(system, payload)
    def counter(payload):
        inspected.append(len(generated))
        assert set(payload['evidence']) == set(host[2])
        return {'issues': [{'field': field, 'code': 'evidence_contradicted'} for field in payload['fields']]
                if len(generated) == 1 else []}
    check.prepare = counter
    research = install(host, monkeypatch, generate, check, batches=4)
    result = pages.run(research.pipeline, research.execution, targets=('trials',))
    assert len(generated) == 4 and inspected == [1, 4]
    assert result['published'] == ['trials']
    assert pages.read(research.store, research.project_id)['trials']['text'] == ''.join(r['text'] for r in generated[-1]['sentences'])

@pytest.mark.parametrize('point', ['before_review', 'after_review', 'before_cached_final', 'before_next_draft'])
def test_uncited_frozen_tail_withdrawal_stops_review_cache_and_dispatch(tmp_path, monkeypatch, point):
    from project_watch.studio import material_snapshot, section_analysis
    research, unit, ingest = fixtures.fixtures.host.__wrapped__(tmp_path)
    first = fixtures.trial()
    tail = fixtures.trial(url='https://clinicaltrials.gov/study/NCT00000002')
    tail['extra'] = dict(tail['extra'], source_identity='clinical:NCT00000002', nct_id='NCT00000002')
    ingest(unit, [first, tail])
    snapshot = material_snapshot.capture(research.execution)
    context = material_snapshot.read(research.store, research.project_id, snapshot['id'])
    sources = section_analysis.numbered(context)
    assert len({s['evidence_version_id'] for s in sources.values()}) == 2
    tail_id = next(s['evidence_version_id'] for key, s in sources.items() if key != 'S1')
    calls = []
    def withdraw():
        research.store.evidence.dependencies.withdraw(research.project_id, tail_id, 'synthetic tail withdrawal', actor='test')
    def generate(*args, **kwargs):
        calls.append('draft')
        if point == 'before_review':
            withdraw()
        return {'trials': draft()}
    def check(system, payload):
        calls.append('support')
        response = reviewer(system, payload)
        if point == 'after_review':
            withdraw()
        return response
    original_publish = pages.publish
    def publish(*args):
        result = original_publish(*args)
        if point in ('before_cached_final', 'before_next_draft'):
            withdraw()
        return result
    monkeypatch.setattr(pages, 'publish', publish)
    install((research, context, sources), monkeypatch, generate, check,
            batches=2 if point == 'before_next_draft' else 1)
    with pytest.raises(ValueError, match='withdrawn'):
        pages.run(research.pipeline, research.execution, targets=('trials',))
    assert calls.count('draft') == 1
    assert calls.count('support') == (0 if point == 'before_review' else 1)
    assert research.execution.read_checkpoint(pages.PROTOCOL + ':result') is None
