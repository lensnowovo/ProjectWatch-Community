"""Fixed read-only commercial sources. Local owner data; model input only with the owner's 设置 consent
(``CommercialConfig.model_use``, used by deep analysis), never otherwise.

Provider claims are not independent verification. No arbitrary URLs/tools, no
retries after ambiguous paid calls, no numerical guesses for qualitative amounts.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from urllib.parse import quote, urlsplit

from project_watch.studio.http import HttpError, Response, system_proxies
from project_watch.studio.settings import COMMERCIAL_PRESETS, COMMERCIAL_PROVIDERS
from project_watch.studio.store import now

PRESETS = {p['id']: p for p in COMMERCIAL_PRESETS}
MAX_BYTES = 2_000_000
RECEIVED_ERRORS = frozenset({
    'commercial_protocol', 'commercial_tool_error', 'commercial_shape',
    'commercial_encoding', 'commercial_size',
})
SCHEMA = """
CREATE TABLE IF NOT EXISTS studio_commercial_preferences (
 project_id TEXT PRIMARY KEY REFERENCES projects(id), investment_status TEXT NOT NULL
 CHECK(investment_status IN ('watching','invested'))
);
CREATE TABLE IF NOT EXISTS studio_commercial_calls (
 project_id TEXT NOT NULL REFERENCES projects(id), call_key TEXT NOT NULL,
 provider TEXT NOT NULL, subject TEXT NOT NULL, status TEXT NOT NULL,
 data TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
 family_key TEXT NOT NULL DEFAULT '',
 PRIMARY KEY(project_id,call_key)
);
CREATE INDEX IF NOT EXISTS studio_commercial_subject ON studio_commercial_calls(project_id,provider,subject,created_at);
"""


def _text(value, limit=500):
    return str(value or '').strip()[:limit]


def _safe_url(value):
    text = _text(value, 2048)
    try:
        parts = urlsplit(text)
        if (parts.scheme == 'https' and parts.hostname and not parts.username and not parts.password
                and not re.search(r'api[_-]?key|token|authorization', parts.query, re.I)):
            return text
    except ValueError:
        pass
    return ''


def _inner(result):
    if not isinstance(result, dict) or result.get('isError'):
        raise HttpError('commercial_tool_error')
    content = result.get('content', [])
    if isinstance(content, list):
        for block in content[:10]:
            if isinstance(block, dict) and block.get('type') == 'text':
                try:
                    data = json.loads(block.get('text', ''))
                    if isinstance(data, (dict, list)):
                        if isinstance(data, dict) and (data.get('ok') is False or data.get('error')):
                            raise HttpError('commercial_tool_error')
                        return data
                except (ValueError, TypeError):
                    continue
    data = result.get('structuredContent')
    if isinstance(data, (dict, list)):
        return data
    raise HttpError('commercial_shape')


def _day(value):
    text = _text(value, 40)
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return ''


def normalize(provider, result, company, aliases):
    """Minimal source-specific owner projection; discard contacts and estimates."""
    data = _inner(result)
    records, rejected = [], 0
    truncated = False
    names = {company.casefold(), *(a.casefold() for a in aliases if len(a) >= 2)}
    def add(kind, title='', when='', amount='', round_='', investors='', url='', excerpt=''):
        records.append({'kind': kind, 'company': company, 'provider': provider,
                        'title': _text(title, 300), 'date': _day(when), 'amount': _text(amount, 120),
                        'round': _text(round_, 100), 'investors': _text(investors, 1000),
                        'url': _safe_url(url), 'excerpt': _text(excerpt, 2000)})
    if provider == 'xiniu':
        if not isinstance(data, dict):
            raise HttpError('commercial_shape')
        info = data.get('公司信息') or {}
        if not isinstance(info, dict) or _text(info.get('公司名称')).casefold() != company.casefold():
            return {'records': [], 'rejected': 1, 'truncated': False}
        rows = data.get('融资事件详情') or []
        news = data.get('融资相关新闻') or []
        if not isinstance(rows, list) or not isinstance(news, list):
            raise HttpError('commercial_shape')
        for row in rows[:30]:
            if not isinstance(row, dict):
                rejected += 1
                continue
            add('funding', when=row.get('融资日期'), amount=row.get('融资金额'),
                round_=row.get('融资轮次'), investors=row.get('投资方'))
        for row in news[:20]:
            if isinstance(row, dict):
                add('news', row.get('标题'), row.get('发布日期'), url=row.get('源链接'))
        truncated = len(rows) > 30 or len(news) > 20
    elif provider == 'pharmcube':
        if not isinstance(data, dict) or not isinstance(data.get('data'), list):
            raise HttpError('commercial_shape')
        for row in data['data'][:20]:
            intro_identity = isinstance(row, dict) and re.match(
                r'^\s*' + re.escape(company) + r'[，,。；;（(\s]', str(row.get('company_intro') or ''))
            if (not isinstance(row, dict) or (not intro_identity
                    and _text(row.get('company_name')).casefold() not in names
                    and _text(row.get('company_name_en')).casefold() not in names)):
                rejected += 1
                continue
            # Numeric amount_value fields can be estimates / differently denominated.
            # Display only the original textual amount, not a computed receipt.
            add('funding', row.get('event_name'), row.get('financing_date'), row.get('amount_str'),
                row.get('event_round_cn'), row.get('total_capitals'), excerpt=row.get('financing_purposes'))
        total = data.get('total', len(data['data']))
        if type(total) is not int or total < 0:
            raise HttpError('commercial_shape')
        truncated = total > 20
    elif provider == 'wind':
        # Official financial_docs text contains an attributed document list.
        rows = data if isinstance(data, list) else data.get('data', data.get('documents', []))
        if isinstance(rows, dict):
            rows = rows.get('news', rows.get('documents', rows.get('items', rows.get('results', []))))
        if not isinstance(rows, list):
            raise HttpError('commercial_shape')
        for row in rows[:5]:
            if not isinstance(row, dict):
                rejected += 1
                continue
            title = row.get('title', row.get('标题', ''))
            body = row.get('content', row.get('text', row.get('正文', '')))
            # Exact legal name or a >=3-char declared alias; no fuzzy entity merge.
            if not any(n in (str(title) + str(body)).casefold() for n in names if len(n) >= 3):
                rejected += 1
                continue
            add('news', title, row.get('publish_time', row.get('date', row.get('published_at', ''))),
                url=row.get('url', row.get('link', '')), excerpt=body)
        truncated = len(rows) > 5
    else:
        raise ValueError('unsupported commercial provider')
    return {'records': records, 'rejected': rejected, 'truncated': truncated}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def mcp_transport(proxy=''):
    """Fixed provider hosts only; SSE stops at response ID, not stream EOF."""
    proxies = {'http': proxy, 'https': proxy} if proxy else system_proxies()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies), _NoRedirect())
    allowed = {(urlsplit(p['endpoint']).hostname, urlsplit(p['endpoint']).path) for p in COMMERCIAL_PRESETS}
    def send(method, url, headers, body, timeout):
        parts = urlsplit(url)
        if (method != 'POST' or parts.scheme != 'https' or parts.username or parts.password
                or parts.port not in (None, 443) or (parts.hostname, parts.path) not in allowed):
            raise HttpError('commercial_destination')
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with opener.open(request, timeout=timeout) as response:
                hs = {k.lower(): v for k, v in response.headers.items()}
                if response.status in (202, 204):
                    return Response(response.status, hs, b'')
                if 'text/event-stream' in hs.get('content-type', ''):
                    if hs.get('content-encoding', 'identity') != 'identity':
                        raise HttpError('commercial_encoding')
                    chunks, size = [], 0
                    expected = json.loads(body).get('id')
                    deadline = time.monotonic() + timeout
                    while size <= MAX_BYTES and time.monotonic() < deadline:
                        line = response.readline(MAX_BYTES - size + 1)
                        if not line:
                            break
                        chunks.append(line)
                        size += len(line)
                        if line.startswith(b'data:'):
                            try:
                                if json.loads(line[5:]).get('id') == expected:
                                    break
                            except (ValueError, AttributeError):
                                pass
                    raw = b''.join(chunks)
                else:
                    raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise HttpError('commercial_size')
                return Response(response.status, hs, raw)
        except urllib.error.HTTPError as exc:
            return Response(exc.code, {}, b'')
        except (OSError, urllib.error.URLError, TimeoutError):
            raise HttpError('commercial_connection') from None
    return send


class MCPClient:
    def __init__(self, provider, key, transport):
        if provider not in PRESETS:
            raise ValueError('unsupported commercial provider')
        self.provider, self.transport, self.key = provider, transport, key
        self.url = PRESETS[provider]['endpoint']
        self.headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream',
                        'Accept-Encoding': 'identity', 'User-Agent': 'ProjectWatch/0.2'}
        if provider == 'xiniu':
            self.url += '?api_key=' + quote(key, safe='')
        else:
            self.headers['Authorization'] = 'Bearer ' + key
        self.sequence = 0

    def _rpc(self, method, params, *, notification=False):
        self.sequence += 1
        payload = {'jsonrpc': '2.0', 'method': method, 'params': params}
        if not notification:
            payload['id'] = self.sequence
        try:
            response = self.transport('POST', self.url, self.headers, json.dumps(payload, ensure_ascii=False).encode(), 40)
        except HttpError as exc:
            raise HttpError('commercial_connection', status=exc.status) from None
        except Exception:
            raise HttpError('commercial_connection') from None
        if response.status not in (200, 202, 204):
            raise HttpError('commercial_http', status=response.status)
        if notification:
            return {}
        sid = response.headers.get('mcp-session-id')
        if sid:
            if len(sid) > 500 or any(ord(c) < 32 for c in sid):
                raise HttpError('commercial_protocol')
            self.headers['Mcp-Session-Id'] = sid
        raw = response.body
        if len(raw) > MAX_BYTES:
            raise HttpError('commercial_size')
        if response.headers.get('content-encoding') == 'gzip':
            # gzip.decompress has no output bound; bounded stream instead.
            import io
            try:
                with gzip.GzipFile(fileobj=io.BytesIO(raw)) as gz:
                    raw = gz.read(MAX_BYTES + 1)
            except (OSError, EOFError):
                raise HttpError('commercial_encoding') from None
            if len(raw) > MAX_BYTES:
                raise HttpError('commercial_size')
        try:
            if 'text/event-stream' in response.headers.get('content-type', ''):
                results = [json.loads(line[5:]) for line in raw.splitlines() if line.startswith(b'data:')]
                message = next((r for r in results if isinstance(r, dict) and r.get('id') == self.sequence), {})
            else:
                message = json.loads(raw)
        except (ValueError, UnicodeError):
            raise HttpError('commercial_protocol') from None
        if (not isinstance(message, dict) or message.get('id') != self.sequence
                or 'error' in message or not isinstance(message.get('result'), dict)):
            raise HttpError('commercial_protocol')
        return message['result']

    def fetch(self, company, aliases, end):
        start = (date.fromisoformat(end) - timedelta(days=730)).isoformat()
        initialized = self._rpc('initialize', {'protocolVersion': '2025-03-26', 'capabilities': {},
            'clientInfo': {'name': 'ProjectWatch', 'version': '0.2'}})
        protocol = initialized.get('protocolVersion')
        if protocol not in ('2024-11-05', '2025-03-26', '2025-06-18'):
            raise HttpError('commercial_protocol')
        self.headers['MCP-Protocol-Version'] = protocol
        if self.provider != 'wind':
            self._rpc('notifications/initialized', {}, notification=True)
        if self.provider == 'xiniu':
            name, args = 'get_company_info', {'firm_name': company, 'aspect': '融资状况分析'}
        elif self.provider == 'pharmcube':
            name, args = 'pharmcube-mcp-investEvent', {'companyName': company, 'dateFrom': start, 'dateTo': end, 'pageNo': 0}
        else:
            start = (date.fromisoformat(end) - timedelta(days=180)).isoformat()
            name, args = 'get_financial_news', {'query': f'查询{company}（{"、".join(aliases[:3])}）{start}至{end}的融资与战略合作报道', 'top_k': 5}
        result = normalize(self.provider, self._rpc('tools/call', {'name': name, 'arguments': args}), company, aliases)
        # A vendor can echo credentials. Never persist them in owner projections.
        encoded = json.dumps(result, ensure_ascii=False)
        encoded_key = json.dumps(self.key, ensure_ascii=False)[1:-1]
        return json.loads(encoded.replace(encoded_key, '[redacted]')) if encoded_key else result


class CommercialStore:
    def __init__(self, store):
        self.store = store
        with closing(store._connect()) as db, db:
            db.executescript(SCHEMA)
            if 'family_key' not in {r[1] for r in db.execute('PRAGMA table_info(studio_commercial_calls)')}:
                db.execute("ALTER TABLE studio_commercial_calls ADD COLUMN family_key TEXT NOT NULL DEFAULT ''")

    def set_focus(self, project_id, value):
        if value not in ('watching', 'invested'):
            raise ValueError('请选择关注中或已投')
        with closing(self.store._connect()) as db, db:
            db.execute('INSERT INTO studio_commercial_preferences VALUES (?,?) ON CONFLICT(project_id) '
                       'DO UPDATE SET investment_status=excluded.investment_status', (project_id, value))

    def collect(self, project, config, *, execution=None, proxy='', client_factory=None, permitted=None):
        if not project.get('activated') or project.get('status') != 'active' or not project.get('legal_name'):
            return {}
        factory = client_factory or (lambda provider, key, proxy: MCPClient(provider, key, mcp_transport(proxy)))
        company, aliases = project['legal_name'], list(project.get('aliases') or [])[:12]
        if project.get('short_name') and project['short_name'] not in aliases:
            aliases.append(project['short_name'])
        day = date.today().isoformat()
        report = {}
        for provider in config.ready_providers():
            key = config.keys[provider]
            if permitted and not permitted(provider, key, company):
                continue
            family = hashlib.sha256(json.dumps(['commercial-v1', provider, key, company, aliases],
                                              ensure_ascii=False).encode()).hexdigest()
            signature = hashlib.sha256((family + ':' + day).encode()).hexdigest()
            at = now()
            with closing(self.store._connect()) as db, db:
                db.execute('BEGIN IMMEDIATE')
                current = self.store.get_project(project['id'])
                if (not current or current['status'] != 'active' or not current['activated']
                        or current.get('config_version') != project.get('config_version')):
                    break
                old = db.execute('SELECT * FROM studio_commercial_calls WHERE project_id=? AND call_key=?',
                                 (project['id'], signature)).fetchone()
                unresolved = db.execute("SELECT 1 FROM studio_commercial_calls WHERE project_id=? AND family_key=? "
                                        "AND call_key<>? AND status IN ('started','held') LIMIT 1",
                                        (project['id'], family, signature)).fetchone()
                # Older builds could store cache='error' while the paid journal
                # remained unknown. Expiry/day changes must not retry that ambiguity.
                legacy_keys = {hashlib.sha256(('commercial:' + r[0]).encode()).hexdigest()
                               for r in db.execute("SELECT call_key FROM studio_commercial_calls "
                                   "WHERE project_id=? AND family_key=? AND status='error'", (project['id'], family))}
                legacy_unknown = legacy_keys and any(r[0] in legacy_keys for r in db.execute(
                    "SELECT c.call_key FROM studio_job_calls c JOIN studio_jobs j ON j.id=c.job_id "
                    "WHERE j.project_id=? AND c.kind='commercial' AND c.status IN ('started','unknown')", (project['id'],)))
                if unresolved or legacy_unknown:
                    report[provider] = {'status': 'held', 'cached': False}
                    continue
                # A local calendar change must not invalidate a 24-hour paid
                # result. Reuse the same identity/key family across midnight.
                if not old:
                    recent = db.execute("SELECT status FROM studio_commercial_calls WHERE project_id=? "
                                        "AND family_key=? AND status IN ('ok','partial','error') "
                                        "AND expires_at>? ORDER BY created_at DESC LIMIT 1",
                                        (project['id'], family, at)).fetchone()
                    if recent:
                        report[provider] = {'status': recent['status'], 'cached': True}
                        continue
                replay = None
                if old and old['status'] in ('started', 'held'):
                    journal_key = hashlib.sha256(('commercial:' + signature).encode()).hexdigest()
                    completed = db.execute('SELECT c.response_body FROM studio_job_calls c JOIN studio_jobs j ON j.id=c.job_id '
                                           'WHERE j.project_id=? AND c.call_key=? AND c.kind=\'commercial\' '
                                           'AND c.status=\'completed\' ORDER BY c.created_at DESC LIMIT 1',
                                           (project['id'], journal_key)).fetchone()
                    if completed:
                        replay = json.loads(completed[0])
                    else:
                        report[provider] = {'status': 'held', 'cached': False}
                        continue
                if old and old['status'] not in ('started', 'held') and old['expires_at'] > at:
                    report[provider] = {'status': old['status'], 'cached': True}
                    continue
                # A conservative all-project intent cap; failures/uncertain calls
                # count too. This limits requests, not provider invoices.
                if replay is None:
                    intents = db.execute("SELECT count(*) FROM studio_commercial_calls WHERE created_at>=? "
                                         "AND status<>'limited'", (at[:10],)).fetchone()[0]
                    if intents >= config.daily_limit:
                        report[provider] = {'status': 'limited', 'cached': False}
                        continue
                db.execute('INSERT INTO studio_commercial_calls VALUES (?,?,?,?,?, ?,?,?,?) '
                           'ON CONFLICT(project_id,call_key) DO UPDATE SET status=excluded.status,data=excluded.data,'
                           'created_at=excluded.created_at,expires_at=excluded.expires_at',
                           (project['id'], signature, provider, company, 'started', '{}', at,
                            (datetime.now(UTC) + timedelta(hours=24)).isoformat(), family))
            try:
                def fetch(provider=provider, key=key):
                    try:
                        data = factory(provider, key, proxy).fetch(company, aliases, day)
                    except HttpError as exc:
                        if exc.status is None and exc.code not in RECEIVED_ERRORS:
                            raise  # A lost response still requires explicit recovery authorization.
                        # Journal received failures before interpreting them. Never persist
                        # vendor messages, credentials or unnormalized commercial payloads.
                        code = exc.code if exc.code in RECEIVED_ERRORS else 'commercial_http'
                        error = {'_commercial_error': {'code': code, 'status': exc.status}}
                        return Response(exc.status or 200, {}, json.dumps(error).encode())
                    return Response(200, {}, json.dumps(data, ensure_ascii=False).encode())
                if replay is not None:
                    data = replay
                else:
                    response = execution.call('commercial', signature, fetch, provider=provider + key) if execution else fetch()
                    data = response.json()
                if '_commercial_error' in data:
                    error = data['_commercial_error']
                    raise HttpError(error['code'], status=error['status'])
                status = 'partial' if data.get('rejected') or data.get('truncated') else 'ok'
            except HttpError as exc:
                status = 'error' if exc.status is not None or exc.code in RECEIVED_ERRORS else 'held'
                data = {'error': 'authorization' if exc.status in (401, 403) else 'quota' if exc.status == 429 else status}
            with closing(self.store._connect()) as db, db:
                db.execute('UPDATE studio_commercial_calls SET status=?,data=? WHERE project_id=? AND call_key=?',
                           (status, json.dumps(data, ensure_ascii=False), project['id'], signature))
            report[provider] = {'status': status, 'cached': False, 'records': len(data.get('records', []))}
        return report

    def view(self, project_id, config, *, targets=None):
        project = self.store.get_project(project_id) or {}
        targets = targets if targets is not None else [project]
        names = {p['legal_name']: p.get('relationship', 'company') for p in targets if p.get('legal_name')}
        with closing(self.store._connect()) as db:
            focus = db.execute('SELECT investment_status FROM studio_commercial_preferences WHERE project_id=?', (project_id,)).fetchone()
            rows = db.execute('SELECT * FROM studio_commercial_calls WHERE project_id=? '
                              'ORDER BY created_at DESC,call_key DESC LIMIT 180', (project_id,)).fetchall()
            used = db.execute('SELECT count(*) FROM studio_commercial_calls WHERE created_at>=?', (now()[:10],)).fetchone()[0]
        records, sources = [], []
        for company, relationship in names.items():
          for provider in COMMERCIAL_PROVIDERS:
            latest = next((r for r in rows if r['provider'] == provider and r['subject'] == company), None)
            saved = next((r for r in rows if r['provider'] == provider and r['subject'] == company and r['status'] in ('ok', 'partial')), None)
            status = latest['status'] if latest else 'not_collected'
            enabled = provider in config.ready_providers()
            if enabled and used >= config.daily_limit and (not latest or latest['expires_at'] < now()):
                status = 'limited'
            stale = bool(saved and (saved['expires_at'] < now() or status not in ('ok', 'partial') or not enabled))
            data = json.loads(saved['data']) if saved else {}
            for record in data.get('records', []):
                records.append({**record, 'collected_at': saved['created_at'], 'stale': stale, 'relationship': relationship})
            sources.append({'provider': provider, 'label': PRESETS[provider]['label'], 'enabled': enabled,
                            'company': company, 'relationship': relationship,
                            'status': status, 'last_checked': latest['created_at'] if latest else None,
                            'stale': stale, 'rejected': data.get('rejected', 0), 'truncated': data.get('truncated', False)})
        groups = {}
        for r in records:
            if r['kind'] == 'funding':
                groups.setdefault((r['company'], r['round']), []).append(r)
        different = any(len({r['provider'] for r in group}) > 1 and (
            len({r['amount'] for r in group if r['amount']}) > 1 or len({r['date'] for r in group if r['date']}) > 1)
            for group in groups.values())
        return {'investment_status': focus[0] if focus else 'watching', 'records': records, 'sources': sources,
                'daily_queries': {'used': used, 'limit': config.daily_limit, 'remaining': max(0, config.daily_limit-used)},
                'different_conventions': different,
                'notice': '来源记录，不代表融资款到账；日期与金额保留各家口径。',
                'private': True}


def targets(pipeline, project):
    """Do not query private identities, automatic candidates or excluded peers."""
    objects = pipeline.objects.page(project['id'])
    public = {r['id']: r for r in pipeline.objects.page(project['id'], include_internal=False)}
    if objects:
        primary = next((r for r in public.values() if r.get('purpose') == 'subject' and r['kind'] == 'company'), None)
        if not primary or primary['name'] != project.get('legal_name'):
            return []
        public_names = pipeline.objects.query_inputs(project['id'])['company_names']
        first = {**project, 'aliases': [n for n in public_names if n != primary['name']],
                 'short_name': project.get('short_name') if project.get('short_name') in public_names else ''}
    else:
        first = dict(project)
    result = [{**first, 'relationship': 'company'}]
    relations = pipeline.relationships.view(project['id'], include_internal=False)
    excluded = {r.get('peer_id') for r in relations['exclusions'] if r.get('company_wide')}
    for row in relations['items']:
        if row.get('peer_id') in excluded or row['relation_type'] == 'unrelated':
            continue
        if row.get('support') != 'supported' and row.get('origin') != 'user_override':
            continue
        if row.get('scope_identity_changed') or row.get('status') in ('stale', 'disputed'):
            continue
        peer = public.get(row.get('peer_id'))
        if not peer or peer['kind'] != 'company' or any(p['legal_name'] == peer['name'] for p in result):
            continue
        result.append({**project, 'legal_name': peer['name'], 'short_name': '', 'aliases': [],
                       'relationship': 'tracked_peer' if row.get('origin') == 'user_override' else 'supported_peer'})
        if len(result) == 3:
            break
    return result
