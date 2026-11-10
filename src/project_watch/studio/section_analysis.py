"""Evidence-reviewed section versions; one bad section cannot erase another."""
import json
from contextlib import closing
from copy import deepcopy

from project_watch.studio import judgment_review as review
from project_watch.studio import material_snapshot
from project_watch.studio.llm import ModelError
from project_watch.studio.step_outcomes import ISOLATABLE
from project_watch.studio.store import digest, now

SECTIONS = ('position', 'technology', 'industry', 'operations', 'chain')
PREFIX = 'snapshot-section:'
UNKNOWN_ERRORS = frozenset({'request_outcome_unknown', 'model_timeout', 'unknown_not_resent', 'request_in_flight'})


def _isolated(execution):
    return (execution.read_checkpoint('analysis_review_sources_v1') or {}).get('version', 1) >= 3


def blocked_outcome(exc):
    code = 'request_outcome_unknown' if exc.code in UNKNOWN_ERRORS else exc.code
    return dict(status='blocked', issues=[code])
# Classification labels are judgments, not quotable facts; the consistency review covers them.
LABELS = frozenset({'verdict', 'confidence', 'maturity', 'consensus'})
# General industry outline (chain.outline): stated as AI industry knowledge, never quote-checked.
OUTLINE = 'outline'


def _reviewed(path: str) -> bool:
    parts = path.split('.')
    return parts[-1] not in LABELS and OUTLINE not in parts
# Evidence lists: each item stands on its own refs and can be dropped without the section.
ITEMS = frozenset({'for', 'against', 'academic', 'industry', 'risks', 'drivers', 'headwinds', 'peers', 'change_if', 'gaps'})


def _item(path):
    parts = path.split('.')
    return (parts[1], int(parts[2])) if len(parts) >= 3 and parts[1] in ITEMS and parts[2].isdigit() else None


def _inherent(conflict):
    """Supporting and opposing evidence of one section are expected to differ."""
    paths = conflict.get('fields') or []
    lists = {p.split('.')[1] for p in paths if len(p.split('.')) > 1}
    return len({p.split('.')[0] for p in paths}) == 1 and lists == {'for', 'against'}


def trim(part, issues, *, keep_blocked=False):
    """Drop the list items an issue names; any issue on the summary or route blocks the section."""
    drop, blocking = set(), []
    for issue in issues:
        paths = issue.get('fields') or [issue.get('field')] if isinstance(issue, dict) else [None]
        for path in paths:
            if not isinstance(path, str):
                blocking.append(issue)
            elif _item(path):
                drop.add(_item(path))
            elif _reviewed(path):
                blocking.append(issue)
    if blocking and not keep_blocked:
        return None, [], blocking
    trimmed, dropped = deepcopy(part), []
    for key in {k for k, _ in drop}:
        values = trimmed.get(key) or []
        dropped.extend(v.get('text') if isinstance(v, dict) else v for i, v in enumerate(values) if (key, i) in drop)
        trimmed[key] = [v for i, v in enumerate(values) if (key, i) not in drop]
    if dropped:
        trimmed['confidence'] = 'low'
    return trimmed, dropped, blocking


def normalization_problem(generator, name, part, sources):
    """A guard-produced placeholder is not a completed, supported summary."""
    full = generator.normalize({name: part}, sources)
    checks = [c for c in (*full.get('consistency_checks', ()), *full.get('qualifier_checks', ()))
              if str(c.get('path', '')) == name + '.summary'
              and c.get('action') in ('omitted', 'uncertain', 'neutralized')]
    if full[name].get('summary') and not checks:
        return None
    return '；'.join(str(c.get('reason') or '') for c in checks) or '总结句含未能核实的表述'


def _review(reviewer, system, payload):
    try:
        return dict(response=reviewer(system, payload))
    except ModelError as exc:
        if exc.code not in ('model_output_invalid_shape', 'model_output_invalid_json', 'model_output_truncated'):
            raise
        return dict(response=None, error=exc.code)


