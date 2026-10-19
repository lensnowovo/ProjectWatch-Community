"""Journalled public article reads shared by Core and optional analysis."""

import hashlib
import json

from project_watch.public_article import READER_FAILURE_CODES, safe_article_links
from project_watch.studio.http import HttpError, Response
from project_watch.studio.public_profile import safe_url


def read_page(execution, reader, url):
    def read():
        try:
            result = reader.read(url)
        except HttpError as exc:
            # An explicit publisher rejection is a known read result, not an
            # uncertain paid outcome. Preserve it through same-job replay.
            if exc.code != 'http_failure' or not exc.status or not 400 <= exc.status < 600:
                raise
            result = {'status': 'unavailable', 'text': '', 'error_code': 'http_failure'}
        # Persist public extracted text/status only, not arbitrary headers/reader diagnostics.
        error = result.get('error_code')
        resolved = result.get('resolved_url')
        safe = {'status': str(result.get('status', 'unavailable'))[:50], 'text': str(result.get('text') or '')[:24000],
                'resolved_url': resolved if safe_url(resolved or '') else None,
                'error_code': error if error in READER_FAILURE_CODES else 'fetch_failed' if error else None}
        if safe['status'] == 'summary_only' and safe['error_code'] == 'insufficient_article_text':
            safe['links'] = safe_article_links(safe['resolved_url'] or url, result.get('links', []))
        return Response(200, {'content-type': 'application/json'}, json.dumps(safe).encode())
    key = hashlib.sha256(url.encode()).hexdigest()
    return json.loads(execution.call('page', key, read, provider='guarded_public_reader', read_only=True).body)
