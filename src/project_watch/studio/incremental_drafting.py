"""Incremental section drafting for automatic analysis rounds.

An automatic round seeds each target section with its published version and drafts
only the fragments that version does not yet reflect. Review and publication are
unchanged: they still see the whole frozen selection. The mode, and the plan of seeds
and carried fragments, are frozen per job, so a job under way never changes its
requests, and a retry cannot sidestep an earlier unknown request with a different one.

A published version records which drafted prefix it was written from (``traversal``).
What it covers is what its job carried forward plus that prefix; a job that stopped
partway leaves its untraversed tail uncovered, so the next round reads it.
"""
import json
from contextlib import closing

from project_watch.studio.store import digest

MODE = 'analyze_incremental_v1'
PLAN = 'analyze_incremental_plan_v1'
TRAVERSAL_VERSION = 1
# Staleness a seed survives: a revised source's new version is new material, and the
# superseded version's citations drop out of the seed when it is renumbered. An object
# revision survives only when it replaced supporting quotes and left the identity alone
# (``objects_keep_identity``). Every reason since the last publish must qualify.
SEEDABLE_STALE = frozenset({'source_revised'})
_OBJECT_STALE = 'object_revised'


def objects_keep_identity(db, pid, artifact_key) -> bool:
    """Each object the published section was bound to differs from its bound version only in refs."""
    from project_watch.studio.dependencies import DependencyStore

    links = db.execute('SELECT object_id,object_version FROM studio_object_dependency_links '
                       'WHERE project_id=? AND artifact_key=?', (pid, artifact_key)).fetchall()
    for object_id, version in links:
        current = db.execute('SELECT record FROM studio_monitor_objects WHERE project_id=? AND id=?',
                             (pid, object_id)).fetchone()
        bound = db.execute('SELECT h.record FROM studio_object_history h JOIN studio_monitor_objects o ON o.id=h.object_id '
                           'WHERE o.project_id=? AND h.object_id=? AND h.version=?', (pid, object_id, version)).fetchone()
        if not current or not bound:
            return False
        now_, then = (DependencyStore._identity(json.loads(r[0])) for r in (current, bound))
        now_.pop('refs', None)
        then.pop('refs', None)
        if now_ != then:
            return False
    return True


def seedable(db, pid, artifact_key, dependency) -> bool:
    status = dependency.get('status')
    if status == 'current':
        return True
    reasons = dependency.get('stale_reasons')
    if status != 'stale' or not reasons:
        return False  # unknown history (recorded before reasons were kept) drafts in full once
    if not set(reasons) <= SEEDABLE_STALE | {_OBJECT_STALE}:
        return False
    return _OBJECT_STALE not in reasons or objects_keep_identity(db, pid, artifact_key)


def _siblings(execution, batch, db):
    return [row[0] for row in db.execute(
        "SELECT id FROM studio_jobs WHERE project_id=? AND kind='analyze' AND input_version IN (?,?) AND id<>?",
        (execution.job['project_id'], 'collect:' + batch, 'redo:' + batch, execution.job['id'])).fetchall()]


def freeze(execution, batch, *, fresh):
    """Decide once per job whether this automatic round may draft incrementally.

    Only a job that starts under this rule and was started by a collection: an owner's
    re-analysis redoes everything, a job already under way keeps the full traversal,
    and so does a retry of a batch whose earlier analysis drafted in full.
    """
    if execution.read_checkpoint(MODE) is not None:
        return
    from project_watch.studio.execution import JobExecution
    allowed = fresh and str(execution.job.get('input_version') or '').startswith('collect:')
    if allowed and batch:
        with closing(execution.store._connect()) as db:
            for job_id in _siblings(execution, batch, db):
                if not (JobExecution(execution.store, {'id': job_id}).read_checkpoint(MODE, db=db) or {}).get('enabled'):
                    allowed = False
                    break
    execution.checkpoint(MODE, {'enabled': bool(allowed)})


def traversal(execution):
    """Which drafted prefix the candidate being published was written from.

    Every publication path reviews the draft whose progress was saved last: drafting
    saves COVERAGE before handing a draft on, and the final review takes the last
    retained draft. The mode and carried set are named so a later reader cannot
    apply the prefix to the wrong order.
    """
    from project_watch.studio.analyze_job import COVERAGE
    coverage = execution.read_checkpoint(COVERAGE) or {}
    drafted = coverage.get('drafted_fragments', coverage.get('processed_fragments'))
    if not isinstance(drafted, int) or drafted < 0:
        return None
    plan = execution.read_checkpoint(PLAN) or {}
    incremental = plan.get('mode') == 'incremental'
    return {'version': TRAVERSAL_VERSION, 'drafted': drafted, 'mode': 'incremental' if incremental else 'full',
            'carried': digest(plan['carried'] if incremental else [])}