def numbered(context):
    result = {}
    for material in context['materials']:
        evidence = material['evidence']
        text = ''.join(part['text'] for part in evidence['parts'])
        try:
            meta = json.loads(text)
            meta = meta if isinstance(meta, dict) else {}
        except ValueError:
            meta = {}
        # Collection proves provenance, not that a source is company news.
        code = 'S' + str(len(result) + 1)
        result[code] = dict(kind='public', text=text, context=text,
            title=meta.get('title') or material['source_key'], url=meta.get('url', ''),
            source=meta.get('source', ''), date=meta.get('published_at') or '',
            evidence_version_id=material['evidence_version_id'], read_scope=material['read_scope'])
    return result


def _refs(value, sources):
    found = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key == 'refs':
                if not isinstance(child, list) or any(not isinstance(c, str) or c not in sources for c in child):
                    raise ValueError('invalid section reference')
                found.update(child)
            else:
                found.update(_refs(child, sources))
    elif isinstance(value, list):
        for child in value:
            found.update(_refs(child, sources))
    elif value is not None and not isinstance(value, str):
        raise ValueError('invalid section field type')
    return found


def _checked(execution, reviewer, guard, batch, refs, sources, *, split=True):
    """Issues and receipts for one review batch. A malformed reply is reviewed once more in
    two halves (new, smaller requests); a half still malformed marks its own fields."""
    guard()
    key = 'section-review:' + digest([review.VERSION, batch])[:40]
    saved = execution.read_checkpoint(key)
    if saved is None:
        saved = _review(reviewer, review.CONSISTENCY_SYSTEM if batch['phase'] == 'consistency' else review.SYSTEM, batch)
        execution.checkpoint_chunked(key, saved)
    try:
        if saved.get('response') is None:
            raise ValueError(saved.get('error') or 'review_unavailable')
        if batch['phase'] == 'consistency':
            found = [c for c in review.validate_consistency(saved['response'], batch) if not _inherent(c)]
        else:
            found = review.validate_review(saved['response'], batch)
            for check in saved['response'].get('checks', []):
                refs.update(witness['ref'] for witness in check.get('evidence', []) if witness.get('ref') in sources)
        return found, [key]
    except ValueError:
        fields = list(batch['fields'])
        if split and batch['phase'] == 'fields' and len(fields) > 1:
            half = len(fields) // 2
            issues, receipts = [], [key]
            for chunk in (fields[:half], fields[half:]):
                sub = {**batch, 'fields': {path: batch['fields'][path] for path in chunk}}
                found, keys = _checked(execution, reviewer, guard, sub, refs, sources, split=False)
                issues.extend(found)
                receipts.extend(keys)
            return issues, receipts
        return [dict(code='invalid_review', field=fields[0] if len(fields) == 1 else None, fields=fields)], [key]


