"""Bounded, passive public article extraction. Content is evidence, never instructions.

No JavaScript execution, cookies, automatic crawling or access-control bypass.
Visible same-origin anchors are passive discovery hints, not article evidence.
An extracted body is not a claim of completeness or publisher correctness.
"""

import hashlib
import ipaddress
import json
import re
from contextlib import suppress
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from project_watch.feeds import MAX_FEED_BYTES, FeedError, GuardedHTTPS, _resolve_public

EXTRACTION_VERSION = 'public-article-v3'
MAX_TEXT_CHARS = 24000
_BLOCKS = {'p', 'h1', 'h2', 'h3', 'h4', 'li', 'blockquote', 'td'}
_SKIP = {'script', 'style', 'noscript', 'nav', 'footer', 'header', 'form',
         'button', 'select', 'svg', 'aside', 'iframe', 'template'}
_VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link',
         'meta', 'param', 'source', 'track', 'wbr'}
_BARRIERS = re.compile(
    r'登录后(?:阅读|查看)|登录(?:后)?阅读全文|付费(?:后)?阅读|订阅后(?:阅读|查看)|'
    r'verify you are human|checking your browser|captcha|access denied|'
    r'subscribe to (?:read|continue)|sign in to (?:read|continue)|enable javascript and cookies', re.I)