def _fragment(material):
    return (material['evidence_version_id'], material['fragment']['part'], material['fragment']['offset'])


def _selection_refs(job):
    """A job's frozen selection as fragment references; full-text selections are reduced."""
    from project_watch.studio import analyze_job
    plan = job.read_checkpoint(analyze_job.SELECTION_REFS)
    if plan is None:
        full = job.read_checkpoint(analyze_job.SELECTION)
        plan = analyze_job._references(full) if isinstance(full, dict) else None
    return plan


def covered_fragments(store, record):
    """The fragments a published section version reflects, or None when that is not known for certain."""
    from project_watch.studio.execution import JobExecution
    marker = record.get('traversal')
    if not isinstance(marker, dict) or marker.get('version') != TRAVERSAL_VERSION or not record.get('job_id'):
        return None
    drafted = marker.get('drafted')
    if not isinstance(drafted, int) or drafted < 0:
        return None
    job = JobExecution(store, {'id': record['job_id']})
    try:
        plan = _selection_refs(job)
        prior = job.read_checkpoint(PLAN) or {}
        refs = [(r[0], r[1], r[2]) for r in plan['refs']] if plan and plan.get('id') == record.get('snapshot_id') else None
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    if refs is None:
        return None
    incremental = prior.get('mode') == 'incremental'
    carried = list(prior.get('carried') or ()) if incremental else []
    if marker.get('mode') != ('incremental' if incremental else 'full') or marker.get('carried') != digest(carried):
        return None
    if any(not isinstance(i, int) or not 0 <= i < len(refs) for i in carried):
        return None
    skip = set(carried)
    order = [ref for i, ref in enumerate(refs) if i not in skip]
    if drafted > len(order):
        return None
    return {refs[i] for i in skip} | set(order[:drafted])


def renumber(raw, numbered, sources, omitted=()):
    """Rewrite a published section's refs into this job's numbering.

    A cited source maps to the code with the same evidence version and text. Citations
    of material no longer selected (superseded, withdrawn or excluded) are dropped, and
    so is any list item left without a citation. Points the publication's normalizer
    omitted (``omitted``) were never shown and are not carried.
    """
    if not isinstance(raw, dict) or not isinstance(numbered, dict):
        return None, {}
    codes = {}
    for code, source in sources.items():
        codes.setdefault((source.get('evidence_version_id'), source.get('text')), code)
    mapping = {old: codes[(source.get('evidence_version_id'), source.get('text'))]
               for old, source in numbered.items()
               if isinstance(source, dict) and (source.get('evidence_version_id'), source.get('text')) in codes}
    omitted = {text for text in omitted if isinstance(text, str)}
    stats = {'kept_items': 0, 'dropped_items': 0, 'dropped_refs': 0}

    def rewrite(value):
        if isinstance(value, dict):
            out = {}
            for key, child in value.items():
                if key == 'refs' and isinstance(child, list):
                    mapped = [mapping[c] for c in child if isinstance(c, str) and c in mapping]
                    stats['dropped_refs'] += len(child) - len(mapped)
                    out[key] = list(dict.fromkeys(mapped))
                else:
                    out[key] = rewrite(child)
            return out
        if isinstance(value, list):
            kept = []
            for child in value:
                cited = isinstance(child, dict) and isinstance(child.get('refs'), list)
                if cited and child.get('text') in omitted:
                    continue
                new = rewrite(child)
                if cited and not new['refs']:
                    # An item that never had a citation is not a lost citation.
                    stats['dropped_items'] += bool(child['refs'])
                    continue
                stats['kept_items'] += cited
                kept.append(new)
            return kept
        return value
    return rewrite(raw), stats


def plan(execution, context, selected, sources, targets):
    """Freeze, once per job, whether this round drafts incrementally and from which seeds.

    A sibling of the same batch that already froze a plan over the same selection is
    followed exactly, so a retry sends the same requests. One with a different
    selection and an unresolved request blocks: a different plan would sidestep it.
    """
    saved = execution.read_checkpoint(PLAN)
    if saved is not None or not (execution.read_checkpoint(MODE) or {}).get('enabled'):
        return saved
    from project_watch.studio.dispatch import RECEIVED_FAILURE, batch_of
    from project_watch.studio.execution import JobExecution
    from project_watch.studio.llm import ModelError
    try:
        mine = [list(_fragment(m)) for m in selected['materials']]
    except (KeyError, TypeError):
        mine = None
    batch, frozen = batch_of(execution.job), None
    if batch:
        with closing(execution.store._connect()) as db:
            for job_id in _siblings(execution, batch, db):
                sibling = JobExecution(execution.store, {'id': job_id})
                other = sibling.read_checkpoint(PLAN, db=db)
                if other is None:
                    continue  # no plan, no draft: drafting starts only after the plan is frozen
                theirs = _selection_refs(sibling)
                if mine is not None and theirs and theirs.get('id') == selected['id'] and theirs['refs'] == mine:
                    frozen = other
                    break
                if db.execute("SELECT 1 FROM studio_job_calls WHERE job_id=? AND (status IN ('unknown','started') "
                              f"OR (kind='model' AND status='completed' AND response_status IN {RECEIVED_FAILURE} "
                              "AND retryable=0)) LIMIT 1", (job_id,)).fetchone():
                    raise ModelError('同批次增量分析请求结果不明，未改用不同请求或重发', code='unknown_not_resent')
    if frozen is None:
        frozen = _build(execution.store, execution.job['project_id'], context, selected, sources, targets, mine)
    execution.checkpoint_chunked(PLAN, frozen)
    return frozen


