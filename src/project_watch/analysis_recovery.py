"""Append-only failed-attempt diagnostics and explicit, consent-bound retry lineage."""
import hmac
import json
import time
from contextlib import closing
from decimal import Decimal
from uuid import uuid4

from project_watch.external_analysis import digest
from project_watch.reading_jobs import ReadingDenied


def ensure_recovery_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS external_analysis_failures (
        id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES external_analysis_jobs(id),
        code TEXT NOT NULL, created_at REAL NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS external_analysis_retries (
        parent_id TEXT PRIMARY KEY REFERENCES external_analysis_jobs(id),
        child_id TEXT NOT NULL UNIQUE REFERENCES external_analysis_jobs(id),
        disclosure_digest TEXT NOT NULL, actor TEXT NOT NULL, created_at REAL NOT NULL)''')
    db.execute('CREATE INDEX IF NOT EXISTS external_analysis_failures_job ON external_analysis_failures(job_id,id)')
    for table in ('external_analysis_failures', 'external_analysis_retries'):
        for op in ('UPDATE', 'DELETE'):
            db.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_no_{op.lower()} BEFORE {op} ON {table}
                BEGIN SELECT RAISE(ABORT, 'analysis recovery history is append only'); END''')


def latest_job(db, job_id):
    while True:
        child = db.execute('SELECT child_id FROM external_analysis_retries WHERE parent_id=?', (job_id,)).fetchone()
        if child is None:
            return job_id
        job_id = child[0]


def _view(store, db, project_id, job_id, config_id, *, check_budget=True):
    row = db.execute('SELECT * FROM external_analysis_jobs WHERE id=? AND project_id=?',
                     (job_id, project_id)).fetchone()
    if row is None or row['state'] not in {'uncertain', 'failed'}:
        raise ReadingDenied('no failed analysis in this project')
    grant = store._grant(db, row['authorization_id'])
    if grant['config_id'] != config_id:
        raise ReadingDenied('retry receiver changed')
    packet = json.loads(row['packet_json'])
    store._scope(grant, packet, db)
    limits = json.loads(grant['limits_json'])
    charges = db.execute('SELECT reserved_cny FROM external_analysis_dispatches WHERE authorization_id=?',
                         (grant['id'],)).fetchall()
    if check_budget and (len(charges) >= limits['max_requests'] or (
            limits['max_cost_cny'] is not None
            and sum((Decimal(r[0]) for r in charges), Decimal(0)) >= Decimal(limits['max_cost_cny']))):
        raise ReadingDenied('retry budget exhausted')
    value = dict(project_id=project_id, job_id=job_id, authorization_id=grant['id'],
                 config_id=config_id, packet_digest=row['input_digest'],
                 recipient=json.loads(grant['recipient_json']), limits=limits,
                 references=packet['references'], max_additional_requests=1)
    return row, {**value, 'disclosure_digest': digest(value)}


def retry_preview(store, project_id, job_id, config_id):
    with closing(store.repo._connect()) as db:
        child = db.execute('SELECT child_id FROM external_analysis_retries WHERE parent_id=?', (job_id,)).fetchone()
        _, value = _view(store, db, project_id, job_id, config_id, check_budget=child is None)
        return {**value, 'existing_retry_id': child[0] if child else None}


def retry(store, project_id, job_id, config_id, disclosure_digest):
    with closing(store.repo._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        child = db.execute('SELECT child_id FROM external_analysis_retries WHERE parent_id=?', (job_id,)).fetchone()
        row, viewed = _view(store, db, project_id, job_id, config_id, check_budget=child is None)
        if not hmac.compare_digest(viewed['disclosure_digest'], disclosure_digest):
            raise ReadingDenied('retry disclosure changed')
        if child:
            child_id = child[0]
        else:
            child_id, now = str(uuid4()), time.time()
            db.execute('''INSERT INTO external_analysis_jobs
                (id,project_id,authorization_id,job_key,packet_json,input_digest,state,created_at,updated_at)
                VALUES(?,?,?,?,?,?,'queued',?,?)''',
                (child_id, project_id, row['authorization_id'], digest(['explicit-retry-v1', job_id]),
                 row['packet_json'], row['input_digest'], now, now))
            db.execute('INSERT INTO external_analysis_retries VALUES(?,?,?,?,?)',
                       (job_id, child_id, disclosure_digest, 'local-operator', now))
    return store.get(child_id)


def failures(store, project_id):
    with closing(store.repo._connect()) as db:
        rows = db.execute('''SELECT j.id,j.packet_json,j.state,j.updated_at,
            (SELECT code FROM external_analysis_failures f WHERE f.job_id=j.id ORDER BY f.id DESC LIMIT 1) AS code
            FROM external_analysis_jobs j WHERE j.project_id=? AND j.state IN ('failed','uncertain')
            AND NOT EXISTS (SELECT 1 FROM external_analysis_retries r WHERE r.parent_id=j.id)
            ORDER BY j.updated_at DESC,j.id''', (project_id,)).fetchall()
    result = []
    for row in rows:
        packet = json.loads(row['packet_json'])
        title = next((r['text'] for r in packet['references'] if r['ref_id'] == 'news:title'), '公开消息')
        result.append(dict(id=row['id'], signal_id=packet['signal_id'], title=title,
                           code=row['code'] or 'legacy_unknown', state=row['state'], updated_at=row['updated_at']))
    return result