def evaluate(execution, raw, sources, reviewer, guard, retained=None):
    """Review fixed fields and cross-section dependencies; unsupported items are dropped.

    raw is updated in place with the trimmed sections that publish() will use.
    """
    outcomes, valid = {}, {}
    isolated = _isolated(execution)
    counter_signature = None
    if isolated:
        from project_watch.studio import review_source_traversal as traversal
        frozen_version = (execution.read_checkpoint('analysis_review_sources_v1') or {}).get('version', 1)
        contract = [frozen_version, review.VERSION, traversal.PROTOCOL,
                    traversal.COUNTER_SYSTEM + traversal.METRIC_SCOPE]
        if frozen_version >= 4:
            contract.append(getattr(reviewer, 'contract', ''))
        counter_signature = digest(contract)
        # Freeze before any provider work. A changed contract cannot turn an old
        # successful/uncertain review into a fresh request under a different key.
        scope = digest([raw, sources, retained])[:32]
        meta_key = 'section-counter-meta-v3:' + scope
        frozen = execution.read_checkpoint(meta_key)
        if frozen is None:
            execution.checkpoint_chunked(meta_key, {'signature': counter_signature})
        elif frozen.get('signature') != counter_signature:
            raise RuntimeError('analysis counterevidence contract changed; existing results retained')
    counter = {'issues': [], 'receipts': []}
    if hasattr(reviewer, 'prepare') and not isolated:
        combined, evidence = combine(raw, sources, retained)
        payload = review.review_input(combined, {'numbered': evidence})
        payload['fields'] = {path: text for path, text in payload['fields'].items() if _reviewed(path)}
        counter = reviewer.prepare(payload)
    for name in SECTIONS:
        part = raw.get(name)
        if not isinstance(part, dict) or not part:
            outcomes[name] = dict(status='missing', issues=['section_missing'])
            continue
        unit_key = 'section-unit-v3:' + digest([name, part, sources, retained])[:40]
        unit = execution.read_checkpoint(unit_key) if isolated else None
        if unit is not None:
            outcomes[name] = unit['outcome']
            if unit.get('raw') is not None:
                valid[name] = raw[name] = unit['raw']
            continue
        try:
            refs = _refs(part, sources)
            if not refs:
                raise ValueError('section has no evidence')
            payload = review.review_input({name: part}, {'numbered': sources})
            payload['fields'] = {path: text for path, text in payload['fields'].items() if _reviewed(path)}
            batches = review.review_batches(payload)
        except (ValueError, TypeError, KeyError):
            outcomes[name] = dict(status='rejected', issues=['invalid_section_or_reference'])
            continue
        if isolated:
            counter = {'issues': [], 'receipts': []}
        issues = [issue for issue in counter['issues'] if issue['field'].split('.')[0] == name]
        receipts = list(counter['receipts'])
        try:
            for batch in batches:
                found, keys = _checked(execution, reviewer, guard, batch, refs, sources)
                issues.extend(found)
                receipts.extend(keys)
        except ModelError as exc:
            if exc.code not in (ISOLATABLE | UNKNOWN_ERRORS if isolated else UNKNOWN_ERRORS):
                raise
            outcomes[name] = blocked_outcome(exc)
            if isolated:
                execution.checkpoint_chunked(unit_key, dict(outcome=outcomes[name], raw=None))
            continue
        trimmed, dropped, blocking = trim(part, issues)
        if trimmed is not None and not _refs(trimmed, sources):
            trimmed, blocking = None, [dict(code='section_without_supported_items', field=name)]
        outcomes[name] = dict(status='approved' if trimmed is not None else 'rejected', issues=issues,
                              refs=sorted(_refs(trimmed, sources) if trimmed is not None else refs),
                              review_receipts=receipts, dropped=dropped, blocking=blocking)
        if trimmed is None:
            basis, removed, _ = trim(part, issues, keep_blocked=True)
            outcomes[name].update(repair_basis=basis, dropped=removed)
        if trimmed is not None:
            valid[name] = raw[name] = trimmed
        if isolated:
            execution.checkpoint_chunked(unit_key, dict(outcome=outcomes[name], raw=trimmed))
            from project_watch.studio.review_source_traversal import safe_yield
            safe_yield(execution)
    if isolated and valid and hasattr(reviewer, 'prepare'):
        # Direct support is independent; the complete counterevidence traversal
        # is shared. An uncertain shared request blocks every dependent section.
        # Use the supported drafts consistently so field identities match trim().
        _, counter_sources = combine({}, sources, retained)
        payload = review.review_input(valid, {'numbered': counter_sources})
        payload['fields'] = {path: text for path, text in payload['fields'].items() if _reviewed(path)}
        key = 'section-counter-v3:' + digest([counter_signature, payload])[:40]
        guard()
        counter = execution.read_checkpoint(key)
        if counter is None:
            try:
                counter = reviewer.prepare(payload)
            except ModelError as exc:
                if exc.code not in ISOLATABLE | UNKNOWN_ERRORS:
                    raise
                counter = {'blocked_code': exc.code}
            execution.checkpoint_chunked(key, counter)
        if counter.get('blocked_code'):
            for name in valid:
                outcomes[name] = blocked_outcome(ModelError('shared counter unavailable', code=counter['blocked_code']))
            return outcomes
        for name in list(valid):
            found = [issue for issue in counter['issues'] if issue['field'].split('.')[0] == name]
            candidate = valid[name]
            trimmed, dropped, blocking = trim(candidate, found)
            outcome = outcomes[name]
            outcome['issues'] = outcome['issues'] + found
            outcome['review_receipts'] = outcome['review_receipts'] + list(counter['receipts'])
            outcome['dropped'] = outcome['dropped'] + dropped
            if trimmed is None or not _refs(trimmed, sources):
                outcome.update(status='rejected', blocking=blocking or [dict(code='section_without_supported_items', field=name)])
                basis, _, _ = trim(candidate, found, keep_blocked=True)
                outcome['repair_basis'] = basis
                del valid[name]
            else:
                raw[name] = valid[name] = trimmed
                outcome['refs'] = sorted(_refs(trimmed, sources))
    combined, evidence = combine(valid, sources, retained)
    if valid and len(combined) > 1:
        payload = {**review.review_input(combined, {'numbered': evidence}), 'phase': 'consistency'}
        key = 'section-consistency:' + digest([review.VERSION, payload])[:40]
        guard()
        saved = execution.read_checkpoint(key)
        if saved is None:
            try:
                saved = _review(reviewer, review.CONSISTENCY_SYSTEM, payload)
            except ModelError as exc:
                if not isolated or exc.code not in ISOLATABLE | UNKNOWN_ERRORS:
                    raise
                # Every new candidate in this combined review depends on its
                # completion. Keep old publications; do not publish unchecked peers.
                saved = dict(response=None, blocked_code=exc.code)
            execution.checkpoint_chunked(key, saved)
        if saved.get('blocked_code'):
            for name in valid:
                code = saved['blocked_code']
                outcomes[name] = dict(status='blocked', issues=[
                    'request_outcome_unknown' if code in UNKNOWN_ERRORS else code])
            guard()
            return outcomes
        try:
            conflicts = [c for c in review.validate_consistency(saved['response'], payload) if not _inherent(c)]
        except ValueError:
            conflicts = None
        if conflicts is None:
            for name in valid:
                outcomes[name].update(status='rejected', issues=[dict(code='invalid_consistency_review')])
        for name in list(valid) if conflicts else []:
            mine = [dict(c, fields=[p for p in c['fields'] if p.split('.')[0] == name]) for c in conflicts
                    if any(p.split('.')[0] == name for p in c['fields'])]
            if not mine:
                continue
            trimmed, dropped, blocking = trim(valid[name], mine)
            if trimmed is None or not _refs(trimmed, sources):
                outcomes[name].update(status='rejected', issues=outcomes[name]['issues'] + mine, blocking=blocking or mine)
                basis, removed, _ = trim(valid[name], mine, keep_blocked=True)
                outcomes[name].update(repair_basis=basis, dropped=outcomes[name]['dropped'] + removed)
                continue
            raw[name] = valid[name] = trimmed
            outcomes[name].update(refs=sorted(_refs(trimmed, sources)), dropped=outcomes[name]['dropped'] + dropped,
                                  issues=outcomes[name]['issues'] + mine)
    guard()
    return outcomes


