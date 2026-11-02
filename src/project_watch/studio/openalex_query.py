"""Shared OpenAlex query boundary for legacy and paginated collection."""
import re

from project_watch.studio.http import HttpError

_WORD = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{3,}(?![A-Za-z0-9])")
_CODE = re.compile(r"[A-Za-z]+\s*-?\s*\d+[A-Za-z0-9]*")


def topic_terms(query):
    """A research topic an English index can match, or None.

    At least two words of three or more letters; otherwise a product code
    (DB-1305, BNT323) as an exact phrase. A single word (STATE) or a
    Chinese-only business phrase matches unrelated papers across every field.
    """
    text = " ".join(str(query or "").split())
    if not text or len(text) > 300:
        return None
    if len(_WORD.findall(text)) >= 2:
        return text
    code = _CODE.search(text)
    return '"' + re.sub(r"\s*-\s*", "-", code.group(0)) + '"' if code else None


def _plain_words(text):
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower())


# Words that appear in papers of every field; they never make a paper about the topic by themselves.
_GENERIC = frozenset({'state', 'system', 'systems', 'material', 'materials', 'high', 'fast', 'based', 'method',
                      'analysis', 'service', 'services', 'software', 'application', 'new', 'novel', 'data',
                      'model', 'study', 'technology', 'platform'})


def topic_match(search, *texts):
    """The exact code phrase, or one specific word of the topic, appears in the title or abstract.

    Words match on their stem, so "battery" finds "batteries". Measured on saved
    rounds, this keeps most papers the screening found relevant while dropping
    full-text hits on a common word.
    """
    body = " " + _plain_words(" ".join(texts)) + " "
    if search.startswith('"'):
        return " " + _plain_words(search).strip() + " " in body
    words = [w for w in _plain_words(search).split() if len(w) >= 3 and not w.isdigit()]
    specific = [w for w in words if w not in _GENERIC] or words
    return any(" " + w[:max(4, len(w) - 2)] in body for w in specific)


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