class _Blocks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.blocks = []
        self.pending = []
        self.semantic = False
        self.ld = None
        self.structured = []
        self.visible = []
        self.anchor = None
        self.links = []

    def flush(self):
        value = re.sub(r'\s+', ' ', ''.join(self.pending)).strip()
        if value:
            self.blocks.append((self.semantic, value))
        self.pending = []

    def handle_starttag(self, tag, attrs):
        # Valueless attributes (<span aria-hidden>, <div class>) arrive as None.
        attrs = {key: value or '' for key, value in attrs}
        if (tag == 'script' and attrs.get('type', '').lower() == 'application/ld+json'
                and not (self.stack and self.stack[-1][1])):
            self.ld = []
        if tag in _BLOCKS or tag in {'div', 'article', 'main', 'section'}:
            self.flush()
        # Browsers implicitly close <p>/<li>; malformed publishers should not
        # accumulate thousands of stack frames or hide later ordinary paragraphs.
        if tag in {'p', 'li'} and self.stack and self.stack[-1][0] == tag:
            self.stack.pop()
        hidden = ('hidden' in attrs or attrs.get('aria-hidden', '').lower() == 'true'
                  or bool(re.search(r'display\s*:\s*none|visibility\s*:\s*hidden', attrs.get('style', ''), re.I)))
        decor = re.search(r'(?:^|[\s_-])(nav|footer|copyright|recommend|related|share|toolbar|breadcrumb|pagination|advertisement)(?:$|[\s_-])',
                          (attrs.get('class', '')+' '+attrs.get('id', '')).lower())
        skip = bool(tag in _SKIP or hidden or decor or (self.stack and self.stack[-1][1]))
        body_container = (tag in {'div', 'section', 'main'}
                          and 'articleBody' in attrs.get('itemprop', '').split())
        semantic = 2 if tag == 'article' or body_container else (self.stack[-1][2] if self.stack else 0)
        if tag == 'main' and not semantic:
            semantic = 1
        if tag not in _VOID:
            if len(self.stack) >= 128:
                raise ValueError('excessive HTML nesting')
            self.stack.append((tag, bool(skip), semantic))
        if tag == 'a':
            self.anchor = None if skip or 'download' in attrs else [attrs.get('href', ''), []]
        if tag == 'br':
            self.pending.append(' ')

    def handle_endtag(self, tag):
        if tag == 'a' and self.anchor is not None:
            if len(self.links) < 80:
                self.links.append({'url': self.anchor[0], 'title': ''.join(self.anchor[1])})
            self.anchor = None
        if tag == 'script' and self.ld is not None:
            with suppress(ValueError, RecursionError):
                self.structured.append(json.loads(''.join(self.ld)))
            self.ld = None
        if tag in _BLOCKS or tag in {'div', 'article', 'main', 'section'}:
            self.flush()
        for i in range(len(self.stack)-1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if self.ld is not None:
            self.ld.append(data)
        if not self.stack or self.stack[-1][1]:
            return
        self.visible.append(data)
        if self.anchor is not None and sum(map(len, self.anchor[1])) < 1000:
            self.anchor[1].append(data[:1000])
        if self.stack[-1][2] == 2 or any(tag in _BLOCKS for tag, _, _ in self.stack):
            self.semantic = self.stack[-1][2]
            self.pending.append(data)


def _empty(status='summary_only', error_code=None):
    return dict(status=status, text='', paragraphs=[], extraction_version=EXTRACTION_VERSION,
                error_code=error_code)


def _structured_body(values):
    """Only schema.org articleBody, never executable script state or extra URLs."""
    pending = [(v, 0) for v in values]
    bodies = []
    visited = 0
    while pending and visited < 5000:
        value, depth = pending.pop()
        visited += 1
        if depth > 10:
            continue
        if isinstance(value, dict):
            # A nested article cannot escape an enclosing paid-content flag.
            if value.get('isAccessibleForFree', True) in (False, 'false'):
                continue
            kinds = value.get('@type', [])
            kinds = [kinds] if isinstance(kinds, str) else kinds
            if (isinstance(kinds, list) and any(t in {'Article', 'NewsArticle', 'BlogPosting'} for t in kinds if isinstance(t, str))
                    and isinstance(value.get('articleBody'), str)
                    and value.get('isAccessibleForFree', True) not in (False, 'false')):
                # Markup in structured strings is ambiguous; do not render or
                # interpret it as HTML/instructions. Visible HTML is preferred.
                body = value['articleBody']
                if '<' not in body and len(body.strip()) >= 100:
                    bodies.append(body)
            pending.extend((v, depth+1) for v in value.values() if isinstance(v, (list, dict)))
        elif isinstance(value, list):
            pending.extend((v, depth+1) for v in value if isinstance(v, (list, dict)))
    return max(bodies, key=len, default='')


def safe_article_links(parent, links):
    """Bounded actual same-origin anchors; readers still validate/pin DNS on GET.

    No queries, credentials, fragments, downloads, base href or alternate ports.
    This does not certify company ownership or article relevance.
    """
    try:
        GuardedHTTPS._target(parent)
        origin = urlsplit(parent)
    except (FeedError, TypeError, ValueError):
        return []
    result, seen = [], {parent.rstrip('/')}
    for hit in links[:80] if isinstance(links, list) else []:
        if not isinstance(hit, dict) or not isinstance(hit.get('url'), str) or not isinstance(hit.get('title'), str):
            continue
        title = ' '.join(hit['title'].split())[:500]
        href = hit['url']
        if len(title) < 8 or len(href) > 2048 or '\\' in href or any(ord(c) < 32 or ord(c) == 127 for c in href):
            continue
        try:
            url = urljoin(parent, href)
            GuardedHTTPS._target(url)
            parsed = urlsplit(url)
        except (FeedError, TypeError, ValueError):
            continue
        if (parsed.hostname != origin.hostname or parsed.query
                or re.search(r'\.(?:pdf|pptx?|docx?|xlsx?|zip|png|jpe?g|svg|mp4|exe)$', parsed.path, re.I)
                or re.search(r'/(?:login|signin|sign-in|account|logout)(?:/|$)', parsed.path, re.I)
                or url.rstrip('/') in seen):
            continue
        seen.add(url.rstrip('/'))
        result.append({'url': url, 'title': title})
        if len(result) == 20:
            break
    return result


def extract_article(body: bytes, content_type: str, *, source_url=None):
    if len(body) > MAX_FEED_BYTES:
        return _empty(error_code='oversized_body')
    if content_type.split(';')[0].strip().lower() not in {'text/html', 'application/xhtml+xml'}:
        return _empty(error_code='unsupported_media_type')
    # Respect declared encodings; never silently decode corrupt input as evidence.
    declaration = re.search(r'charset\s*=\s*["\']?([\w-]+)', content_type, re.I)
    if declaration is None:
        declaration = re.search(r'charset\s*=\s*["\']?([\w-]+)', body[:4096].decode('ascii', 'ignore'), re.I)
    encoding = declaration.group(1).lower() if declaration else 'utf-8-sig'
    if encoding not in {'utf-8', 'utf-8-sig', 'utf8', 'gb2312', 'gbk', 'gb18030', 'big5', 'iso-8859-1', 'windows-1252'}:
        return _empty(error_code='unsupported_encoding')
    try:
        source = body.decode(encoding, errors='strict')
        parser = _Blocks()
        parser.feed(source)
        parser.close()
        parser.flush()
    except (UnicodeError, ValueError, RecursionError):
        return _empty(error_code='unreadable_html')
    blocks = parser.blocks
    # Barrier pages cannot become a misleading "full text" claim. This may
    # conservatively reject a report discussing paywalls; the feed remains usable.
    if _BARRIERS.search(' '.join(parser.visible)):
        return _empty(error_code='access_restricted')
    links = safe_article_links(source_url, parser.links) if source_url else []
    if any(semantic == 2 for semantic, _ in blocks):
        blocks = [(semantic, text) for semantic, text in blocks if semantic == 2]
    else:
        # Generic pages often place menus/recommendations inside <main>. Only
        # substantial prose is eligible as partial body, never link-label lists.
        blocks = [(0, text) for _, text in blocks if len(text) >= 60]
    if sum(len(text) for _, text in blocks) < 100:
        structured = _structured_body(parser.structured)
        if structured and not _BARRIERS.search(structured):
            blocks = [(2, p.strip()) for p in structured.splitlines() if p.strip()]
    if sum(len(text) for _, text in blocks) < 100:
        return dict(_empty(error_code='insufficient_article_text'), links=links)
    paragraphs, output = [], ''
    partial = not any(semantic == 2 for semantic, _ in blocks)
    for _, value in blocks:
        start = len(output) + (2 if output else 0)
        room = MAX_TEXT_CHARS - start
        if room <= 0:
            partial = True
            break
        text = value[:room]
        output += ('\n\n' if output else '') + text
        paragraphs.append(dict(id=f'p{len(paragraphs)+1}', text=text, start=start, end=len(output)))
        if len(text) != len(value):
            partial = True
            break
    return dict(status='partial_body' if partial else 'article_body', text=output,
                paragraphs=paragraphs, extraction_version=EXTRACTION_VERSION, error_code=None, links=links)


def resolve_article(host, *, resolver=None, public_dns=None):
    """Recover only proxy fake-IP DNS, then pin and validate the public answer.

    DNS receives a public hostname only, never article text/private material.
    Private or mixed answers never trigger a fallback.
    """
    from project_watch.model_transport import _public_dns
    try:
        addresses = (resolver or _resolve_public)(host)
        if addresses and all(ipaddress.ip_address(a) in ipaddress.ip_network('198.18.0.0/15') for a in addresses):
            addresses = (public_dns or _public_dns)(host)
        if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
            raise FeedError('disallowed_address')
        return addresses
    except FeedError:
        raise
    except Exception as exc:
        raise FeedError('dns_failure') from exc


READER_FAILURE_CODES = frozenset({
    'disallowed_url', 'dns_failure', 'disallowed_address', 'connection_failed',
    'oversized_feed', 'invalid_redirect', 'rate_limited', 'http_failure',
    'too_many_redirects', 'tls_verification_failed', 'host_network_unavailable',
    'fetch_failed', 'oversized_body', 'unsupported_media_type', 'unsupported_encoding',
    'unreadable_html', 'access_restricted', 'insufficient_article_text',
})


class ArticleReader:
    def __init__(self, transport=None):
        from project_watch.host_network import configured_article_transport
        self.transport = transport or configured_article_transport()

    def read(self, url):
        try:
            response = self.transport.get_response(url)
        except FeedError as exc:
            # Only fixed transport codes, never raw exceptions/HTML or credentials.
            result = _empty('unavailable', str(exc) if str(exc) in READER_FAILURE_CODES else 'fetch_failed')
            return dict(result, source_url=url, resolved_url=None, body_sha256=None)
        result = extract_article(response.body, response.headers.get('content-type', ''), source_url=response.url)
        return dict(result, source_url=url, resolved_url=response.url,
                    body_sha256=hashlib.sha256(response.body).hexdigest())
