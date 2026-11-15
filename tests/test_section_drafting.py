"""Extract-then-synthesize section drafting over real snapshot and publication storage; synthetic, no network."""
import json
import threading
import time
from types import SimpleNamespace

import pytest
from test_incremental_drafting import TARGETS, Research

from project_watch.studio import analyze_job as a
from project_watch.studio import incremental_drafting as inc
from project_watch.studio import section_analysis
from project_watch.studio import section_drafting as sd
from project_watch.studio.llm import ModelError

EXTRACT, MERGE, SYNTHESIS = '只是全部素材中的一批', 'candidates 是从同一家公司多批素材', '本次输入不是原文素材'


class Extracting(Research):
    """The same project harness, with analyses that start under the extract-then-synthesize protocol."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls, self.fail, self.bogus_ref, self.in_flight, self.peak = [], set(), False, 0, 0
        self.lock = threading.Lock()

    def job(self, batch, *, kind='collect', fresh=True):
        execution = super().job(batch, kind=kind, fresh=fresh)
        sd.freeze(execution, batch, fresh=fresh)
        return execution

    def draft(self, fail_at=None):
        def respond(system, user, **kwargs):
            payload = json.loads(user)
            kind = EXTRACT if EXTRACT in system else MERGE if MERGE in system else SYNTHESIS
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
                    return {'points': [dict(section=name, field='for', text=f'{code} 记录招募状态。', refs=[code],
                                            weight='high') for name in payload['fields'] for code in payload['materials']]}
                if kind == MERGE:
                    return {'points': [dict(section=name, field=field, text=p['text'], refs=p['refs'], weight=p['weight'])
                                       for name, fields in payload['candidates'].items()
                                       for field, points in fields.items() for p in points[:1]]}
                draft = {}
                for name in payload['requested_sections']:
                    points = [dict(text=p['text'], refs=p['refs']) for field in payload['candidates'].get(name, {}).values()
                              for p in field]
                    points += list(((payload.get('published') or {}).get(name) or {}).get('for') or [])
                    if self.bogus_ref:
                        points.append(dict(text='无来源的断言。', refs=['S999']))
                    draft[name] = dict(verdict='steady', confidence='low', summary='合成材料仍在招募阶段。',
                                       **{'for': points, 'against': [], 'change_if': []})
                return draft
            finally:
                with self.lock:
                    self.in_flight -= 1
        return SimpleNamespace(json=respond, ready=True)

    def of(self, kind):
        return [payload for k, payload in self.calls if k == kind]


@pytest.fixture
def project(tmp_path, monkeypatch):
    return Extracting(tmp_path, monkeypatch)


def cited(record):
    return {ref for point in record['raw']['for'] for ref in point['refs']}


def test_every_batch_informs_the_published_section_not_only_the_last(project):
    project.add(6)
    execution, publication, _ = project.run('b1')
    extractions = project.of(EXTRACT)
    assert len(extractions) == 3 and not any('previous_draft' in p for p in extractions)  # independent reads
    [synthesis] = project.of(SYNTHESIS)
    candidates = {ref for fields in synthesis['candidates'].values() for points in fields.values()
                  for point in points for ref in point['refs']}
    first_batch = set(extractions[0]['materials'])
    assert first_batch <= candidates and set(extractions[-1]['materials']) <= candidates
    assert set(publication['published']) == set(TARGETS)
    assert first_batch <= cited(project.head('chain'))  # the first batch survives into the published version
    coverage = execution.read_checkpoint(a.COVERAGE)
    assert coverage['drafted_fragments'] == 6 and coverage['remaining_fragments'] == 0
    assert not coverage.get('blocked')


def test_batches_are_read_several_at_a_time_after_the_first(project):
    project.add(10)
    project.run('b1')
    assert len(project.of(EXTRACT)) == 5 and project.peak >= 2


def test_an_incremental_round_weighs_new_candidates_with_the_published_version(project):
    project.add(3)
    project.run('b1')
    before = cited(project.head('chain'))
    project.add(2)
    start = len(project.calls)
    second, publication, _ = project.run('b2')
    assert second.read_checkpoint(inc.PLAN)['mode'] == 'incremental'
    calls = project.calls[start:]
    extracted = {code for kind, p in calls if kind == EXTRACT for code in p['materials']}
    assert len(extracted) == 2  # only the new material is read
    [synthesis] = [p for kind, p in calls if kind == SYNTHESIS]
    assert set(synthesis['published']) == set(TARGETS)
    assert set(publication['published']) == set(TARGETS)
    assert len(cited(project.head('chain'))) == len(before) + 2  # nothing published earlier was dropped


def test_a_lost_extraction_keeps_the_prefix_uses_later_batches_and_is_not_resent(project):
    project.add(6)
    project.fail = {(EXTRACT, 2)}  # the second batch's request (sent in the first parallel wave)
    execution, _, _ = project.run('b1')
    coverage = execution.read_checkpoint(a.COVERAGE)
    assert coverage['blocked'] and coverage['drafted_fragments'] == 2 and coverage['remaining_fragments'] == 4
    [synthesis] = project.of(SYNTHESIS)
    lost = set(project.of(EXTRACT)[1]['materials'])
    last = set(project.of(EXTRACT)[2]['materials'])
    in_synthesis = {ref for fields in synthesis['candidates'].values() for points in fields.values()
                    for point in points for ref in point['refs']}
    assert last <= in_synthesis and not lost & in_synthesis
    assert project.head('chain')['traversal']['drafted'] == 2  # a later round reads the rest again
    # Drafting the same snapshot again replays saved answers and never resends the lost request.
    sent = len(project.calls)
    selected = a.frozen_selection(execution, project.pid, project.context(execution, 'b1'))
    sd.draft(execution, project.draft(), 'SYSTEM', {}, section_analysis.numbered(selected), selected, list(TARGETS))
    assert len(project.calls) == sent


def test_after_repeated_failures_no_further_batch_is_sent(project):
    project.add(12)
    project.fail = {(EXTRACT, None)}
    project.run('b1')
    assert len(project.of(EXTRACT)) <= 1 + sd.PARALLEL  # the first alone, then at most one wave


def test_references_outside_the_candidates_are_dropped(project):
    project.add(2)
    project.bogus_ref = True
    project.run('b1')
    assert 'S999' not in cited(project.head('chain'))


def test_a_large_candidate_pool_is_merged_before_synthesis(project, monkeypatch):
    monkeypatch.setattr(sd, 'POOL_CHARS', 1000)
    project.add(12)
    project.run('b1')
    merges = project.of(MERGE)
    [synthesis] = project.of(SYNTHESIS)
    assert merges and len(json.dumps(synthesis['candidates'], ensure_ascii=False)) <= sd.POOL_CHARS


def test_without_a_synthesis_the_published_version_stands(project):
    project.add(2)
    project.run('b1')
    old = project.head('chain')
    project.add(2)
    project.fail = {(SYNTHESIS, None)}
    second, publication, _ = project.run('b2')
    coverage = second.read_checkpoint(a.COVERAGE)
    assert coverage['blocked'] and coverage['drafted_fragments'] == 0
    assert publication['published'] == [] and project.head('chain')['id'] == old['id']


def test_only_analyses_that_start_fresh_extract(project):
    project.add(1)
    assert sd.extracting(project.job('b1'))
    assert not sd.extracting(project.job('b2', fresh=False))
    started = Research.job(project, 'b3')
    started.checkpoint_chunked(a.DRAFT + ':0:x', {'draft': {}, 'fingerprint': 'x'})
    sd.freeze(started, 'b3', fresh=True)
    assert not sd.extracting(started)
    # A retry of a collection batch whose earlier analysis drafted cumulatively keeps drafting that way.
    retry = project.job('b3')
    assert not sd.extracting(retry)


def test_a_seed_that_stands_is_not_synthesized_again(project):
    project.add(2)
    project.run('b1')
    old = project.head('chain')
    project.add(6)
    start = len(project.calls)
    project.fail = {(EXTRACT, start + 1)}  # the first new batch is lost; later batches still answer
    second, publication, _ = project.run('b2')
    assert not [kind for kind, _ in project.calls[start:] if kind != EXTRACT]  # no merge or synthesis paid for
    assert second.read_checkpoint(a.COVERAGE)['drafted_fragments'] == 0
    assert publication['published'] == [] and project.head('chain')['id'] == old['id']


def test_a_truncated_synthesis_does_not_narrow_the_next_batch_size(project):
    from synthetic_providers import legacy_job

    from project_watch.studio.execution import JobExecution
    from project_watch.studio.step_outcomes import CHECKPOINT

    def job(status):
        return JobExecution(project.store, legacy_job(project.store, project.pid, 'analyze', status=status,
                                                      input_version='collect:b9'))
    first = job('running')
    first.checkpoint(a.DRAFT_BATCH, a.WIDE_BATCH)
    first.checkpoint_chunked(CHECKPOINT, {'draft:' + sd.SYNTHESIS + ':x': {'status': 'blocked',
                                                                            'code': 'model_output_truncated'}})
    retry = job('running')
    a.freeze_draft_batch(retry, 'b9', fresh=True)
    assert retry.read_checkpoint(a.DRAFT_BATCH) == a.WIDE_BATCH
