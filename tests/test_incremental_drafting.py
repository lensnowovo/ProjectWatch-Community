"""Incremental section drafting over real snapshot and publication storage; synthetic, no network."""
import json
from contextlib import closing
from copy import deepcopy
from types import SimpleNamespace

import collection_fixtures
import pytest
from synthetic_providers import legacy_job
from test_collection_ingest import trial

from project_watch.studio import analyze_job as a
from project_watch.studio import incremental_drafting as inc
from project_watch.studio import judgment_delivery as delivery
from project_watch.studio import section_analysis
from project_watch.studio.execution import JobExecution
from project_watch.studio.llm import ModelError
from project_watch.studio.store import digest

TARGETS = ('operations', 'chain')
RETIRED = [s for s in section_analysis.SECTIONS if s not in TARGETS]  # keep the fixture to two sections
SMALL = {'materials': 2, 'chars': 64000}  # two fragments per draft request


class Research:
    """One project; each round ingests trials, captures its batch snapshot and runs one analyze job."""

    def __init__(self, tmp_path, monkeypatch, *, judgment=False):
        self.research, self.unit, self.ingest = collection_fixtures.host(tmp_path)
        self.pipeline, self.store, self.pid = self.research.pipeline, self.research.store, self.research.project_id
        self.judgment, self.n, self.requests, self.reviews = judgment, 0, [], []
        monkeypatch.setattr(a, '_reviewer', self._reviewer)

    def add(self, count):
        trials = [trial(url=f'https://clinicaltrials.gov/study/NCT0000{self.n + i:04}') for i in range(count)]
        for i, item in enumerate(trials):
            item['extra'] = {**item['extra'], 'source_identity': f'clinical:NCT{self.n + i:08}',
                             'nct_id': f'NCT{self.n + i:08}'}
        self.n += count
        self.ingest(self.unit | {'id': f'unit-{self.n}'}, trials)
        with closing(self.store._connect()) as db, db:
            db.execute("UPDATE studio_items SET triaged=1,relevant=1,category='trial',importance='major' "
                       'WHERE project_id=?', (self.pid,))

    def job(self, batch, *, kind='collect', fresh=True):
        job = legacy_job(self.store, self.pid, 'analyze', status='running', input_version=f'{kind}:{batch}')
        execution = JobExecution(self.store, job)
        execution.checkpoint(a.REVIEW_PROTOCOL, {'version': 4})
        execution.checkpoint(a.SECTION_SCOPE, {'retired': RETIRED})
        execution.checkpoint(a.DRAFT_BATCH, SMALL)
        execution.checkpoint(delivery.MODE, {'enabled': self.judgment, 'independent_pages': self.judgment})
        inc.freeze(execution, batch, fresh=fresh)
        return execution

    def context(self, execution, batch):
        from project_watch.studio import material_snapshot
        snapshot = a.snapshot_for(execution, batch)
        return material_snapshot.read(self.store, self.pid, snapshot['id'])

    def draft(self, fail_at=None):
        """A cumulative drafter: keeps the previous draft's points and adds one citing each new material."""
        start = len(self.requests)

        def respond(system, user, **kwargs):
            payload = json.loads(user)
            self.requests.append(payload)
            if fail_at is not None and len(self.requests) - start == fail_at:
                raise ModelError('synthetic lost draft', code='request_outcome_unknown')
            previous = payload.get('previous_draft') or {}
            draft = {}
            for name in payload['requested_sections']:
                points = list((previous.get(name) or {}).get('for') or [])
                points += [dict(text='合成材料记录招募状态。', refs=[code]) for code in payload['materials']]
                draft[name] = dict(verdict='steady', confidence='low', summary='合成材料仍在招募阶段。',
                                   **{'for': points, 'against': [], 'change_if': []})
            return draft
        return SimpleNamespace(json=respond, ready=True)

    def _reviewer(self, _pipeline, _execution, _llm, guard):
        def review(system, payload):
            guard()
            if 'conflicts' in system:
                return dict(consistent=True, reason='synthetic', conflicts=[])
            ref = next(iter(payload['evidence']))
            return dict(checks=[dict(field=f, status='supported', reason='synthetic source match',
                                     evidence=[dict(ref=ref, quote=payload['evidence'][ref]['context'][:12])])
                                for f in payload['fields']])

        def prepare(payload):
            self.reviews.append(set(payload['evidence']))
            return {'issues': [], 'receipts': []}
        review.prepare = prepare
        return review

    def run(self, batch, *, affected=TARGETS, fail_at=None, kind='collect', fresh=True):
        execution = self.job(batch, kind=kind, fresh=fresh)
        context = self.context(execution, batch)
        start = len(self.requests)
        publication = a.sections(self.pipeline, execution, self.draft(fail_at), context,
                                 affected=None if kind == 'redo' else set(affected))
        return execution, publication, self.requests[start:]

    def head(self, name):
        with closing(self.store._connect()) as db:
            row = db.execute('SELECT v.record FROM studio_insight_heads h JOIN studio_insight_versions v '
                             'ON v.id=h.version_id WHERE h.project_id=? AND h.artifact_key=?',
                             (self.pid, section_analysis.PREFIX + name)).fetchone()
        return json.loads(row[0]) if row else None


