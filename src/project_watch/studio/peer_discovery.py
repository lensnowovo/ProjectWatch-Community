"""Cited public company leads and retrieval tools, not investment assessments."""

import hashlib
import json
import re
from contextlib import closing
from itertools import zip_longest

from project_watch.studio.business_identity import explicit_aliases
from project_watch.studio.monitoring_contracts import (
    EvidenceInput,
    EvidencePart,
    LocatedRef,
    ObjectInput,
)
from project_watch.studio.peer_contracts import Candidate as Candidate
from project_watch.studio.peer_contracts import Discovery
from project_watch.studio.peer_names import company_name
from project_watch.studio.provenance import COMPETITION_BODY_LIMIT, provenance

SCHEMA = '''
CREATE TABLE IF NOT EXISTS studio_competition_discovered (
 project_id TEXT NOT NULL REFERENCES projects(id), discovery_key TEXT NOT NULL,
 object_id TEXT NOT NULL REFERENCES studio_monitor_objects(id), PRIMARY KEY(project_id,discovery_key)
);
CREATE TABLE IF NOT EXISTS studio_competition_run_state (
 project_id TEXT PRIMARY KEY REFERENCES projects(id), record TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS studio_competition_run_history (
 job_id TEXT PRIMARY KEY REFERENCES studio_jobs(id), project_id TEXT NOT NULL REFERENCES projects(id), record TEXT NOT NULL
);
'''

INSTITUTION = re.compile(r'(?i)\b(?:hospital|university|college|medical cent(?:er|re)|clinic|school of)\b'
                         r'|医院|大学|学院|研究所|卫生院|医学中心')

def initialize(store):
    with closing(store._connect()) as db, db:
        db.executescript(SCHEMA)


def institution_name(name):
    """Hospitals/universities are trial sites or sponsors, not product competitors."""
    return bool(INSTITUTION.search(name))


def whitespace_span(quote, text):
    """Exact characters with only whitespace allowed to differ; no other repair.

    Search snippets often carry spaces the model drops (\"之一 ,其\"). Returns the
    source span (start, end) or None; punctuation and wording must match.
    """
    if quote in text:
        start = text.index(quote)
        return start, start + len(quote)
    chars = [c for c in quote if not c.isspace()]
    if len(chars) < 6 or len(chars) > 2000:
        return None
    match = re.search(r'\s*'.join(re.escape(c) for c in chars), text)
    return (match.start(), match.end()) if match else None


def atomic_discovery_name(name):
    """Automatic actor ambiguity guard; never split or restrict human choices."""
    return company_name(name) and not any(separator in name for separator in ('/', '／', '\\', '、', ';', '；', '\n', '\r'))


