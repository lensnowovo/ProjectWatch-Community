"""Existing source adapters with job-local request and cache provenance."""
import hashlib
import json
from contextlib import closing
from copy import copy
from urllib.parse import urlsplit

from project_watch.studio.collection_pages import clinical_page, cninfo_page, openalex_page
from project_watch.studio.http import Http, HttpError


class CollectionSources:
    """Used serially before the DSH gateway; never wraps a second request journal.

    The supplied Collectors must already use the job's guarded, journalled HTTP
    and project query cache. Snapshot differences include journal replay hits,
    not just new rows, so receipt recovery cannot masquerade as a new request.
    """

    def __init__(self, execution, collectors, *, max_pages=3):
        if type(max_pages) is not int or not 1 <= max_pages <= 10:
            raise ValueError('invalid collection page limit')
        self.max_pages = max_pages
        self.execution, self.collectors = execution, copy(collectors)
        # Validate the received wire shape before legacy list conversion can
        # turn a missing result field into a seemingly successful empty list.
        original = collectors.http

        def send(method, url, headers, body, timeout):
            response = original.transport(method, url, headers, body, timeout)
            if response.status == 200:
                _validate_search_response(url, response)
            return response

        self.collectors.http = Http(send)

    def capabilities(self):
        ready = 'available' if self.collectors.has_search else 'not_configured'
        return (dict.fromkeys(('search', 'patent_search', 'cn_regulatory'), ready)
                | dict.fromkeys(('openalex_topic', 'openalex_author', 'openalex_affiliation',
                                 'clinical_sponsor', 'clinical_term', 'cninfo'), 'available'))

    def snapshot(self):
        job = self.execution.job
        with closing(self.execution.store._connect()) as db:
            requests = {r['call_key']: dict(r) for r in db.execute(
                "SELECT c.call_key,c.status,c.response_status,c.created_at,COALESCE(h.hits,0) replay_hits "
                "FROM studio_job_calls c LEFT JOIN studio_job_cache_hits h "
                "ON c.job_id=h.job_id AND c.call_key=h.call_key WHERE c.job_id=?", (job['id'],))}
            uses = {r['id']: dict(r) for r in db.execute(
                "SELECT u.id,u.query_key,u.outcome,u.created_at,c.completed_at FROM studio_query_uses u "
                "LEFT JOIN studio_query_cache c ON u.project_id=c.project_id AND u.query_key=c.query_key "
                "WHERE u.project_id=? AND u.job_id=?", (job['project_id'], job['id']))}
        return requests, uses

    def collect(self, unit):
        source = unit['source']
        if self.capabilities().get(source) != 'available':
            raise HttpError('collection_source_unavailable')
        before_requests, before_uses = self.snapshot()
        trace_stage = 'collection_trace:' + unit['id'][:40]
        previous = self.execution.read_checkpoint(trace_stage) or {'requests': [], 'query_uses': []}
        error = None
        details = {'coverage_kind': 'bounded_search', 'per_request_limit': 8 if source == 'cn_regulatory' else 10,
                   'window_applied': False, 'truncated': False}
        try:
            if source == 'search':
                items = self.collectors.web_search(unit['query'], count=10)
            elif source == 'patent_search':
                items = self.collectors.patents(unit['query'])
            elif source == 'cn_regulatory':
                items = self.collectors.cn_regulatory(unit['query'])
            else:
                details = self._pages(unit)
                items = details.pop('items')
        except HttpError as exc:
            error = exc.code
            raise
        finally:
            after_requests, after_uses = self.snapshot()
            requests = [r for key, r in after_requests.items() if before_requests.get(key) != r]
            uses = [r for key, r in after_uses.items() if key not in before_uses]
            pages = self.execution.read_checkpoint('collection_pages:' + unit['id'][:40]) or {}
            trace = dict(
                requests=list({r['call_key']: r for r in
                               [*previous['requests'], *pages.get('requests', []), *requests]}.values()),
                query_uses=list({r['id']: r for r in [*previous['query_uses'], *uses]}.values()),
                last_error=error)
            # Also retain provenance when parsing fails or a dispatch becomes
            # unknown. No URLs, credentials, queries or raw responses are copied.
            self.execution.checkpoint_chunked(trace_stage, trace)
        # A paginated resume may only replay its durable pages. The manifest is
        # frozen, so original requests remain valid provenance for these items.
        relevant_requests = trace['requests'] if source.startswith(('openalex_', 'clinical_')) or source == 'cninfo' else requests
        ids = [f"call:{self.execution.job['id']}:{r['call_key']}" for r in relevant_requests]
        cached = [r for r in uses if r['outcome'] == 'cached']
        ids.extend('query-use:' + r['id'] for r in cached)
        if not ids:
            raise HttpError('collection_request_provenance_missing')
        return dict(items=items, request_ids=ids, cache_provenance=cached,
                    request_provenance=trace, **details)

    def _pages(self, unit):
        stage = 'collection_pages:' + unit['id'][:40]
        continuation = unit.get('continuation') or {}
        state = self.execution.read_checkpoint(stage) or {'items': [], 'pages': 0, 'cursor': continuation.get('cursor'),
                                                        'done': False, 'metadata': {}, 'requests': [],
                                                        'visited': list(continuation.get('visited', [])), 'exhausted': False}
        paper = unit['source'].startswith('openalex_')
        filing = unit['source'] == 'cninfo'
        seen = {(item['extra']['source_identity'], item['extra']['source_version']) for item in state['items']}
        interruption = {}
        while not state['done'] and not state.get('exhausted') and state['pages'] < self.max_pages:
            cursor = state['cursor'] or (1 if paper or filing else None)
            before_requests, _ = self.snapshot()
            try:
                if cursor in state.get('visited', []):
                    raise HttpError('collection_cursor_cycle')
                if paper or filing:
                    method = openalex_page if paper else cninfo_page
                    options = {'mode': unit['source'][9:]} if paper else {}
                    page = method(self.collectors.http, unit['query'], page=state['cursor'] or 1,
                                  start=(unit.get('window_start') or '')[:10] or None,
                                  end=unit['window_end'][:10], **options)
                else:
                    page = clinical_page(self.collectors.http, unit['query'], mode=unit['source'][9:],
                                         cursor=state['cursor'])
            except HttpError as exc:
                # Only a known source failure or budget limit can close with
                # partial data. Pause/recovery/unknown gates retain the original
                # exception and stop the round before any further dispatch.
                recoverable = exc.code in ('request_budget', 'public_read_failed') or exc.code.startswith(
                    ('invalid_', 'inconsistent_', 'http_', 'collection_cursor_'))
                unknown = any(v['unknown'] for v in self.execution.usage().values())
                if not state['pages'] or not recoverable or unknown:
                    raise
                interruption = dict(outcome='budget_deferred' if exc.code == 'request_budget' else 'failed',
                                    reason=exc.code)
                break
            for item in page['items']:
                extra = item['extra']
                identity = extra['source_identity'] if filing else (
                    ('openalex:' + extra['openalex_id']) if paper else ('clinical:' + extra['nct_id']))
                version = hashlib.sha256(json.dumps(extra['source_record'], sort_keys=True,
                                                     ensure_ascii=False).encode()).hexdigest()
                extra.update(source_identity=identity, source_version=version)
                if (identity, version) not in seen:
                    state['items'].append(item)
                    seen.add((identity, version))
            state['pages'] += 1
            state.setdefault('visited', []).append(cursor)
            state['cursor'] = page['next_cursor']
            state['metadata'] = {key: value for key, value in page.items() if key != 'items'}
            state['done'] = not page['truncated']
            state['exhausted'] = page['truncated'] and state['cursor'] is None
            after_requests, _ = self.snapshot()
            changed = [r for key, r in after_requests.items() if before_requests.get(key) != r]
            state['requests'] = list({r['call_key']: r for r in
                                      [*state.get('requests', []), *changed]}.values())
            # Store the page and its request identities atomically. A crash
            # before the outer trace write must not orphan recovered evidence.
            # A source cap can report truncation without a usable continuation.
            self.execution.checkpoint_chunked(stage, state)
            if page['truncated'] and state['cursor'] is None:
                break
        return dict(state['metadata'], items=state['items'], truncated=not state['done'],
                    next_cursor=state['cursor'], pages_completed=state['pages'],
                    visited_cursors=state['visited'], **interruption)


def _validate_search_response(url, response):
    host, path = urlsplit(url).hostname, urlsplit(url).path
    keys = {
        ('api.tavily.com', '/search'): ('results',),
        ('api.deepseek.com', '/v1/tools/search'): ('results',),
        ('api.bochaai.com', '/v1/web-search'): ('data', 'webPages', 'value'),
        ('api.search.brave.com', '/res/v1/web/search'): ('web', 'results'),
        ('api.search.brave.com', '/res/v1/news/search'): ('results',),
    }.get((host, path))
    if keys is None:
        return
    value = response.json()
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            raise HttpError('invalid_search_response')
        value = value[key]
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise HttpError('invalid_search_response')