def drafted(requests):
    return [code for payload in requests for code in payload['materials']]


def evidence_of(requests):
    return {payload['materials'][code]['evidence_version_id'] for payload in requests for code in payload['materials']}


@pytest.fixture(params=[False, True], ids=['single-review', 'judgment-delivery'])
def project(tmp_path, monkeypatch, request):
    return Research(tmp_path, monkeypatch, judgment=request.param)


def test_second_round_drafts_only_new_material_on_top_of_the_published_version(project):
    project.add(3)
    first, published, requests = project.run('b1')
    assert first.read_checkpoint(inc.PLAN)['reason'].startswith('never_published:')  # requirement 4
    assert set(published['published']) == set(TARGETS)
    old = {name: project.head(name) for name in TARGETS}
    assert all(r['traversal'] == {'version': 1, 'drafted': 3, 'mode': 'full', 'carried': digest([])}
               for r in old.values())
    first_evidence = evidence_of(requests)

    project.add(2)
    second, published, requests = project.run('b2')
    plan = second.read_checkpoint(inc.PLAN)
    assert plan['mode'] == 'incremental' and len(plan['carried']) == 3
    # Only the two new trials are drafted; the first payload revises the published version.
    assert len(drafted(requests)) == 2 and not evidence_of(requests) & first_evidence
    seed = requests[0]['previous_draft']
    assert set(seed) == set(TARGETS)
    selection = section_analysis.numbered(a.frozen_selection(second, project.pid, project.context(second, 'b2')))
    for name in TARGETS:
        cited = {ref for point in seed[name]['for'] for ref in point['refs']}
        assert cited and cited.isdisjoint(requests[0]['materials'])
        # Each carried point still cites the same source text under its new code.
        before = {(s['evidence_version_id'], s['text']) for s in old[name]['numbered'].values()}
        assert {(selection[c]['evidence_version_id'], selection[c]['text']) for c in cited} == before
    # Review still checks counterevidence against the whole frozen selection (requirement 5).
    assert project.reviews and project.reviews[-1] >= set(selection)
    coverage = published['coverage']
    assert coverage['mode'] == 'incremental' and coverage['carried_fragments'] == 3
    assert coverage['new_fragments'] == 2 and coverage['drafted_fragments'] == 2
    assert coverage['processed_fragments'] == coverage['fragments'] == 5 and coverage['remaining_fragments'] == 0
    assert coverage['seeded_sections']['chain']['version_id'] == old['chain']['id']
    assert set(published['published']) == set(TARGETS)
    new = project.head('chain')
    assert new['traversal']['mode'] == 'incremental' and new['traversal']['drafted'] == 2
    assert len(new['numbered']) == 5  # the carried points and the new ones
    everything = a.frozen_selection(second, project.pid, project.context(second, 'b2'))['materials']
    assert inc.covered_fragments(project.store, new) == {
        (m['evidence_version_id'], m['fragment']['part'], m['fragment']['offset']) for m in everything}