def discover_candidates(service, project_id, subject_id, sources, data, *, guard=None, report=None,
                        exclude_institutions=False, self_names=()):
    if len(sources) > 20 or any(r['project_id'] != project_id or r['visibility'] != 'public' for r in sources):
        raise ValueError('discovery requires bounded public project evidence')
    initialize(service.store)
    target = service.objects.get(project_id, subject_id, include_internal=False)
    blocked = {n.casefold() for row in service.relationships.view(project_id, include_internal=False)['exclusions']
               if row['company_wide'] for n in row['peer_names']}
    own = {target['name'].casefold(), *(a['name'].casefold() for a in target['aliases']),
           *(n.strip().casefold() for n in self_names if n.strip())}
    result = []
    def reject(reason):
        if report is not None:
            report[reason] = report.get(reason, 0) + 1
    for proposed in Discovery.model_validate(data).candidates:
        if not atomic_discovery_name(proposed.name):
            reject('ambiguous_actor_name' if company_name(proposed.name) else 'non_company_name')
            continue
        # Only where registry sponsor class already defines product peers; a
        # university can develop technology in other domains (scoped-publisher plan).
        if exclude_institutions and institution_name(proposed.name):
            reject('institution')
            continue
        marker = proposed.source
        if not marker.startswith('D') or not marker[1:].isdigit() or not 1 <= int(marker[1:]) <= len(sources):
            reject('unknown_source')
            continue
        evidence = sources[int(marker[1:])-1]
        if proposed.name.casefold() in own | blocked or proposed.name not in proposed.quote:
            reject('excluded_or_unquoted_name')
            continue
        located = next(((i, span) for i, p in enumerate(evidence['parts'])
                        for span in [whitespace_span(proposed.quote, p['text'])] if span), None)
        if located is None:
            reject('unlocated_quote')
            continue
        part, (start, end) = located
        # The stored quote is the source's own span, never the model's spacing.
        source_quote = evidence['parts'][part]['text'][start:end]
        if proposed.name not in source_quote:
            reject('excluded_or_unquoted_name')
            continue
        proposed = proposed.model_copy(update={'quote': source_quote})
        ref = LocatedRef(evidence_version_id=evidence['id'], part_index=part, start=start,
                         end=end, quote=source_quote)
        aliases = explicit_aliases(proposed.name, proposed.quote)
        if any(a.name.casefold() in own | blocked for a in aliases):
            reject('excluded_alias')
            continue
        key = hashlib.sha256(json.dumps([evidence['source_key'], proposed.name.casefold()]).encode()).hexdigest()
        with closing(service.store._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if guard:
                guard(db)
            # A delayed model reply cannot revive withdrawn/superseded input.
            head = db.execute('SELECT evidence_version_id FROM studio_evidence_heads WHERE project_id=? AND source_key=?',
                              (project_id, evidence['source_key'])).fetchone()
            withdrawn = db.execute('SELECT status FROM studio_evidence_lifecycle WHERE project_id=? AND evidence_version_id=?',
                                   (project_id, evidence['id'])).fetchone()
            if not head or head[0] != evidence['id'] or (withdrawn and withdrawn[0] == 'withdrawn'):
                reject('stale_source')
                continue
            row = db.execute('SELECT object_id FROM studio_competition_discovered WHERE project_id=? AND discovery_key=?', (project_id, key)).fetchone()
            if row:
                obj = service.objects.get(project_id, row[0], db=db)
                if not obj['user_override'] and (obj['refs'] != [ref.model_dump(mode='json')]
                        or obj['aliases'] != [a.model_dump(mode='json') for a in aliases]):
                    item = ObjectInput.model_validate({k: obj[k] for k in ObjectInput.model_fields}).model_copy(update={'refs': (ref,), 'aliases': aliases})
                    obj = service.objects.save(project_id, item, actor='automatic', object_id=obj['id'], expected_version=obj['version'], db=db)
            else:
                obj = service.objects.save(project_id, ObjectInput(kind='company', name=proposed.name,
                    purpose='related', visibility='public', refs=(ref,), aliases=aliases), actor='automatic', db=db)
                db.execute('INSERT INTO studio_competition_discovered VALUES (?,?,?)', (project_id, key, obj['id']))
        if not any(r['id'] == obj['id'] for r in result):
            result.append(obj)
    return result


def query_key(query):
    return hashlib.sha256(query.strip().encode()).hexdigest()


def merge_query_leads(batches):
    """Local memberships, fair bounded merge; provider claims are not trusted."""
    groups, leads = {}, {}
    for query, hits in batches:
        key = query_key(query)
        group = groups.setdefault(key, [])
        for hit in hits:
            url = hit.get('url') or ''
            if not url:
                continue
            url = url.split('#', 1)[0]
            previous = leads.get(url, {})
            keys = list(previous.get('_retrieval_query_keys', []))
            if key not in keys:
                keys.append(key)
            leads[url] = {**hit, 'url': url, '_retrieval_query_keys': keys}
            if url not in group:
                group.append(url)
    ordered = dict.fromkeys(url for row in zip_longest(*groups.values()) for url in row if url)
    return [leads[url] for url in list(ordered)[:8]]


def snapshot_source(pipeline, project_id, hit, text='', scope='summary', *, execution=None):
    passage = (text or str(hit.get('snippet') or ''))[:COMPETITION_BODY_LIMIT]
    title = ' '.join(str(hit.get('title') or '').split())[:500]
    body = title + '\n' + passage
    if not body.strip() or not str(hit.get('url') or '').startswith(('http://', 'https://')):
        return None
    details = provenance(hit['url'], passage, read_scope=scope)
    # Separate discovery snippets from original passages: replaying discovery
    # must never replace a richer current original with a search summary.
    source_key = 'competition:' + scope + ':' + hashlib.sha256(hit['url'].encode()).hexdigest()
    item = EvidenceInput(project_id=project_id, source_key=source_key,
        visibility='public', source_type='search_summary' if scope == 'summary' else 'public_page', read_scope=scope,
        extraction_version='competition-source-v2', **details,
        parts=(EvidencePart(locator_kind='stored_text', locator=hit['url'][:500], text=body),))
    if execution is None:
        return pipeline.evidence.save(item)
    with closing(pipeline.store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        execution._guard(db)
        return pipeline.evidence.save(item, db=db)
