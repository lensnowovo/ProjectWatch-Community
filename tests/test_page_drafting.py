"""Whole-project page summaries that read only unextracted material; real storage, synthetic model, no network."""
import json
import threading
import time
from types import SimpleNamespace

import pytest
from synthetic_providers import legacy_job
from test_incremental_drafting import Research

from project_watch.studio import analyze_job as a
from project_watch.studio import page_drafting as pd
from project_watch.studio import page_summaries as pages
from project_watch.studio.execution import JobExecution
from project_watch.studio.llm import ModelError

EXTRACT, MERGE, SYNTHESIS = '开篇摘要准备素材', '整理某一页开篇摘要的候选事实', '本页候选事实'


class Summaries(Research):
    """The analysis harness's project, read by whole-project page summary jobs."""

    def __init__(self, tmp_path, monkeypatch):
        super().__init__(tmp_path, monkeypatch)
        self.calls, self.fail, self.in_flight, self.peak = [], set(), 0, 0
        self.lock = threading.Lock()
        monkeypatch.setattr(pages, 'select_context', lambda store, pid, context, targets: a.select(store, pid, context))
        monkeypatch.setattr(a, 'MAX_MATERIALS', 2)  # two materials per extraction request
        monkeypatch.setattr(self.pipeline, 'tools', lambda **kw: (None, None, SimpleNamespace(json=self.respond), None))

    def respond(self, system, user, **kwargs):
        payload = json.loads(user)
        kind = EXTRACT if EXTRACT in system else MERGE if MERGE in system else SYNTHESIS if SYNTHESIS in system else 'draft'
        with self.lock:
            self.calls.append((kind, payload))
            number = len(self.calls)
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
        try:
            time.sleep(0.05)  # long enough for requests sent together to overlap
            if (kind, number) in self.fail or (kind, None) in self.fail:
                raise ModelError('synthetic lost request', code='request_outcome_unknown')
            if kind == EXTRACT:
                return {'points': [dict(section='trials', field='facts', text=f'{code} 记录了试验招募状态。', refs=[code],
                                        weight='high') for code in payload['materials']]}
            if kind == MERGE:
                return {'points': [dict(section=page, field=field, text=p['text'], refs=p['refs'], weight=p['weight'])
                                   for page, fields in payload['candidates'].items()
                                   for field, points in fields.items() for p in points[:3]]}
            if kind == SYNTHESIS:
                [page] = payload['page_contracts']
                refs = [ref for point in payload['candidates'] for ref in point['refs']]
                return {page: dict(status='ready', sentences=[dict(text='合成试验仍在招募受试者。', refs=refs[:1]),
                                                              dict(text='研究处于入组阶段。', refs=refs[:1])],
                                   details=[dict(text=f'{ref} 登记为招募中。', refs=[ref]) for ref in refs[1:]])}
            raise AssertionError('a whole-project job sent a cumulative draft')
        finally:
            with self.lock:
                self.in_flight -= 1

    def _reviewer(self, _pipeline, _execution, _llm, guard):
        """Supports each field with its own first citation, so details stay in the publication."""
        def review(system, payload):
            guard()
            if 'conflicts' in system:
                return dict(consistent=True, reason='synthetic', conflicts=[])
            checks = []
            for field in payload['fields']:
                page, group, index, _ = field.split('.')
                ref = payload['analysis'][page][group][int(index)]['refs'][0]
                checks.append(dict(field=field, status='supported', reason='synthetic source match', standalone=True,
                                   evidence=[dict(ref=ref, quote=payload['evidence'][ref]['context'][:12])]))
            return dict(checks=checks)
        review.prepare = lambda payload: {'issues': [], 'receipts': []}
        return review

    def summarize(self, *, targets=None):
        job = legacy_job(self.store, self.pid, 'page_summary', status='running', input_version='analyze:x')
        execution = JobExecution(self.store, job)
        start = len(self.calls)
        result = pages.run(self.pipeline, execution, targets=targets)
        return execution, result, self.calls[start:]

    def shown(self):
        return pages.read(self.store, self.pid).get('trials')

    def summary_head(self):
        from contextlib import closing
        with closing(self.store._connect()) as db:
            row = db.execute('SELECT v.record FROM studio_insight_heads h JOIN studio_insight_versions v ON v.id=h.version_id '
                             "WHERE h.project_id=? AND h.artifact_key='page-summary:trials'", (self.pid,)).fetchone()
        return json.loads(row[0]) if row else None


def of(calls, kind):
    return [payload for k, payload in calls if k == kind]


def read(calls):
    return {payload['materials'][c]['evidence_version_id'] for payload in of(calls, EXTRACT) for c in payload['materials']}


def cited(record):
    return {record['numbered'][r]['evidence_version_id'] for row in record['raw']['sentences'] + record['raw']['details']
            for r in row['refs']}


@pytest.fixture
def project(tmp_path, monkeypatch):
    return Summaries(tmp_path, monkeypatch)


def test_a_later_job_reads_only_new_material_and_keeps_earlier_facts(project):
    project.add(4)
    first, result, calls = project.summarize()
    assert first.read_checkpoint(pd.MODE) == {'version': 1, 'enabled': True, 'prior': None}
    assert len(of(calls, EXTRACT)) == 2 and result['published'] == ['trials']
    assert result['pages']['papers'] == 'no_material'
    before = read(calls)
    project.add(2)
    second, result, calls = project.summarize()
    assert second.read_checkpoint(pd.MODE)['prior'] == first.job['id']
    assert len(of(calls, EXTRACT)) == 1 and not read(calls) & before  # only the two new trials
    [synthesis] = of(calls, SYNTHESIS)
    assert len(synthesis['candidates']) == 6  # facts from every trial, earlier ones carried
    assert result['published'] == ['trials'] and len(cited(project.summary_head())) == 6