def test_a_blocked_round_leaves_its_tail_for_the_next_round(project):
    project.add(2)
    project.run('b1')
    project.add(6)
    # The second new-material request is lost: two of six new fragments were drafted.
    blocked, publication, requests = project.run('b2', fail_at=2)
    assert len(requests) == 2 and blocked.read_checkpoint(a.COVERAGE)['blocked']
    assert publication['coverage']['remaining_fragments'] == 4 and publication['coverage']['drafted_fragments'] == 2
    record = project.head('chain')
    # Published from the one new draft that was kept: its record covers 2 carried + 2 drafted.
    assert record['job_id'] == blocked.job['id'] and record['traversal']['drafted'] == 2
    assert len(inc.covered_fragments(project.store, record)) == 4
    tail = {code for payload in requests[1:] for code in payload['materials']}
    tail_evidence = {requests[1]['materials'][c]['evidence_version_id'] for c in tail}
    project.add(0)
    _, publication, requests = project.run('b3')
    # The four untraversed fragments are read anew, including the one whose request was lost.
    assert len(drafted(requests)) == 4 and tail_evidence <= evidence_of(requests)
    assert publication['coverage']['carried_fragments'] == 4


def test_blocked_before_any_new_material_keeps_the_published_version_without_review(project):
    project.add(2)
    project.run('b1')
    before = {name: project.head(name)['id'] for name in TARGETS}
    project.add(2)
    reviews = len(project.reviews)
    _, publication, requests = project.run('b2', fail_at=1)
    assert len(requests) == 1 and len(project.reviews) == reviews  # the seed is not reviewed again
    assert {name: project.head(name)['id'] for name in TARGETS} == before
    assert publication['status'] == 'partial' and publication['published'] == []
    # The published versions stand; they are kept, not lost.
    assert all(publication['sections'][n]['status'] == 'unchanged' for n in TARGETS)
    _, publication, requests = project.run('b3')
    assert len(drafted(requests)) == 2  # nothing new was covered, so it is all read again


def test_no_new_material_keeps_intact_versions_without_a_request(project):
    project.add(2)
    project.run('b1')
    before = {name: project.head(name)['id'] for name in TARGETS}
    _, publication, requests = project.run('b2')
    assert requests == [] and publication['status'] == 'completed'
    assert all(publication['sections'][n]['status'] == 'unchanged' for n in TARGETS)
    assert {name: project.head(name)['id'] for name in TARGETS} == before


def test_a_section_never_published_makes_the_round_a_full_traversal(project):
    project.add(2)
    project.run('b1', affected=('chain',))
    with closing(project.store._connect()) as db, db:
        db.execute('DELETE FROM studio_insight_heads WHERE artifact_key=?', (section_analysis.PREFIX + 'operations',))
    project.add(1)
    execution, _, requests = project.run('b2')
    assert execution.read_checkpoint(inc.PLAN) == {'mode': 'full', 'reason': 'never_published:operations',
                                                   'targets': list(TARGETS)}
    assert len(drafted(requests)) == 3 and 'previous_draft' not in requests[0]



def _finish(project, execution, days_ago=0):
    from datetime import UTC, datetime, timedelta
    with closing(project.store._connect()) as db, db:
        db.execute("UPDATE studio_jobs SET status='done',finished_at=? WHERE id=?",
                   ((datetime.now(UTC) - timedelta(days=days_ago)).isoformat(), execution.job['id']))


