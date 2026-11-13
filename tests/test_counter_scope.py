"""Relevant counterevidence scope, shared page traversals and concurrent requests; synthetic, offline."""
import json
import threading
from types import SimpleNamespace

import pytest
from test_review_source_traversal import Journal, payload

from project_watch.studio import review_source_traversal as r

CLAIM = '联影医疗前三季度海外收入29.96亿元'


def corpus(filler=200):
    evidence = {f'S{i}': {'title': f'资讯 {i}', 'context': f'普通行业动态第{i}条。' * 60} for i in range(filler)}
    evidence['S7'] = {'title': '联影医疗三季报', 'context': '联影医疗2026年前三季度营收105.8亿元，海外收入29.96亿元。'}
    evidence['S150'] = {'title': '海外收入更正', 'context': '更正：联影医疗前三季度海外收入为27.1亿元。'}
    return evidence


def counter_payload(evidence, fields=None):
    fields = fields or {'claim.0': CLAIM}
    return {'fields': fields, 'analysis': {f: {'text': t, 'refs': ['S7']} for f, t in fields.items()},
            'page_contract': 'synthetic', 'evidence': evidence}


def relevant_journal():
    journal = Journal()
    journal.saved[r.SCOPE] = {'mode': 'relevant', 'version': 1}
    return journal


def counting_model(sent):
    def model(system, user, **kwargs):
        body = json.loads(user)
        sent.append(set(body['evidence']))
        return {'checked_fields': list(body['fields']), 'checks': []}
    return model


def test_relevant_scope_keeps_the_cited_and_the_possibly_contradicting_source():
    chosen = r.relevant_evidence(counter_payload(corpus()))
    assert set(chosen) == {'S7', 'S150'}  # the cited report and the correction; no unrelated filler


def test_relevant_scope_is_bounded_per_assertion():
    evidence = {f'S{i}': {'title': f'海外收入 {i}', 'context': f'联影医疗海外收入第{i}项说明。' * 40} for i in range(300)}
    fields = {f'claim.{i}': f'{CLAIM}，第{i}项' for i in range(5)}
    chosen = r.relevant_evidence({**counter_payload(evidence, fields), 'analysis': {}})
    assert 0 < len(chosen) <= r.PER_FIELD * len(fields)
    assert len(json.dumps(chosen, ensure_ascii=False)) <= r.FIELD_CHARS * len(fields)
    assert r.relevant_evidence({**counter_payload(evidence, fields), 'analysis': {}}) == chosen  # deterministic


def test_a_long_filing_that_best_matches_an_assertion_stays_in_scope():
    evidence = {'NOTE': {'title': '简讯', 'context': '联影医疗海外收入29.96亿元。'},
                'REPORT': {'title': '联影医疗年度报告', 'context': '年度报告正文。' * 12000 + '海外收入27.10亿元，前三季度。'}}
    evidence.update({f'F{i}': {'title': '天气', 'context': '晴。'} for i in range(20)})
    chosen = r.relevant_evidence({'fields': {'claim.0': CLAIM}, 'analysis': {}, 'evidence': evidence})
    assert 'REPORT' in chosen or 'NOTE' in chosen
    best = r.relevant_evidence({'fields': {'claim.0': '联影医疗年度报告海外收入前三季度'}, 'analysis': {},
                                'evidence': evidence})
    assert 'REPORT' in best  # longer than a field's budget, still checked


def test_packing_many_assertions_keeps_each_ones_contradicting_source():
    """Review finding: a shared budget let 30 packed assertions keep only 5 of their 30 contradictions."""
    evidence = {f'F{i}': {'title': f'行业资讯{i}', 'context': '医疗设备行业常规动态说明。' * 400} for i in range(120)}
    fields = {}
    for i in range(30):
        product = f'型号{chr(0x4e00 + 37 * i)}{chr(0x4e00 + 41 * i + 7)}{chr(0x4e00 + 43 * i + 11)}'
        fields[f'claim.{i}'] = f'{product}已获准上市销售'
        evidence[f'C{i}'] = {'title': f'{product}更正', 'context': f'更正：{product}尚未获准上市。' + '补充说明。' * 800}
    chosen = r.relevant_evidence({'fields': fields, 'analysis': {}, 'evidence': evidence})
    assert {f'C{i}' for i in range(30)} <= set(chosen)


