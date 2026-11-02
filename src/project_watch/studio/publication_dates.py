"""Publication dates for search results, with where each date came from.

Search providers rarely return a field named "published". A date in the page's
URL or a labelled date at the top of the page ("发布时间：2026-10-16") is the
page's own statement; the provider's page date agrees with it in most cases and
is used last. The source is recorded so a reader can tell them apart.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

_DATE = r"(20[0-3]\d)\s*[-/.年]\s*(1[0-2]|0?[1-9])\s*[-/.月]\s*([12]\d|3[01]|0?[1-9])(?!\d)"
_URL = re.compile(r"(?<!\d)(20[0-3]\d)([-/_]?)(1[0-2]|0[1-9])\2([12]\d|3[01]|0[1-9])(?!\d)")
# A bare 时间/日期 must stand alone: "成立日期" or "截止时间" is not a publication date.
_LABELLED = re.compile(r"(?:发布时间|发布日期|发表时间|发表日期|发文日期|首次公示信息日期|公示日期|登记日期|(?:^|(?<=[\s|｜·【\[]))(?:时间|日期)"
                       r"|Published(?: on)?|Posted(?: on)?|Date)\s*[:：|｜]?\s*" + _DATE, re.I)
SOURCES = {"url": "网址日期", "page": "页面标注日期", "search": "搜索服务提供的页面日期"}


def _valid(year, month, day) -> str | None:
    try:
        value = datetime(int(year), int(month), int(day), tzinfo=UTC)
    except ValueError:
        return None
    return value.isoformat() if value <= datetime.now(UTC) + timedelta(days=1) else None


def _provider(value) -> str | None:
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    parsed = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat() if parsed <= datetime.now(UTC) + timedelta(days=1) else None


def derive(url: str, head: str, provider_date=None) -> tuple[str | None, str | None]:
    """(ISO date, source) from the URL, a labelled date near the top of the page, then the provider."""
    path = re.sub(r"^[a-z]+://[^/]+", "", url or "", flags=re.I)
    match = _URL.search(path)
    if match and (date := _valid(match[1], match[3], match[4])):
        return date, "url"
    match = _LABELLED.search((head or "")[:600])
    if match and (date := _valid(match[1], match[2], match[3])):
        return date, "page"
    if provider_date and (date := _provider(provider_date)):
        return date, "search"
    return None, None