def combine(raw, sources, retained=None):
    combined, evidence = deepcopy(raw), dict(sources)
    for name, record in (retained or {}).items():
        if name in combined:
            continue
        aliases = {ref: 'retained_' + name + '_' + ref for ref in record['numbered']}
        def remap(value, aliases=aliases):
            if isinstance(value, dict):
                return {key: [aliases[ref] for ref in child] if key == 'refs' else remap(child)
                        for key, child in value.items()}
            return [remap(child) for child in value] if isinstance(value, list) else value
        combined[name] = remap(record['raw'])
        evidence.update({aliases[ref]: value for ref, value in record['numbered'].items()})
    return combined, evidence


def retained_sections(store, pid, context):
    """Only the frozen baseline, with its original permitted evidence and date."""
    result = {}
    with closing(store._connect()) as db:
        for key, version in context['baseline_analysis'].items():
            if not key.startswith(PREFIX):
                continue
            row = db.execute('SELECT record FROM studio_insight_versions WHERE project_id=? AND id=?', (pid, version)).fetchone()
            if not row:
                continue
            record = json.loads(row[0])
            try:
                for eid in record['evidence_ids']:
                    material_snapshot._permitted(db, pid, eid)
            except ValueError:
                continue
            if (store.evidence.dependencies.get(pid, key) or {}).get('status') == 'current':
                result[record['section']] = record
    return result


