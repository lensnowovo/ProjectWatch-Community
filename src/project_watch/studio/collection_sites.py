"""Official-site snapshots using the existing public-IP and robots path."""
import base64
import hashlib
import json

from project_watch.feeds import GuardedHTTPS
from project_watch.studio import monitor
from project_watch.studio.http import HttpError, Response
from project_watch.studio.sources import _item


class CollectionSites:
    def __init__(self, research, sources):
        self.research, self.sources = research, sources
        self.transport = getattr(research.reader, 'transport', None)

    @property
    def capability(self):
        return 'available' if isinstance(self.transport, GuardedHTTPS) else 'not_integrated'

    def collect(self, unit, guard):
        if self.capability != 'available':
            raise HttpError('collection_source_unavailable')
        research, execution = self.research, self.research.execution
        stage = 'collection_site:' + unit['id'][:40]
        state = execution.read_checkpoint(stage) or {'items': {}, 'requests': []}
        before, _ = self.sources.snapshot()

        def connector(host, ip, path):
            guard()

            def request():
                status, headers, body = self.transport.connector(host, ip, path)
                # The journal's normal safe-header subset omits Location. Keep
                # the exact public redirect response in the durable payload so
                # replay follows the same path without issuing that GET again.
                payload = json.dumps({'headers': headers, 'body': base64.b64encode(body).decode()}).encode()
                return Response(status, headers, payload)

            key = hashlib.sha256(('collection-site:https://' + host + path).encode()).hexdigest()
            try:
                saved = execution.call('page', key, request, provider='collection_site:' + host,
                                       read_only=True, public_read=True)
            except Exception as exc:
                if any(row['unknown'] for row in execution.usage().values()):
                    raise HttpError('request_outcome_unknown') from exc
                raise
            payload = json.loads(saved.body)
            return saved.status, payload['headers'], base64.b64decode(payload['body'])

        transport = GuardedHTTPS(resolver=self.transport.resolver, connector=connector,
                                 fatal_errors=(RuntimeError,))

        def retain_requests():
            after, _ = self.sources.snapshot()
            changed = [r for key, r in after.items() if before.get(key) != r]
            state['requests'] = list({r['call_key']: r for r in [*state['requests'], *changed]}.values())

        def snapshot(page, blocks):
            guard()
            item = _item('company', page['label'], page['url'], source='公司官网', snippet='\n'.join(blocks),
                         extra={'source_identity': 'site:' + page['id'],
                                'source_record': {'url': page['url'], 'blocks': blocks,
                                                  'scope': 'visible_text_snapshot'},
                                'page_kind': page['kind']})
            state['items'][page['id']] = item
            retain_requests()
            execution.checkpoint_chunked(stage, state)

        with research.store.write_scope(execution._guard):
            report = monitor.check_sites(research.pipeline.monitor, transport, research.project_id,
                                         unit['query'], execution=execution, force=True,
                                         record_changes=False, on_snapshot=snapshot, guard=guard,
                                         journal_requests=False)
        retain_requests()
        execution.checkpoint_chunked(stage, state)
        pages = research.pipeline.monitor.pages(research.project_id)
        incomplete = [p for p in pages if p['status'] != 'ok' or p['id'] not in state['items']]
        errors = report['errors'] + [p.get('error') or p['status'] for p in incomplete]
        budget = any('request_budget' in error for error in errors)
        if not state['requests']:
            raise HttpError('request_budget' if budget else 'collection_request_provenance_missing')
        partial = bool(errors) or not pages
        details = dict(outcome='budget_deferred' if budget else 'failed',
                       reason='request_budget' if budget else 'site_pages_unavailable') if partial else {}
        return dict(items=list(state['items'].values()), truncated=partial,
                    request_ids=[f"call:{execution.job['id']}:{r['call_key']}" for r in state['requests']],
                    request_provenance={'requests': state['requests']}, coverage_kind='registered_site_pages',
                    window_applied=False, pages_checked=report['checked'], **details)
