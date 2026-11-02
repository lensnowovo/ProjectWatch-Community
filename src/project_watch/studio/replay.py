"""Offline record/replay of provider HTTP, for development and diagnosis only.

The corpus is a separate SQLite file kept outside the repository. Nothing here
is used by a normal installation; a developer installs it explicitly with
``Pipeline.http_factory = replay_factory(...)``.

Two lookups, in order:

1. A secret-free request key (method, URL and body with credentials removed).
   ``record`` writes these, so recorded search/model traffic can be replayed
   without the original keys.
2. The request journal key ``sha256(kind + ':' + sha256([method, url, headers]
   + body))``. It does not depend on the job, so completed responses imported
   from an existing database replay exactly for any request whose headers and
   body carry no secret (OpenAlex, Europe PMC, patent pages, trials...).

Only completed journal rows are imported. Unknown or started requests are never
turned into a successful response. A miss raises ``ReplayMiss`` — a connection
failure, because nothing was sent — and is listed, so a replay run never
silently reaches the network and a missing source fails like an unreachable one.
"""
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from project_watch.studio.http import Http, Response

JOURNAL_KINDS = ('open_data', 'page', 'site', 'search', 'model', 'commercial')
SECRET_HEADERS = frozenset(('authorization', 'x-api-key', 'api-key', 'cookie', 'x-subscription-token', 'proxy-authorization'))
SECRET_FIELDS = frozenset(('api_key', 'apikey', 'apiKey', 'key', 'token', 'access_token', 'secret'))
SCHEMA = '''
CREATE TABLE IF NOT EXISTS replay_journal (
    kind TEXT NOT NULL, call_key TEXT NOT NULL, response_status INTEGER NOT NULL,
    response_headers TEXT NOT NULL, response_body BLOB NOT NULL, origin TEXT NOT NULL,
    PRIMARY KEY(kind, call_key));
CREATE TABLE IF NOT EXISTS replay_requests (
    request_key TEXT PRIMARY KEY, method TEXT NOT NULL, url TEXT NOT NULL, body TEXT,
    response_status INTEGER NOT NULL, response_headers TEXT NOT NULL, response_body BLOB NOT NULL,
    recorded_at TEXT NOT NULL);
'''


def _clean_url(url):
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in SECRET_FIELDS]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ''))


def _clean_body(body):
    if not body:
        return ''
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return hashlib.sha256(body).hexdigest()

    def scrub(value):
        if isinstance(value, dict):
            return {k: scrub(v) for k, v in value.items() if k not in SECRET_FIELDS}
        if isinstance(value, list):
            return [scrub(v) for v in value]
        return value
    return json.dumps(scrub(data), ensure_ascii=False, sort_keys=True)


def request_key(method, url, body):
    """Secret-free identity of a request; headers are deliberately excluded."""
    return hashlib.sha256(json.dumps([method, _clean_url(url), _clean_body(body)], ensure_ascii=False).encode()).hexdigest()


def journal_key(kind, method, url, headers, body):
    """Identical to JobExecution.http followed by JobExecution.call."""
    digest = hashlib.sha256(json.dumps([method, url, sorted(headers.items())]).encode() + (body or b'')).hexdigest()
    return hashlib.sha256((kind + ':' + digest).encode()).hexdigest()


class ReplayMiss(ConnectionError):
    """No recorded response: equivalent to failing before the request was sent."""


class Corpus:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.executescript(SCHEMA)

    def _connect(self):
        return sqlite3.connect(self.path)

    def import_journal(self, database, *, origin=''):
        """Copy completed responses from a (copied) ProjectWatch database, read-only."""
        source = sqlite3.connect(f'file:{Path(database).resolve().as_posix()}?mode=ro', uri=True)
        added = 0
        with closing(source), closing(self._connect()) as db, db:
            rows = source.execute("SELECT kind,call_key,response_status,response_headers,response_body FROM studio_job_calls "
                                  "WHERE status='completed' AND response_body IS NOT NULL")
            for kind, key, status, headers, body in rows:
                added += db.execute('INSERT OR IGNORE INTO replay_journal VALUES (?,?,?,?,?,?)',
                                    (kind, key, status, headers or '{}', body, origin or Path(database).name)).rowcount
        return added

    def record(self, method, url, body, response):
        from project_watch.studio.store import now

        with closing(self._connect()) as db, db:
            db.execute('INSERT OR REPLACE INTO replay_requests VALUES (?,?,?,?,?,?,?,?)',
                       (request_key(method, url, body), method, _clean_url(url), _clean_body(body), response.status,
                        json.dumps({k: v for k, v in response.headers.items() if k.lower() not in SECRET_HEADERS}),
                        response.body, now()))

    def lookup(self, method, url, headers, body):
        with closing(self._connect()) as db:
            row = db.execute('SELECT response_status,response_headers,response_body FROM replay_requests WHERE request_key=?',
                             (request_key(method, url, body),)).fetchone()
            if row:
                return Response(row[0], json.loads(row[1]), row[2]), 'recorded'
            for kind in JOURNAL_KINDS:
                row = db.execute('SELECT response_status,response_headers,response_body FROM replay_journal WHERE kind=? AND call_key=?',
                                 (kind, journal_key(kind, method, url, headers, body))).fetchone()
                if row:
                    return Response(row[0], json.loads(row[1]), row[2]), 'journal:' + kind
        return None, None

    def stats(self):
        with closing(self._connect()) as db:
            return {'journal': dict(db.execute('SELECT kind,COUNT(*) FROM replay_journal GROUP BY kind').fetchall()),
                    'recorded': db.execute('SELECT COUNT(*) FROM replay_requests').fetchone()[0]}


class ReplayTransport:
    """Transport for ``Http``. ``replay`` never reaches the network; ``record`` always does."""

    def __init__(self, corpus, *, mode='replay', upstream=None):
        if mode not in ('replay', 'record'):
            raise ValueError('invalid replay mode')
        if mode == 'record' and upstream is None:
            raise ValueError('record mode needs an upstream transport')
        self.corpus, self.mode, self.upstream = corpus, mode, upstream
        self.hits, self.misses = [], []

    def __call__(self, method, url, headers, body, timeout):
        if self.mode == 'record':
            response = self.upstream(method, url, headers, body, timeout)
            self.corpus.record(method, url, body, response)
            return response
        response, source = self.corpus.lookup(method, url, headers, body)
        if response is None:
            self.misses.append(dict(method=method, url=_clean_url(url), host=urlsplit(url).hostname))
            raise ReplayMiss('replay_miss: ' + _clean_url(url))
        self.hits.append(dict(method=method, url=_clean_url(url), source=source))
        return response

    def report(self):
        hosts = {}
        for row in self.misses:
            hosts[row['host']] = hosts.get(row['host'], 0) + 1
        return {'hits': len(self.hits), 'misses': len(self.misses), 'missed_hosts': hosts,
                'by_source': {s: sum(h['source'] == s for h in self.hits) for s in {h['source'] for h in self.hits}}}


def replay_factory(transport):
    """Drop-in for ``Pipeline.http_factory``: every client uses the same transport."""
    return lambda _settings: Http(transport)


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) >= 2 and args[0] == 'import':
        corpus = Corpus(args[1])
        for database in args[2:]:
            print(Path(database).name, corpus.import_journal(database), 'responses imported')
        print(json.dumps(corpus.stats()))
        return 0
    if len(args) == 2 and args[0] == 'stats':
        print(json.dumps(Corpus(args[1]).stats()))
        return 0
    print('usage: python -m project_watch.studio.replay import <corpus.sqlite3> <copied-db>... | stats <corpus.sqlite3>')
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