def publish(pipeline, execution, context, raw, sources, outcomes, guard, *, targets=SECTIONS):
    """Publish only independently approved sections, preserving prior heads."""
    pid = execution.job['project_id']
    generator = pipeline.insight_generator
    accepted = {}
    for name, result in outcomes.items():
        if result['status'] != 'approved':
            continue
        try:
            if ((execution.read_checkpoint('analysis_review_sources_v1') or {}).get('version', 1) >= 2
                    and normalization_problem(generator, name, raw[name], sources)):
                raise ValueError('section summary replaced by normalization guard')
            full = generator.normalize({name: raw[name]}, sources)
            normalized = full[name]
            if not normalized.get('summary'):
                raise ValueError('section has no supported summary')
            accepted[name] = normalized
            # Points the normalizer's guards omitted are recorded like review-trimmed ones.
            omitted = [c['path'] for c in (*full.get('consistency_checks', ()), *full.get('qualifier_checks', ()))
                       if c.get('action') in ('omitted', 'uncertain') and str(c.get('path', '')).startswith(name + '.')]
            for path in dict.fromkeys(omitted):
                parts = path.split('.')
                if len(parts) >= 3 and parts[2].isdigit():
                    points = raw[name].get(parts[1]) if isinstance(raw[name], dict) else None
                    kept = {p.get('text') for p in normalized.get(parts[1]) or () if isinstance(p, dict)}
                    if isinstance(points, list) and int(parts[2]) < len(points) and isinstance(points[int(parts[2])], dict) \
                            and points[int(parts[2])].get('text') not in kept:
                        result.setdefault('dropped', []).append(points[int(parts[2])].get('text'))
        except (ValueError, TypeError, KeyError, AttributeError):
            result.update(status='rejected', issues=['section_normalization_failed'])
    guard()
    material_snapshot.read(execution.store, pid, context['id'])
    with closing(execution.store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        material_snapshot._guard(execution, db)
        for name, section in accepted.items():
            key = PREFIX + name
            signature = digest([context['id'], raw[name], outcomes[name], section])
            identity = digest([pid, key, signature])
            exists = db.execute('SELECT 1 FROM studio_insight_versions WHERE id=?', (identity,)).fetchone()
            if exists:
                # Replaying a publication must never move a newer head backwards.
                continue
            evidence_ids = sorted({sources[ref]['evidence_version_id'] for ref in outcomes[name]['refs']})
            for eid in evidence_ids:
                material_snapshot._permitted(db, pid, eid)
            dependency = pipeline.evidence.dependencies.track(pid, key, 'insight', evidence_ids, db=db)
            if context['object_versions']:
                dependency = pipeline.evidence.dependencies.track_objects(pid, key, context['object_versions'], db=db)
            if dependency['status'] != 'current':
                raise ValueError('section evidence changed')
            previous = db.execute('SELECT version_id FROM studio_insight_heads WHERE project_id=? AND artifact_key=?', (pid, key)).fetchone()
            record = dict(id=identity, section=name, analysis=section, raw=raw[name],
                empty_analysis=generator.normalize({}, {}),
                snapshot_id=context['id'], job_id=execution.job['id'], generated_at=now(),
                parent_version_id=previous[0] if previous else None, evidence_ids=evidence_ids,
                review=outcomes[name], numbered={ref: sources[ref] for ref in outcomes[name]['refs']})
            db.execute('INSERT INTO studio_insight_versions VALUES (?,?,?,?,?,?)',
                       (identity, pid, key, signature, json.dumps(record, ensure_ascii=False), record['generated_at']))
            db.execute('INSERT INTO studio_insight_heads VALUES (?,?,?) ON CONFLICT(project_id,artifact_key) DO UPDATE SET version_id=excluded.version_id',
                       (pid, key, identity))
    return dict(status='completed' if set(accepted) == set(targets) else 'partial', targets=list(targets),
                snapshot_id=context['id'], sections=outcomes, published=list(accepted))


def _strip_codes(value):
    """Records published before inline codes were stripped at normalization."""
    from project_watch.studio.citation_text import strip_inline_codes

    if isinstance(value, dict):
        for key, child in value.items():
            if key in ('text', 'summary') and isinstance(child, str):
                value[key] = strip_inline_codes(child)
            else:
                _strip_codes(child)
    elif isinstance(value, list):
        for child in value:
            _strip_codes(child)


def project(store, pid, baseline):
    """Compose per-section dates without relabelling retained results as new."""
    with closing(store._connect()) as db:
        rows = db.execute('SELECT v.record FROM studio_insight_heads h JOIN studio_insight_versions v ON v.id=h.version_id '
                          'WHERE h.project_id=? AND h.artifact_key LIKE ?', (pid, PREFIX + '%')).fetchall()
        if not rows:
            return baseline
        records = [json.loads(row[0]) for row in rows]
        result = dict(baseline or {})
        analysis = {**records[0]['empty_analysis'], **(result.get('analysis') or {})}
        meta, references, source_counts = {}, {}, {}
        def citations(value, numbered_sources):
            if isinstance(value, dict):
                if 'code' in value and 'kind' in value and 'label' in value:
                    source = numbered_sources.get(value['code'], {})
                    identity = source.get('evidence_version_id') or digest(value)
                    if identity not in references:
                        prefix = value['code'][0]
                        source_counts[prefix] = source_counts.get(prefix, 0) + 1
                        references[identity] = {**value, 'code': prefix + str(source_counts[prefix])}
                    value.update(references[identity])
                else:
                    for child in value.values():
                        citations(child, numbered_sources)
            elif isinstance(value, list):
                for child in value:
                    citations(child, numbered_sources)
        for record in records:
            name = record['section']
            try:
                for eid in record['evidence_ids']:
                    material_snapshot._permitted(db, pid, eid)
                permitted = True
            except ValueError:
                permitted = False
            dependency = store.evidence.dependencies.get(pid, PREFIX + name)
            status = (dependency or {}).get('status', 'stale') if permitted else 'withdrawn'
            meta[name] = dict(version_id=record['id'], snapshot_id=record['snapshot_id'],
                              generated_at=record['generated_at'], status=status)
            if permitted:
                analysis[name] = deepcopy(record['analysis'])
                citations(analysis[name], record['numbered'])
                _strip_codes(analysis[name])
            else:
                analysis[name] = records[0]['empty_analysis'][name]
        # Mixed-version sections do not gain a newly generated global headline. Gaps come from the
        # published sections only; an older whole-analysis record's gaps belong to another snapshot.
        analysis['headline'] = ''
        analysis['gaps'] = list(dict.fromkeys(g for name in SECTIONS if name in meta
                                              for g in (analysis.get(name) or {}).get('gaps') or []))
        for name in SECTIONS:
            if name not in meta and baseline and baseline.get('analysis', {}).get(name):
                analysis[name] = deepcopy(analysis[name])
                citations(analysis[name], baseline.get('numbered', {}))
                meta[name] = dict(status='retained', generated_at=baseline.get('generated_at'), version_id=baseline.get('id'))
        analysis['cited'] = list(references.values())
        result.update(analysis=analysis, section_versions=meta, generated_at=max(r['generated_at'] for r in records),
                      total=len(references), counts=source_counts, model='saved-material-analysis',
                      status='current' if len(meta) == len(SECTIONS) and all(m['status'] == 'current' for m in meta.values()) else 'partial',
                      publication_policy='per-section-v1')
        result.pop('artifact_key', None)
        return result
