"""Bounded, read-only material projection from one SQLite read snapshot.

No original files, raw bodies, private file paths, provider logs or AI briefs.
Source assertions retain exact reference coordinates, not automatic truth labels.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import closing

from project_watch.studio.application import ProjectMissing
from project_watch.studio.provenance import effective_family

MAX_BYTES = 512 * 1024
LIMITS = {'objects': 20, 'facts': 100, 'relationships': 50, 'overrides': 50, 'evidence': 100}


def refs(record):
    record = dict(record)
    record['refs'] = [{k: r[k] for k in ('evidence_version_id', 'part_index', 'start', 'end')}
                      for r in record.get('refs', [])]
    return record


def compile_package(service, project_id, *, include_internal=False):
    with closing(service.store._connect()) as db, db:
        # BEGIN pins every projection, including relationship dependency reads.
        db.execute('BEGIN')
        row = db.execute('SELECT p.name,s.legal_name,s.short_name,s.industry,s.website,s.status,(s.consent_at IS NOT NULL) AS activated,s.config_version '
                         'FROM studio_projects s JOIN projects p ON p.id=s.project_id WHERE s.project_id=?', (project_id,)).fetchone()
        if row is None:
            raise ProjectMissing('Studio project not found')
        project = {'id': project_id, **dict(row)}
        def records(table, order, limit):
            return [json.loads(r[0]) for r in db.execute(
                f'SELECT t.record FROM {table} t WHERE t.project_id=? AND (? OR t.visibility=\'public\') '
                f'ORDER BY t.{order} LIMIT ?', (project_id, include_internal, limit + 1))]
        objects = [refs(service.objects._disclose(r, include_internal)) for r in
                   records('studio_monitor_objects', 'id', LIMITS['objects'])]
        facts = records('studio_fact_records', 'id', LIMITS['facts'])
        permitted_facts = []
        for fact in facts:
            try:
                service.objects.get(project_id, fact['subject_id'], include_internal=include_internal, db=db)
                peer_id = (fact.get('attributes') or {}).get('peer_id')
                if peer_id:
                    service.objects.get(project_id, peer_id, include_internal=include_internal, db=db)
            except KeyError:
                continue
            dep = service.evidence.dependencies.get(project_id, 'fact:' + fact['id'], db=db)
            permitted_facts.append({**refs(fact), 'dependency': {
                k: dep[k] for k in ('status', 'generation', 'stale_reason') if dep and k in dep} if dep else None})
        fields = ('scope_key', 'subject_id', 'peer_id', 'subject_product_id', 'peer_product_id', 'origin', 'support',
                  'relation_type', 'decision', 'version', 'visibility', 'object_versions', 'status',
                  'scope_identity_changed', 'previous_support', 'validity_reason', 'missing', 'overlap_kind', 'assertion_basis')
        relationships = [{k: r[k] for k in fields if k in r} for r in
                         service.relationships.page(project_id, include_internal=include_internal, db=db)]
        overrides = [{k: r[k] for k in fields if k in r} for r in
                     service.relationships.overrides(project_id, include_internal=include_internal, db=db)]
        # JSON removal avoids loading all extracted document text just for a manifest.
        evidence = []
        rows = db.execute("SELECT json_remove(e.record,'$.parts') AS record,json_array_length(e.record,'$.parts') AS part_count,"
                          "CASE WHEN json_extract(e.record,'$.extraction_version') IN ('competition-source-v1','competition-source-v2') "
                          "AND json_array_length(e.record,'$.parts')=1 THEN json_extract(e.record,'$.parts[0]') END AS family_part,"
                          "h.evidence_version_id=e.id AS current_head,l.status AS lifecycle FROM studio_evidence_versions e "
                          "LEFT JOIN studio_evidence_heads h ON h.project_id=e.project_id AND h.source_key=e.source_key "
                          "LEFT JOIN studio_evidence_lifecycle l ON l.project_id=e.project_id AND l.evidence_version_id=e.id "
                          "WHERE e.project_id=? AND (? OR e.visibility='public') ORDER BY e.seq DESC LIMIT ?",
                          (project_id, include_internal, LIMITS['evidence'] + 1)).fetchall()
        for row in rows:
            data = json.loads(row['record'])
            if row['family_part']:
                data.update(effective_family({**data, 'parts': [json.loads(row['family_part'])]}))
            keys = ('id', 'content_hash', 'extraction_version', 'visibility', 'source_type', 'origin', 'read_scope',
                    'propagation_family', 'family_basis', 'created_at')
            evidence.append({**{k: data[k] for k in keys if k in data}, 'part_count': row['part_count'],
                             'current_head': bool(row['current_head']), 'lifecycle': row['lifecycle'] or 'current'})
        health = [json.loads(r[0]) for r in db.execute('SELECT record FROM studio_source_health WHERE project_id=? ORDER BY source_kind', (project_id,))]
        data = {'objects': objects, 'facts': permitted_facts, 'relationships': relationships, 'overrides': overrides, 'evidence': evidence}
        truncated = {k: len(v) > LIMITS[k] or (k in ('relationships', 'overrides') and len(v) == 500) for k, v in data.items()}
        result = {'schema_version': 1, 'package_version': 1, 'instance_id': service.instance_id, 'project_id': project_id,
                  'project': project, **{k: v[:LIMITS[k]] for k, v in data.items()}, 'truncated': truncated,
                  'health': {'sources': health, 'full_coverage': False, 'business_silence': 'not_evaluated'},
                  'disclosure': {'include_internal': include_internal, 'raw_files': False, 'raw_bodies': False,
                                 'quote_text': False, 'analysis': False},
                  'continuations': {'objects': 'monitor_objects', 'facts': 'object_facts', 'relationships': 'monitor_relationships',
                                    'overrides': 'monitor_relationships', 'evidence': 'evidence_versions'},
                  'reference_resolution': 'evidence_part / resolve_excerpt; references may point outside this bounded manifest'}
        def encoded():
            return json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
        while len(encoded()) > MAX_BYTES - 100:
            nonempty = [k for k in LIMITS if result[k]]
            if not nonempty:
                raise ValueError('package metadata exceeds response bound')
            key = max(nonempty, key=lambda k: len(json.dumps(result[k], ensure_ascii=False).encode()))
            result[key].pop()
            truncated[key] = True
        result['snapshot_id'] = hashlib.sha256(encoded()).hexdigest()
        return result