def test_with_nothing_new_no_request_is_sent_and_the_publication_stands(project):
    project.add(2)
    project.summarize()
    head = project.summary_head()
    _, result, calls = project.summarize()
    assert calls == []
    assert result['pages']['trials'] == 'unchanged' and result['status'] == 'completed'
    assert project.summary_head()['id'] == head['id']


def test_extraction_requests_are_sent_several_at_a_time(project):
    project.add(12)
    _, _, calls = project.summarize()
    assert len(of(calls, EXTRACT)) == 6 and project.peak >= 2


def test_material_whose_extraction_failed_is_read_again_next_time(project):
    project.add(6)
    project.fail = {(EXTRACT, 2)}
    _, result, calls = project.summarize()
    lost = {payload['materials'][c]['evidence_version_id'] for payload in of(calls, EXTRACT)[1:2] for c in payload['materials']}
    assert result['published'] == ['trials'] and not cited(project.summary_head()) & lost
    project.fail = set()
    _, _, calls = project.summarize()
    assert read(calls) == lost
    assert lost <= cited(project.summary_head())


def test_a_failed_page_keeps_its_publication_and_is_written_next_time(project):
    project.add(2)
    project.summarize()
    head = project.summary_head()
    project.add(2)
    project.fail = {(SYNTHESIS, None)}
    _, result, _ = project.summarize()
    assert result['failures'] == {'trials': 'request_outcome_unknown'} and result['status'] == 'partial'
    assert project.summary_head()['id'] == head['id']
    project.fail = set()
    _, result, calls = project.summarize()
    assert not of(calls, EXTRACT) and len(of(calls, SYNTHESIS)) == 1  # facts carried, page written again
    assert result['published'] == ['trials'] and len(cited(project.summary_head())) == 4


def test_a_large_page_pool_is_merged_before_writing(project, monkeypatch):
    monkeypatch.setattr(pd, 'POOL_CHARS', 600)
    project.add(12)
    _, _, calls = project.summarize()
    assert of(calls, MERGE)
    [synthesis] = of(calls, SYNTHESIS)
    assert len(json.dumps(synthesis['candidates'], ensure_ascii=False)) <= pd.POOL_CHARS


def test_withdrawn_material_leaves_the_carried_facts(project):
    project.add(4)
    project.summarize()
    gone = sorted(cited(project.summary_head()))[0]
    project.pipeline.evidence.dependencies.withdraw(project.pid, gone, 'Synthetic withdrawal', actor='test')
    project.add(1)
    _, _, calls = project.summarize()
    [synthesis] = of(calls, SYNTHESIS)
    assert len(synthesis['candidates']) == 4
    assert gone not in cited(project.summary_head())


def test_single_page_and_competition_jobs_keep_the_cumulative_reading(project):
    project.add(2)
    calls = []

    def cumulative(system, user, **kwargs):
        calls.append(json.loads(user))
        return {}
    project.pipeline.tools = lambda **kw: (None, None, SimpleNamespace(json=cumulative), None)
    execution, _, _ = project.summarize(targets=('competition',))
    assert execution.read_checkpoint(pd.MODE)['enabled'] is False
    assert calls and 'previous_draft' in calls[0]


def test_a_job_that_could_not_read_all_material_is_partial(project):
    project.add(12)
    project.fail = {(EXTRACT, None)}
    _, result, calls = project.summarize()
    assert len(of(calls, EXTRACT)) <= 1 + 3  # sends stop after repeated failures
    assert result['status'] == 'partial' and result['draft_failure']['unread'] == 12


def test_a_carried_fact_that_lost_one_of_its_sources_is_dropped(project, monkeypatch):
    project.add(2)
    joined = project.respond

    def extract_together(system, user, **kwargs):
        value = joined(system, user, **kwargs)
        if EXTRACT in system:  # one fact drawing on both materials of the batch
            codes = list(json.loads(user)['materials'])
            value['points'] = [dict(section='trials', field='facts', text='两项试验均在招募。', refs=codes, weight='high')]
        return value
    monkeypatch.setattr(project.pipeline, 'tools', lambda **kw: (None, None, SimpleNamespace(json=extract_together), None))
    project.summarize()
    gone = sorted(cited(project.summary_head()))[0]
    project.pipeline.evidence.dependencies.withdraw(project.pid, gone, 'Synthetic withdrawal', actor='test')
    project.add(1)
    _, _, calls = project.summarize()
    [synthesis] = of(calls, SYNTHESIS)
    assert [p['text'] for p in synthesis['candidates']] == ['两项试验均在招募。']  # only the new batch's fact
    assert len(synthesis['candidates'][0]['refs']) == 1


def test_a_resumed_job_reports_the_page_it_published_without_new_requests(project):
    from contextlib import closing
    project.add(2)
    execution, _, _ = project.summarize()
    with closing(project.store._connect()) as db, db:  # as if the job yielded right after publishing
        db.execute('DELETE FROM studio_job_checkpoints WHERE job_id=? AND stage=?',
                   (execution.job['id'], pages.PROTOCOL + ':result'))
    sent = len(project.calls)
    again = pages.run(project.pipeline, execution)
    assert len(project.calls) == sent
    assert again['published'] == ['trials'] and again['pages']['trials'] == 'approved'
