"""Opt-in, fixed-target public-news starter. No request is sent during preview."""

from __future__ import annotations

import unicodedata
from urllib.parse import parse_qs, urlencode, urlsplit

PROVIDER = "gdelt"
CADENCE_MINUTES = 1440
INITIAL_RUN_KEY = "public-search:gdelt:first-run:v1"
DISCLOSURE = (
    "开启后会将项目名称发送给 GDELT 公开新闻检索服务，并每日检查。"
    "同名新闻可能误报，简称和别名可能漏报；结果仅为候选线索，不是已核验项目事实。"
)


def gdelt_rss_target(name: str) -> str:
    """Create a constrained GDELT DOC RSS query for one exact project name."""
    normalized = normalized_public_name(name)
    parameters = urlencode(
        {
            "query": f'"{normalized}"',
            "mode": "artlist",
            "format": "rss",
            "maxrecords": "50",
            "timespan": "1week",
            "sort": "datedesc",
        }
    )
    return f"https://api.gdeltproject.org/api/v2/doc/doc?{parameters}"


def normalized_public_name(name: str) -> str:
    """A single, conservative query tokenization shared by public providers."""
    normalized = unicodedata.normalize("NFKC", name).strip()
    if (
        len(normalized) > 120
        or sum(char.isalnum() for char in normalized) < 2
        or any(char in normalized for char in ('"', "\\", "\n", "\r", "\t"))
    ):
        raise ValueError("project name is unsuitable for public search")
    return normalized


def is_gdelt_rss_target(target: str) -> bool:
    """Only the canonical URL emitted above may take the opt-in proxy route."""
    if not isinstance(target, str):
        return False
    try:
        parsed = urlsplit(target)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "api.gdeltproject.org"
            or parsed.path != "/api/v2/doc/doc"
            or parsed.fragment
        ):
            return False
        params = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
        if set(params) != {"query", "mode", "format", "maxrecords", "timespan", "sort"}:
            return False
        query = params["query"]
        if (
            len(query) != 1
            or len(query[0]) < 4
            or not query[0].startswith('"')
            or not query[0].endswith('"')
        ):
            return False
        return gdelt_rss_target(query[0][1:-1]) == target
    except ValueError:
        return False


def parse_loopback_proxy(url: str) -> tuple[str, int]:
    """Reject implicit, credentialed and remote proxies for the fixed-source path."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid GDELT proxy URL") from exc
    if (
        not isinstance(url, str)
        or url != url.strip()
        or parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or port is None
        or port == 0
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("invalid GDELT proxy URL")
    return parsed.hostname, port
