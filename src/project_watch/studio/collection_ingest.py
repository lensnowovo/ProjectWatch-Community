"""Transactional source identity and immutable snapshots for host collection.

Collection imports evidence, not semantic relevance, company authorship or new
events. DSH still has to assess these candidates before publishing judgments.
"""
import hashlib
import json
import uuid
from contextlib import closing
from datetime import datetime
from urllib.parse import urlsplit

from project_watch.studio import evidence_tasks
from project_watch.studio.http import HttpError
from project_watch.studio.monitoring_contracts import EvidenceInput, EvidencePart
from project_watch.studio.sources import item_key
from project_watch.studio.store import now

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS studio_collection_sources (
        project_id TEXT NOT NULL REFERENCES projects(id), identity TEXT NOT NULL,
        item_id TEXT NOT NULL REFERENCES studio_items(id),
        evidence_version_id TEXT NOT NULL REFERENCES studio_evidence_versions(id),
        observed_at TEXT NOT NULL, PRIMARY KEY(project_id,identity))""",
    """CREATE TABLE IF NOT EXISTS studio_collection_observations (
        job_id TEXT NOT NULL REFERENCES studio_jobs(id), unit_id TEXT NOT NULL,
        item_id TEXT NOT NULL REFERENCES studio_items(id),
        evidence_version_id TEXT NOT NULL REFERENCES studio_evidence_versions(id),
        operation TEXT NOT NULL CHECK(operation IN ('added','updated','reused','historical')),
        observed_at TEXT NOT NULL, PRIMARY KEY(job_id,unit_id,item_id,evidence_version_id))""",
)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _time(value):
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError('source observation requires timezone')
    return moment


def _record(item):
    """Validate source data before opening a write transaction; never silently drop."""
    if not isinstance(item, dict) or not isinstance(item.get('extra', {}), dict):
        raise ValueError('invalid collected item')
    for field in ('title', 'url', 'source', 'snippet'):
        if not isinstance(item.get(field), str):
            raise ValueError('invalid collected item text')
    url = urlsplit(item['url'])
    if not item['title'].strip() or url.scheme not in ('https', 'http') or not url.hostname or url.username or url.password:
        raise ValueError('invalid collected source identity')
    extra = item.get('extra', {})
    raw = extra.get('source_record')
    if raw is not None and not isinstance(raw, dict):
        raise ValueError('invalid structured source record')
    stable = extra.get('source_identity')
    if stable is not None and (not isinstance(stable, str) or not stable or len(stable) > 500):
        raise ValueError('invalid stable source identity')
    identity = stable or 'url:' + item_key(item['url'], item['title'])
    snapshot = {k: item.get(k) for k in ('title', 'url', 'source', 'snippet', 'published_at')}
    if raw is not None:
        snapshot['source_record'] = raw
    text = _json(snapshot)
    if len(text.encode()) > 1_000_000:
        raise ValueError('collected source record exceeds storage bound')
    return identity, text, raw is not None


class CollectionIngest:
    def __init__(self, execution):
        self.execution, self.store = execution, execution.store
        self.project_id = execution.job['project_id']
        with self.store.write_scope(execution._guard), closing(self.store._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            execution._guard(db)
            for statement in SCHEMA:
                db.execute(statement)
            db.execute(evidence_tasks.SCHEMA)
            from project_watch.studio.material_ownership import SCHEMA as ownership_schema

            for statement in ownership_schema:
                db.execute(statement)

    def __call__(self, unit, items):
        try:
            prepared = [(item, *_record(item)) for item in items]
            unique = {}
            for entry in prepared:
                _item, identity, text, _structured = entry
                if identity in unique and unique[identity][2] != text:
                    raise ValueError('conflicting versions in one source response')
                unique[identity] = entry
            prepared = list(unique.values())
        except (ValueError, TypeError) as exc:
            raise HttpError('invalid_collection_record') from exc
        payload = self.execution.read_checkpoint('collection_payload:' + unit['id'][:40]) or {}
        times = [r['completed_at'] for r in payload.get('cache_provenance', []) if r.get('completed_at')]
        times.extend(r['created_at'] for r in payload.get('request_provenance', {}).get('requests', [])
                     if r.get('created_at'))
        # A conservative retrieval bound prevents an old cache replay replacing
        # a newer snapshot. Query/window dates are not publication dates.
        observed = min(times, key=_time) if times else unit['window_end']
        _time(observed)
        refs = []
        with self.store.write_scope(self.execution._guard), closing(self.store._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            self.execution._guard(db)
            for item, identity, text, structured in prepared:
                refs.append(self._save(db, unit, item, identity, text, structured, observed))
        # Database evidence and observations are atomic; replay after this write
        # returns the same original disposition, not a second added/updated item.
        counts = {name + '_count': sum(r['operation'] == name for r in refs)
                  for name in ('added', 'updated', 'reused', 'historical')}
        return dict(saved_count=len(refs), evidence_refs=refs, **counts)

    def _save(self, db, unit, item, identity, text, structured, observed):
        source = db.execute('SELECT * FROM studio_collection_sources WHERE project_id=? AND identity=?',
                            (self.project_id, identity)).fetchone()
        existing = db.execute('SELECT * FROM studio_items WHERE project_id=? AND id=?',
                              (self.project_id, source['item_id'])).fetchone() if source else None
        if existing is None and not identity.startswith('site:'):
            # Adopt a legacy URL-based item once, preserving its ID and status.
            # Site diff events and current page snapshots have different identities;
            # multiple historical diffs must never be collapsed into one snapshot.
            field, identifier = ('nct_id', identity[9:]) if identity.startswith('clinical:') else (
                ('openalex_id', identity[9:]) if identity.startswith('openalex:') else ('source_identity', identity))
            matches = db.execute(
                "SELECT * FROM studio_items WHERE project_id=? AND (key=? OR url=? OR "
                "json_extract(CASE WHEN json_valid(extra) THEN extra ELSE '{}' END,?)=?)",
                (self.project_id, item_key(item['url'], item['title']), item['url'], '$.' + field, identifier)).fetchall()
            if len(matches) > 1:
                raise HttpError('ambiguous_collection_identity')
            existing = matches[0] if matches else None
        item_id = existing['id'] if existing else uuid.uuid4().hex
        key = 'collection:' + hashlib.sha256(identity.encode()).hexdigest()
        older_source = source is not None and _time(observed) < _time(source['observed_at'])
        # A second adapter can discover an already indexed URL. It must not
        # downgrade that item with an older cached representation either.
        other_times = [r[0] for r in db.execute('SELECT observed_at FROM studio_collection_sources '
                                              'WHERE project_id=? AND item_id=?', (self.project_id, item_id))]
        historical = older_source or any(_time(observed) < _time(t) for t in other_times)
        record = self.store.evidence.save(EvidenceInput(
            project_id=self.project_id, source_key=key, extraction_version='collection-snapshot-v1',
            source_type='structured_record' if structured else 'search_summary', visibility='public',
            read_scope='partial' if structured else 'summary',
            raw_content_hash=hashlib.sha256(text.encode()).hexdigest(),
            parts=[EvidencePart(locator_kind='stored_text', locator=f'source fields JSON chars {i}:{i+len(text[i:i+20000])}',
                                text=text[i:i+20000]) for i in range(0, len(text), 20000)]),
            db=db, advance_head=not older_source)
        prior = db.execute('SELECT operation,observed_at FROM studio_collection_observations '
                           'WHERE job_id=? AND unit_id=? AND item_id=? AND evidence_version_id=?',
                           (self.execution.job['id'], unit['id'], item_id, record['id'])).fetchone()
        operation = ('historical' if historical else 'reused' if source and source['evidence_version_id'] == record['id']
                     else 'updated' if existing else 'added')
        if existing is None:
            category = {'chain': 'company', 'technology': 'industry'}.get(unit['category'], unit['category'])
            db.execute('INSERT INTO studio_items (id,project_id,key,category,title,url,source,snippet,published_at,'
                       'collected_at,extra) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (item_id, self.project_id, key, category, item['title'], item['url'], item['source'],
                        item['snippet'], item.get('published_at'), now(), _json(item.get('extra', {}))))
        elif not historical and operation == 'updated':
            from project_watch.studio.event_revisions import record as record_event

            if existing['event_id']:
                record_event(db, self.project_id, existing['event_id'])
            extra = json.loads(existing['extra'] or '{}')
            extra.update(item.get('extra', {}))
            db.execute('UPDATE studio_items SET title=?,url=?,source=?,snippet=?,published_at=?,extra=?,triaged=0 '
                       'WHERE id=? AND project_id=?',
                       (item['title'], item['url'], item['source'], item['snippet'], item.get('published_at'),
                        _json(extra), item_id, self.project_id))
            if existing['event_id']:
                record_event(db, self.project_id, existing['event_id'])
        if not older_source:
            db.execute('INSERT INTO studio_collection_sources VALUES (?,?,?,?,?) '
                       'ON CONFLICT(project_id,identity) DO UPDATE SET evidence_version_id=excluded.evidence_version_id,'
                       'observed_at=excluded.observed_at', (self.project_id, identity, item_id, record['id'], observed))
        db.execute('INSERT OR IGNORE INTO studio_collection_observations VALUES (?,?,?,?,?,?)',
                   (self.execution.job['id'], unit['id'], item_id, record['id'], operation, observed))
        if operation != 'historical':
            evidence_tasks.admit(db, self.execution, record['id'])
        return dict(item_id=item_id, evidence_version_id=record['id'], identity=identity,
                    operation=prior['operation'] if prior else operation, observed_at=prior['observed_at'] if prior else observed,
                    source_type=record['source_type'], read_scope=record['read_scope'],
                    direction=unit['category'], target_id=unit['target_id'])