def test_a_section_rejected_after_a_full_read_waits_instead_of_forcing_every_round_full(project, monkeypatch):
    plain = a._reviewer
    def rejecting(*args):
        review = plain(*args)
        def strict(system, payload):
            result = review(system, payload)
            if 'checks' in result:
                for row in result['checks']:
                    if row['field'].startswith('operations'):
                        row.update(status='insufficient', evidence=[])
            return result
        strict.prepare = review.prepare
        return strict
    project.add(2)
    with monkeypatch.context() as m:
        m.setattr(a, '_reviewer', rejecting)
        first, publication, _ = project.run('b1')
    assert publication['published'] == ['chain'] and project.head('operations') is None
    _finish(project, first)
    project.add(1)
    second, publication, requests = project.run('b2')
    plan = second.read_checkpoint(inc.PLAN)
    # Live: one never-published section made every round re-read all 1,840 fragments.
    assert plan['mode'] == 'incremental' and plan['targets'] == ['chain']
    assert list(plan['deferred_sections']) == ['operations']
    assert len(drafted(requests)) == 1 and publication['coverage']['deferred_sections']
    _finish(project, second)
    project.add(1)
    third, _, requests = project.run('b3')
    assert third.read_checkpoint(inc.PLAN)['mode'] == 'incremental'
    _finish(project, third)
    _finish(project, first, days_ago=inc.FULL_RETRY_DAYS + 1)
    with closing(project.store._connect()) as db, db:
        # Only the old full read drafted operations; the incremental rounds left it out.
        db.execute("UPDATE studio_jobs SET finished_at=? WHERE id IN (?,?)",
                   ('2000-01-01T00:00:00+00:00', second.job['id'], third.job['id']))
    project.add(1)
    fourth, _, requests = project.run('b4')
    assert fourth.read_checkpoint(inc.PLAN)['reason'] == 'never_published:operations'
    assert len(drafted(requests)) == 5


def test_a_full_read_for_another_reason_still_drafts_a_deferred_section(project, monkeypatch):
    plain = a._reviewer
    def rejecting(*args):
        review = plain(*args)
        def strict(system, payload):
            result = review(system, payload)
            for row in result.get('checks', ()):
                if row['field'].startswith('operations'):
                    row.update(status='insufficient', evidence=[])
            return result
        strict.prepare = review.prepare
        return strict
    project.add(2)
    with monkeypatch.context() as m:
        m.setattr(a, '_reviewer', rejecting)
        first, _, _ = project.run('b1')
    _finish(project, first)
    monkeypatch.setattr(inc, 'covered_fragments', lambda store, record: None)
    project.add(1)
    second, _, requests = project.run('b2')
    plan = second.read_checkpoint(inc.PLAN)
    assert plan['mode'] == 'full' and plan['targets'] == list(TARGETS)
    assert len(drafted(requests)) == 3


@pytest.mark.parametrize('outcome', [{'status': 'blocked', 'issues': ['request_outcome_unknown']},
                                     {'status': 'rejected', 'issues': ['section_normalization_failed']}],
                         ids=['unclear-request', 'unusable-reply'])
def test_a_section_not_rejected_by_review_is_read_in_full_again(project, monkeypatch, outcome):
    project.add(2)
    first, _, _ = project.run('b1', affected=('chain',))
    with closing(project.store._connect()) as db, db:
        db.execute('DELETE FROM studio_insight_heads WHERE artifact_key=?', (section_analysis.PREFIX + 'operations',))
    publication = first.read_checkpoint(a.PUBLICATION)
    publication['sections']['operations'] = outcome
    publication['published'] = ['chain']
    first.checkpoint_chunked(a.PUBLICATION, publication)
    _finish(project, first)
    project.add(1)
    second, _, _ = project.run('b2')
    assert second.read_checkpoint(inc.PLAN)['reason'] == 'never_published:operations'

def test_a_version_without_a_traversal_record_is_not_a_seed(project, monkeypatch):
    with monkeypatch.context() as published_before_the_upgrade:
        published_before_the_upgrade.setattr(inc, 'traversal', lambda execution: None)
        project.add(2)
        project.run('b1')
    assert 'traversal' not in project.head('chain')
    project.add(1)
    execution, _, requests = project.run('b2')
    assert execution.read_checkpoint(inc.PLAN)['reason'].startswith('coverage_unknown:')
    assert len(drafted(requests)) == 3


