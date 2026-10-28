"""Public object/relationship identities for standing-scope adaptation."""
import json
from datetime import date

from project_watch.studio.dsh_bootstrap import digest


def bindings(pipeline, db, pid, records):
    objects, names = {}, {}
    today = date.today().isoformat()
    for row in db.execute("SELECT record FROM studio_monitor_objects WHERE project_id=? AND visibility='public'", (pid,)):
        obj = pipeline.objects._disclose(json.loads(row[0]), False)
        objects[obj['id']] = obj
        current = obj['user_override'] or bool(obj['refs'])
        for ref in obj['refs']:
            current &= bool(db.execute('SELECT 1 FROM studio_evidence_heads h LEFT JOIN studio_evidence_lifecycle l '
                "ON l.evidence_version_id=h.evidence_version_id AND l.project_id=h.project_id WHERE h.project_id=? "
                "AND h.evidence_version_id=? AND COALESCE(l.status,'')!='withdrawn'", (pid, ref['evidence_version_id'])).fetchone())
        aliases = [a['name'] for a in obj['aliases'] if current and
                   (not a.get('valid_from') or a['valid_from'] <= today) and (not a.get('valid_to') or a['valid_to'] >= today)]
        for name in [obj['name'], *aliases]:
            names.setdefault(name.strip().casefold(), set()).add(obj['id'])
    effective = pipeline.relationships.page(pid, include_internal=False, db=db)
    overrides = pipeline.relationships.overrides(pid, include_internal=False, db=db)
    result = {}
    for row in records:
        subjects = names.get(str(row.get('subject', '')).strip().casefold(), set())
        targets = names.get(row.get('relation_target', '').strip().casefold(), set())
        reason = 'ambiguous_identity' if len(subjects) != 1 or len(targets) > 1 else ''
        subject, target = next(iter(subjects), None), next(iter(targets), None)
        target_obj = objects.get(target, {})
        target_company = target if target_obj.get('kind') == 'company' else target_obj.get('parent_id')
        def matches(relation, subject=subject, target=target, target_company=target_company):
            if relation['subject_id'] != subject:
                return False
            if relation.get('peer_product_id'):
                return relation['peer_product_id'] == target
            return bool(target_company and relation['peer_id'] == target_company)
        selected = [r for r in effective if matches(r)]
        excluded = [r for r in overrides if r['decision'] == 'exclude' and matches(r)]
        if excluded:
            reason = 'owner_excluded'
        key = digest([subject, row.get('relation_type'), target or ['typed_target', row.get('relation_target', '').strip().casefold()]])
        result[row['key']] = dict(key=key, subject_object_id=subject, target_object_id=target,
            object_versions={oid: objects[oid]['version'] for oid in (subject, target, target_company) if oid in objects},
            relationships=[{k: r.get(k) for k in ('scope_key', 'origin', 'version', 'object_versions')} for r in selected],
            overrides=[{k: r.get(k) for k in ('scope_key', 'decision', 'version')} for r in overrides if matches(r)],
            blocked_reason=reason)
    return result