def seed_permitted(db, pid, record):
    """Every source the published version cites is still one the owner allows."""
    from project_watch.studio import material_snapshot
    try:
        for eid in record.get('evidence_ids') or ():
            material_snapshot._permitted(db, pid, eid)
    except ValueError:
        return False
    return True


def _build(store, pid, context, selected, sources, targets, refs):
    from project_watch.studio.section_analysis import PREFIX

    def full(reason):
        return {'mode': 'full', 'reason': reason, 'targets': list(targets)}
    if refs is None or len(refs) != len(sources):
        return full('selection_without_fragment_positions')
    position = {tuple(ref): i for i, ref in enumerate(refs)}
    seeds, carried, lag = {}, None, {}
    for name in targets:
        version = (context.get('baseline_analysis') or {}).get(PREFIX + name)
        if not version:
            return full('never_published:' + name)
        with closing(store._connect()) as db:
            row = db.execute('SELECT record FROM studio_insight_versions WHERE project_id=? AND id=?',
                             (pid, version)).fetchone()
            record = json.loads(row[0]) if row else {}
            if record and not seed_permitted(db, pid, record):
                # It cites material the owner has since excluded: do not feed that text back into drafting.
                return full('not_permitted:' + name)
        dependency = store.evidence.dependencies.get(pid, PREFIX + name) or {}
        status = dependency.get('status')
        with closing(store._connect()) as db:
            usable = seedable(db, pid, PREFIX + name, dependency)
        if not usable:
            return full('not_current:' + name)
        covered = covered_fragments(store, record) if record.get('section') == name else None
        if covered is None:
            return full('coverage_unknown:' + name)
        raw, stats = renumber(record.get('raw'), record.get('numbered'), sources,
                              (record.get('review') or {}).get('dropped') or ())
        if raw is None:
            return full('seed_unusable:' + name)
        indices = {position[ref] for ref in covered if ref in position}
        carried = indices if carried is None else carried & indices
        lag[name] = len(indices)
        # A seed that lost citations (or whose head is stale) differs from what is shown:
        # it is reviewed and published again even if no new material arrived.
        intact = status == 'current' and not stats['dropped_items'] and not stats['dropped_refs']
        seeds[name] = dict(version_id=version, job_id=record['job_id'], raw=raw, covered_fragments=len(indices),
                           status=status, intact=intact, **stats)
    # Every target must read what any one of them has not covered, so a section whose
    # published version lags (for example, rejected in later rounds) holds the others back.
    # It is never worse than a full read; ``lagging_sections`` makes it visible.
    common = len(carried or ())
    lagging = sorted(name for name, count in lag.items() if count == common and any(c > common for c in lag.values()))
    return {'mode': 'incremental', 'targets': list(targets), 'carried': sorted(carried or ()), 'seeds': seeds,
            **({'lagging_sections': lagging} if lagging else {})}


def view(frozen, selected, sources):
    """New material only, the seeds, and a coverage record saying what was carried forward."""
    carried = set(frozen['carried'])
    draft_sources = {code: source for i, (code, source) in enumerate(sources.items()) if i not in carried}
    seeded = {name: {k: v for k, v in seed.items() if k != 'raw'} for name, seed in frozen['seeds'].items()}
    coverage = {**selected['coverage'], 'mode': 'incremental', 'carried_fragments': len(carried),
                'new_fragments': len(draft_sources), 'seeded_sections': seeded,
                **({'lagging_sections': frozen['lagging_sections']} if frozen.get('lagging_sections') else {})}
    seed = {name: frozen['seeds'][name]['raw'] for name in frozen['targets']}
    return draft_sources, seed, {**selected, 'coverage': coverage}


def seed_stands(execution, seed):
    """Drafting stopped before any new material: the seed is a published version, not a candidate."""
    from project_watch.studio.analyze_job import COVERAGE
    coverage = execution.read_checkpoint(COVERAGE) or {}
    return bool(seed) and bool(coverage.get('blocked')) and not coverage.get('drafted_fragments')