def test_owner_reanalysis_and_jobs_already_under_way_keep_the_full_traversal(project):
    project.add(2)
    project.run('b1')
    project.add(1)
    redo, _, requests = project.run('b2', kind='redo')
    assert redo.read_checkpoint(inc.MODE) == {'enabled': False} and len(drafted(requests)) == 3
    assert 'previous_draft' not in requests[0]  # the full traversal's first request is unchanged
    old, _, requests = project.run('b3', fresh=False)  # PROTOCOL predates this job's start
    assert old.read_checkpoint(inc.MODE) == {'enabled': False} and old.read_checkpoint(inc.PLAN) is None
    assert len(drafted(requests)) == 3


def test_a_retry_of_a_batch_drafted_in_full_stays_full(project):
    project.add(2)
    first = project.job('b1', fresh=False)  # an analysis of this batch that started before the upgrade
    assert first.read_checkpoint(inc.MODE) == {'enabled': False}
    retry = project.job('b1')
    assert retry.read_checkpoint(inc.MODE) == {'enabled': False}
    again = project.job('b9')
    assert again.read_checkpoint(inc.MODE) == {'enabled': True}


def test_a_retry_follows_the_earlier_plan_and_never_sidesteps_its_unknown_request(project):
    project.add(2)
    project.run('b1')
    project.add(3)
    first, _, _ = project.run('b2', fail_at=1)
    plan = first.read_checkpoint(inc.PLAN)
    retry = project.job('b2')
    context = project.context(retry, 'b2')
    selected = a.frozen_selection(retry, project.pid, context)
    sources = section_analysis.numbered(selected)
    assert inc.plan(retry, context, selected, sources, list(TARGETS)) == plan
    # A sibling with an unresolved request and a different selection blocks a new plan.
    with closing(project.store._connect()) as db, db:
        db.execute("INSERT INTO studio_job_calls(job_id,call_key,kind,provider,status,created_at) "
                   "VALUES (?,?,?,?,?,datetime('now'))", (first.job['id'], 'lost', 'model', 'synthetic', 'unknown'))
    other = project.job('b2')
    shorter = {**selected, 'materials': selected['materials'][1:]}
    with pytest.raises(ModelError, match='未改用不同请求'):
        inc.plan(other, context, shorter, section_analysis.numbered(shorter), list(TARGETS))


def test_renumbering_rewrites_every_reference_and_drops_lost_citations():
    sources = {'S1': {'evidence_version_id': 'e2', 'text': 'B'}, 'S2': {'evidence_version_id': 'e1', 'text': 'A'}}
    numbered = {'S1': {'evidence_version_id': 'e1', 'text': 'A'}, 'S2': {'evidence_version_id': 'e2', 'text': 'B'},
                'S3': {'evidence_version_id': 'gone', 'text': 'C'}}
    raw = {'summary': 's', 'refs': ['S1', 'S3'], 'for': [{'text': 'a', 'refs': ['S1', 'S2']},
                                                         {'text': 'lost', 'refs': ['S3']},
                                                         {'text': 'hidden', 'refs': ['S2']}],
           'competition_signals': [{'peer': 'p', 'event': 'e', 'refs': ['S2', 'S2']}], 'change_if': ['x']}
    seed, stats = inc.renumber(deepcopy(raw), numbered, sources, omitted=['hidden'])
    assert seed == {'summary': 's', 'refs': ['S2'], 'for': [{'text': 'a', 'refs': ['S2', 'S1']}],
                    'competition_signals': [{'peer': 'p', 'event': 'e', 'refs': ['S1']}], 'change_if': ['x']}
    assert stats == {'kept_items': 2, 'dropped_items': 1, 'dropped_refs': 2}