def test_a_relevant_job_sends_a_few_requests_instead_of_the_whole_corpus():
    full, relevant = [], []
    r.counterevidence(Journal(), SimpleNamespace(json=counting_model(full)), counter_payload(corpus()), lambda: None)
    r.counterevidence(relevant_journal(), SimpleNamespace(json=counting_model(relevant)), counter_payload(corpus()),
                      lambda: None)
    assert len(full) > 1 and set().union(*full) == set(corpus())  # a job without the scope still reads every source
    assert len(relevant) == 1 and relevant[0] == {'S7', 'S150'}


def test_no_source_sharing_a_term_sends_no_counter_request():
    sent = []
    evidence = {f'S{i}': {'title': '无关', 'context': '天气晴朗。'} for i in range(5)}
    result = r.counterevidence(relevant_journal(), SimpleNamespace(json=counting_model(sent)),
                               {**counter_payload(evidence), 'analysis': {}}, lambda: None)
    assert result == {'issues': [], 'receipts': []} and sent == []


def test_scope_is_decided_once_and_a_job_that_already_sent_keeps_the_full_corpus(tmp_path):
    from project_watch.repository import ProjectRepository
    from project_watch.studio.execution import JobExecution
    from project_watch.studio.store import StudioStore, now

    repository = ProjectRepository(tmp_path / 'scope.sqlite3')
    store = StudioStore(repository.db_path)
    pid = repository.create_project('Synthetic Scope').id
    store.upsert_project(pid, {}, consent=True)
    fresh, started = (store.enqueue(pid, kind) for kind in ('analyze', 'page_summary'))
    fresh, started = store.claim(), store.claim()
    with store._connect() as db:
        db.execute("INSERT INTO studio_job_calls(job_id,call_key,kind,provider,status,created_at) "
                   "VALUES (?,?,?,?,?,?)", (started['id'], 'synthetic', 'model', 'p', 'completed', now()))
    for job, expected in ((fresh, True), (started, False)):
        execution = JobExecution(store, job)
        r.freeze_scope(execution)
        assert r.relevant_scope(execution) is expected
    with store._connect() as db:  # a later request never flips a decided job
        db.execute("INSERT INTO studio_job_calls(job_id,call_key,kind,provider,status,created_at) "
                   "VALUES (?,?,?,?,?,?)", (fresh['id'], 'synthetic', 'model', 'p', 'completed', now()))
    execution = JobExecution(store, fresh)
    r.freeze_scope(execution)
    assert r.relevant_scope(execution)


def test_independent_review_requests_overlap_up_to_the_parallel_limit():
    # Waits for overlap instead of a fixed sleep, so a loaded machine cannot
    # make the requests miss each other.
    cond, active, peak, calls = threading.Condition(), [0], [0], [0]
    def model(system, user, **kwargs):
        with cond:
            calls[0] += 1
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            cond.notify_all()
            if calls[0] > 1:  # the first request runs alone
                cond.wait_for(lambda: peak[0] >= r.PARALLEL, timeout=5)
            active[0] -= 1
        body = json.loads(user)
        ref, source = next(iter(body['evidence'].items()))
        return {'checks': [{'field': f, 'status': 'supported', 'inference': 'direct', 'reason': 'synthetic',
                            'evidence': [{'ref': ref, 'quote': source['context'][:30]}]} for f in body['fields']]}
    r.run(Journal(), SimpleNamespace(json=model), 'system', payload(), lambda: None)
    assert peak[0] == r.PARALLEL  # the first request alone, then the rest together


@pytest.fixture
def host(tmp_path):
    import test_page_summaries as fixtures
    return fixtures.host.__wrapped__(tmp_path)


def test_pages_share_one_counter_traversal_and_reuse_verdicts(host, monkeypatch):
    from test_page_summaries import draft, reviewer
    from test_page_summary_delivery import install

    from project_watch.studio import page_summaries as pages

    traversals = []
    def check(system, data):
        return reviewer(system, data)
    def prepare(data):
        traversals.append(sorted({row.get('page') for row in data['analysis'].values()}, key=str))
        return {'issues': [], 'receipts': []}
    check.prepare = prepare
    def page_draft(page):
        value = draft()
        for row in value['sentences'] + value['details']:
            row['text'] = page + ' ' + row['text']  # distinct sentences per page
        return value
    research = install(host, monkeypatch, lambda system, data, **k: {p: page_draft(p) for p in json.loads(data)['page_contracts']},
                       check, batches=2)
    result = pages.run(research.pipeline, research.execution, targets=('trials', 'papers'))
    assert set(result['published']) == {'trials', 'papers'}
    assert traversals[0] == ['papers', 'trials']  # both pages' sentences in one traversal
    assert len(traversals) == 1  # the same sentences later in the job reuse the saved verdict
