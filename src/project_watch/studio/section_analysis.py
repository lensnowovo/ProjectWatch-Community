"""Evidence-reviewed section versions; one bad section cannot erase another."""
import json
from contextlib import closing
from copy import deepcopy

from project_watch.studio import judgment_review as review
from project_watch.studio import material_snapshot
from project_watch.studio.llm import ModelError
from project_watch.studio.store import digest, now

SECTIONS = ('position', 'technology', 'industry', 'operations', 'chain')
PREFIX = 'snapshot-section:'
# Classification labels are judgments, not quotable facts; the consistency review covers them.
LABELS = frozenset({'verdict', 'confidence', 'maturity', 'consensus'})
# Evidence lists: each item stands on its own refs and can be dropped without the section.
ITEMS = frozenset({'for', 'against', 'academic', 'industry', 'risks', 'peers', 'change_if'})


def _item(path):
    parts = path.split('.')
    return (parts[1], int(parts[2])) if len(parts) >= 3 and parts[1] in ITEMS and parts[2].isdigit() else None


def _inherent(conflict):
    """Supporting and opposing evidence of one section are expected to differ."""
    paths = conflict.get('fields') or []
    lists = {p.split('.')[1] for p in paths if len(p.split('.')) > 1}
    return len({p.split('.')[0] for p in paths}) == 1 and lists == {'for', 'against'}


def trim(part, issues):
    """Drop the list items an issue names; any issue on the summary or route blocks the section."""
    drop, blocking = set(), []
    for issue in issues:
        paths = issue.get('fields') or [issue.get('field')] if isinstance(issue, dict) else [None]
        for path in paths:
            if not isinstance(path, str):
                blocking.append(issue)
            elif _item(path):
                drop.add(_item(path))
            elif path.split('.')[-1] not in LABELS:
                blocking.append(issue)
    if blocking:
        return None, [], blocking
    trimmed, dropped = deepcopy(part), []
    for key in {k for k, _ in drop}:
        values = trimmed.get(key) or []
        dropped.extend(v.get('text') if isinstance(v, dict) else v for i, v in enumerate(values) if (key, i) in drop)
        trimmed[key] = [v for i, v in enumerate(values) if (key, i) not in drop]
    if dropped:
        trimmed['confidence'] = 'low'
    return trimmed, dropped, []


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
    for name in SECTIONS:
        part = raw.get(name)
        if not isinstance(part, dict) or not part:
            outcomes[name] = dict(status='missing', issues=['section_missing'])
            continue
        try:
            refs = _refs(part, sources)
            if not refs:
                raise ValueError('section has no evidence')
            payload = review.review_input({name: part}, {'numbered': sources})
            payload['fields'] = {path: text for path, text in payload['fields'].items()
                                 if path.split('.')[-1] not in LABELS}
            batches = review.review_batches(payload)
        except (ValueError, TypeError, KeyError):
            outcomes[name] = dict(status='rejected', issues=['invalid_section_or_reference'])
            continue
        issues, receipts = [], []
        for batch in batches:
            found, keys = _checked(execution, reviewer, guard, batch, refs, sources)
            issues.extend(found)
            receipts.extend(keys)
        trimmed, dropped, blocking = trim(part, issues)
        if trimmed is not None and not _refs(trimmed, sources):
            trimmed, blocking = None, [dict(code='section_without_supported_items', field=name)]
        outcomes[name] = dict(status='approved' if trimmed is not None else 'rejected', issues=issues,
                              refs=sorted(_refs(trimmed, sources) if trimmed is not None else refs),
                              review_receipts=receipts, dropped=dropped, blocking=blocking)
        if trimmed is not None:
            valid[name] = raw[name] = trimmed
    combined, evidence = deepcopy(valid), dict(sources)
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
    if valid and len(combined) > 1:
        payload = {**review.review_input(combined, {'numbered': evidence}), 'phase': 'consistency'}
        key = 'section-consistency:' + digest([review.VERSION, payload])[:40]
        guard()
        saved = execution.read_checkpoint(key)
        if saved is None:
            saved = _review(reviewer, review.CONSISTENCY_SYSTEM, payload)
            execution.checkpoint_chunked(key, saved)
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
                continue
            raw[name] = valid[name] = trimmed
            outcomes[name].update(refs=sorted(_refs(trimmed, sources)), dropped=outcomes[name]['dropped'] + dropped,
                                  issues=outcomes[name]['issues'] + mine)
    guard()
    return outcomes


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
            else:
                analysis[name] = records[0]['empty_analysis'][name]
        # Mixed-version sections do not gain a newly generated global headline.
        analysis['headline'] = ''
        analysis.setdefault('gaps', [])
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