def test_a_stale_head_is_reviewed_again_even_without_new_material(project, monkeypatch):
    project.add(2)
    project.run('b1')
    before = project.head('chain')['id']
    real = project.store.evidence.dependencies.get
    monkeypatch.setattr(project.store.evidence.dependencies, 'get', lambda pid, key, **kw: {
        **real(pid, key, **kw), 'status': 'stale', 'stale_reason': 'source_revised',
        'stale_reasons': ['source_revised']})
    execution, publication, requests = project.run('b2')
    assert requests == [] and execution.read_checkpoint(inc.PLAN)['mode'] == 'incremental'
    assert project.head('chain')['id'] != before and 'chain' in publication['published']
    monkeypatch.setattr(project.store.evidence.dependencies, 'get', lambda pid, key, **kw: {
        **real(pid, key, **kw), 'status': 'stale', 'stale_reason': 'event_revised',
        'stale_reasons': ['event_revised']})
    project.add(1)
    execution, _, requests = project.run('b3')
    assert execution.read_checkpoint(inc.PLAN)['reason'] == 'not_current:operations'
    assert len(drafted(requests)) == 3


def test_a_traversal_record_that_does_not_match_its_job_is_not_trusted(project):
    project.add(3)
    first, _, _ = project.run('b1', fail_at=2)  # one of two requests kept: 2 of 3 fragments drafted
    record = project.head('chain')
    assert len(inc.covered_fragments(project.store, record)) == 2
    for wrong in ({'mode': 'incremental'}, {'carried': digest([0])}, {'drafted': 4}, {'version': 2}):
        assert inc.covered_fragments(project.store, {**record, 'traversal': {**record['traversal'], **wrong}}) is None
    assert inc.covered_fragments(project.store, {**record, 'snapshot_id': 'other'}) is None
    assert first.read_checkpoint(a.COVERAGE)['remaining_fragments'] == 1


def test_a_lagging_section_holds_the_carried_set_back_and_says_so(project):
    project.add(2)
    project.run('b1')
    project.add(2)
    project.run('b2', affected=('chain',))  # only chain moves on to cover four fragments
    assert len(inc.covered_fragments(project.store, project.head('chain'))) == 4
    assert len(inc.covered_fragments(project.store, project.head('operations'))) == 2
    project.add(2)
    third, publication, requests = project.run('b3')
    plan = third.read_checkpoint(inc.PLAN)
    # Nothing either section has not read is skipped: operations still reads b2's material.
    assert plan['mode'] == 'incremental' and len(plan['carried']) == 2 and len(drafted(requests)) == 4
    assert plan['lagging_sections'] == ['operations']
    assert publication['coverage']['lagging_sections'] == ['operations']


def test_a_resumed_job_keeps_its_frozen_targets_when_relevance_changes(project):
    project.add(2)
    project.run('b1')
    project.add(4)
    execution = project.job('b2')
    context = project.context(execution, 'b2')
    drafter = project.draft()
    calls = []
    def crash_on_second(system, user, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError('synthetic worker crash')
        return drafter.json(system, user, **kwargs)
    with pytest.raises(RuntimeError):
        a.sections(project.pipeline, execution, SimpleNamespace(json=crash_on_second, ready=True), context,
                   affected=set(TARGETS))
    sent = len(project.requests)
    # On resume no section is affected any more; the frozen plan still finishes the round.
    publication = a.sections(project.pipeline, execution, project.draft(), context, affected=set())
    assert publication['targets'] == list(TARGETS) and set(publication['published']) == set(TARGETS)
    assert len(project.requests) > sent and publication['coverage']['drafted_fragments'] == 4


def test_a_section_citing_excluded_material_is_not_a_seed(project, monkeypatch):
    project.add(2)
    project.run('b1')
    project.add(2)
    chain = project.head('chain')['id']
    original = inc.seed_permitted
    monkeypatch.setattr(inc, 'seed_permitted', lambda db, pid, record: record['id'] != chain and original(db, pid, record))
    second, _publication, requests = project.run('b2')
    assert second.read_checkpoint(inc.PLAN)['reason'] == 'not_permitted:chain'
    assert len(drafted(requests)) == 4
