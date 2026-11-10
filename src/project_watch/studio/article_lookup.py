"""Passive publisher discovery rules shared by news and competition reading."""

from urllib.parse import urlsplit

from project_watch.studio import events
from project_watch.studio.public_profile import safe_url

UNRESOLVABLE_LINK_HOSTS = ('news.google.com', 'static.cninfo.com.cn', 'tianyancha.com')


def title_query(title, *, max_chars=202):
    """A retrieval hint only; matches_original still checks the original title."""
    title = str(title or '').strip().replace('"', '')
    available = max_chars - 2  # Include the enclosing quotes in the provider limit.
    if len(title) > available:
        prefix = title[:available]
        # Avoid slicing an English word; Chinese text can end at a character.
        if prefix[-1:].isascii() and title[available:available + 1].isalnum():
            boundary = prefix.rfind(' ')
            if boundary >= 8:
                prefix = prefix[:boundary]
        title = prefix.rstrip()
    return '"' + title + '"' if len(title) >= 8 else None


def matches_original(lead, hit, *, different_publisher=False):
    url = hit.get('url') or ''
    if not safe_url(url):
        return False
    target = urlsplit(url)
    host = (target.hostname or '').lower()
    if target.scheme != 'https' or any(host == h or host.endswith('.' + h) for h in UNRESOLVABLE_LINK_HOSTS):
        return False
    if different_publisher:
        try:
            original = (urlsplit(lead.get('url') or '').hostname or '').lower()
        except ValueError:
            return False
        if host == original or host.endswith('.' + original) or original.endswith('.' + host):
            return False
    title, candidate = str(lead.get('title') or '').strip(), str(hit.get('title') or '')
    return (len(title) >= 8 and events.similarity(title, candidate) >= 0.8
            and not events.conflicting(title, candidate)
            and events.close_in_time(lead.get('published_at'), hit.get('published_at')))
