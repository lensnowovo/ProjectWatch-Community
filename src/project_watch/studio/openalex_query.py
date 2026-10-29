"""Shared OpenAlex query boundary for legacy and paginated collection."""
import re

from project_watch.studio.http import HttpError


def scope_params(query, mode):
    if (mode not in ('topic', 'author', 'affiliation') or not isinstance(query, str)
            or not query.strip() or len(query) > 300 or any(ord(c) < 32 for c in query)):
        raise HttpError('invalid_collection_query')
    if mode == 'topic':
        return {'search': query}
    # OpenAlex search filters preserve commas in text, except comma+field:.
    # Encoding commas does NOT escape filter syntax. Preserve corporate names,
    # but do not allow a name to add another filter, OR or negation operator.
    if any(c in query for c in '|!') or re.search(r',\s*[\w.-]+\s*:', query):
        raise HttpError('invalid_collection_query')
    key = 'raw_affiliation_strings.search' if mode == 'affiliation' else 'raw_author_name.search'
    return {'filter': key + ':' + query}


def get_results(http, url):
    """Classify only received HTTP failures; never retry or reclassify unknown."""
    try:
        return http.get_json(url)
    except HttpError as exc:
        if exc.code != 'http_failure':
            raise
        code = ({400: 'invalid_collection_query', 422: 'invalid_collection_query',
                 401: 'http_source_access_denied', 403: 'http_source_access_denied',
                 429: 'http_source_rate_limited'}.get(exc.status)
                or ('http_source_unavailable' if exc.status and exc.status >= 500 else 'http_failure'))
        # Service error bodies can echo query input; retain status, not raw body.
        raise HttpError(code, status=exc.status) from exc
