"""Personal SQLite article snapshots. Reads never dispatch network/model work.

One durable attempt per discovered signal/version/extractor. A lost lease is a
recorded unavailable attempt, not permission for unlimited fetch retries.
"""
import hashlib
import json
import secrets
import time
from contextlib import closing

from project_watch.public_article import EXTRACTION_VERSION, MAX_TEXT_CHARS

LEASE_SECONDS = 120


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def ensure_article_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS public_article_fetches (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
        signal_id TEXT NOT NULL REFERENCES signals(id), source_url TEXT NOT NULL,
        signal_version TEXT NOT NULL, extraction_version TEXT NOT NULL,
        token TEXT NOT NULL, lease_until REAL NOT NULL, created_at REAL NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS public_article_snapshots (
        fetch_id TEXT PRIMARY KEY REFERENCES public_article_fetches(id),
        payload_json TEXT NOT NULL, snapshot_digest TEXT NOT NULL, fetched_at REAL NOT NULL)''')
    for op in ('UPDATE', 'DELETE'):
        db.execute(f'''CREATE TRIGGER IF NOT EXISTS public_article_snapshot_no_{op.lower()}
            BEFORE {op} ON public_article_snapshots
            BEGIN SELECT RAISE(ABORT,'article snapshots are immutable'); END''')
    db.execute('''CREATE TRIGGER IF NOT EXISTS public_article_fetch_identity
        BEFORE UPDATE OF id,project_id,signal_id,source_url,signal_version,extraction_version,token,created_at
        ON public_article_fetches BEGIN SELECT RAISE(ABORT,'article identity is immutable'); END''')


class ArticleStore:
    def __init__(self, repo, *, initialize=True):
        self.repo = repo
        if initialize:
            with closing(repo._connect()) as db, db:
                ensure_article_schema(db)

    def _identity(self, project_id, signal_id):
        with closing(self.repo._connect()) as db:
            row = db.execute('SELECT * FROM signals WHERE id=? AND project_id=?', (signal_id, project_id)).fetchone()
            prior = None if row is None else db.execute('''SELECT id,extraction_version
                FROM public_article_fetches WHERE project_id=? AND signal_id=? AND source_url=?
                AND signal_version=? ORDER BY created_at,id LIMIT 1''',
                (project_id, signal_id, row['url'], row['content_sha256'])).fetchone()
        if row is None:
            raise ValueError('discovered project signal required')
        identity = dict(project_id=project_id, signal_id=signal_id, source_url=row['url'],
                        signal_version=row['content_sha256'], extraction_version=EXTRACTION_VERSION)
        # An extractor upgrade is not consent to refetch/rebill all old stories.
        # Keep the first attempt (including failure), its citations and job key.
        if prior is not None:
            return dict(id=prior['id'], **dict(identity, extraction_version=prior['extraction_version']))
        return dict(id=_digest(identity), **identity)

    def get(self, project_id, signal_id):
        identity = self._identity(project_id, signal_id)
        with closing(self.repo._connect()) as db:
            row = db.execute('SELECT * FROM public_article_snapshots WHERE fetch_id=?', (identity['id'],)).fetchone()
        if row is None:
            return None
        return dict(json.loads(row['payload_json']), snapshot_digest=row['snapshot_digest'])

    def claim(self, project_id, signal_id):
        identity = self._identity(project_id, signal_id)
        now = time.time()
        with closing(self.repo._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM public_article_fetches WHERE id=?', (identity['id'],)).fetchone()
            if old is not None:
                if old['lease_until'] < now and not db.execute(
                        'SELECT 1 FROM public_article_snapshots WHERE fetch_id=?', (identity['id'],)).fetchone():
                    self._save(db, dict(old), self._failure(old['source_url'], 'interrupted',
                                                          version=old['extraction_version']))
                return None
            claim = dict(**identity, token=secrets.token_hex(24), lease_until=now+LEASE_SECONDS, created_at=now)
            db.execute('INSERT INTO public_article_fetches VALUES(?,?,?,?,?,?,?,?,?)', tuple(claim.values()))
        return claim

    @staticmethod
    def _failure(url, code, *, version=EXTRACTION_VERSION):
        return dict(source_url=url, resolved_url=None, body_sha256=None, status='unavailable',
                    text='', paragraphs=[], extraction_version=version, error_code=code)

    @staticmethod
    def _save(db, claim, result):
        now = time.time()
        payload = dict(result, id=claim['id'], project_id=claim['project_id'], signal_id=claim['signal_id'],
                       signal_version=claim['signal_version'], fetched_at=now)
        db.execute('INSERT INTO public_article_snapshots VALUES(?,?,?,?)',
                   (claim['id'], _json(payload), _digest(payload), now))

    def read_once(self, project_id, signal_id, reader):
        claim = self.claim(project_id, signal_id)
        if claim is None:
            return self.get(project_id, signal_id)
        try:
            result = reader.read(claim['source_url'])
            if (result['source_url'] != claim['source_url'] or result['extraction_version'] != EXTRACTION_VERSION
                    or result['status'] not in {'article_body', 'partial_body', 'summary_only', 'unavailable'}
                    or len(result['text']) > MAX_TEXT_CHARS
                    or any(result['text'][p['start']:p['end']] != p['text'] for p in result['paragraphs'])):
                raise ValueError('invalid article extraction')
        except Exception:
            result = self._failure(claim['source_url'], 'fetch_failed')
        with closing(self.repo._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM public_article_snapshots WHERE fetch_id=?', (claim['id'],)).fetchone():
                if claim['lease_until'] < time.time():
                    result = self._failure(claim['source_url'], 'interrupted')
                self._save(db, claim, result)
        return self.get(project_id, signal_id)
