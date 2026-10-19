"""Add a web page or online PDF to a project's materials by URL.

The page is read once through the same guarded transport as news articles (public IPs only,
size-limited, no scripts, no cookies, paywall/login pages rejected). Readable text is saved as a
plain-text document with its source URL on top, then goes through normal document intake, so
it is analysed and cited like any uploaded file. Nothing behind a login is fetched.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from urllib.parse import urlsplit

MAX_TITLE = 60
REASONS = {
    "access_restricted": "这个网页需要登录或付费才能看到全文",
    "insufficient_article_text": "这个网页上没有读到足够的正文（可能依赖脚本加载）",
    "unsupported_media_type": "这个链接不是网页或 PDF",
    "unsupported_encoding": "网页编码无法识别",
    "oversized_body": "网页太大",
    "oversized_feed": "文件太大（在线读取上限 2MB）",
    "disallowed_url": "只支持 http/https 公网地址",
    "disallowed_address": "只支持公网地址，不能读取内网或本机地址",
    "http_failure": "网站返回了错误",
    "dns_failure": "找不到这个网站",
    "connection_failed": "连接网站失败",
    "rate_limited": "网站暂时拒绝访问（请求太频繁）",
}


class LinkError(ValueError):
    pass


def normalise(url: str) -> str:
    url = (url or "").strip()
    if re.match(r"^[a-z][a-z0-9+.\-]*:", url, re.I) and not re.match(r"^https?://", url, re.I):
        raise LinkError("网址格式不正确，只支持 http/https 链接")
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise LinkError("网址格式不正确，只支持 http/https 链接")
    return url


def _filename(title: str, url: str, suffix: str) -> str:
    base = re.sub(r"[\\\\/:*?\"<>|\r\n\t]+", " ", title or "").strip() or (urlsplit(url).hostname or "网页")
    return f"{base[:MAX_TITLE].strip()}{suffix}"


def _title(body: bytes) -> str:
    match = re.search(rb"<title[^>]*>(.*?)</title>", body[:200_000], re.I | re.S)
    if not match:
        return ""
    for encoding in ("utf-8", "gb18030"):
        try:
            return " ".join(match.group(1).decode(encoding).split())
        except UnicodeDecodeError:
            continue
    return ""


def fetch(reader, url: str) -> tuple[str, bytes]:
    """Return (display filename, file bytes) for a readable public page or PDF; raise LinkError otherwise."""
    from project_watch.feeds import FeedError
    from project_watch.public_article import extract_article

    url = normalise(url)
    transport = getattr(reader, "transport", None)
    if transport is None:
        raise LinkError("在线读取不可用")
    try:
        response = transport.get_response(url)
    except FeedError as exc:
        raise LinkError(REASONS.get(str(exc), "无法读取这个链接")) from exc
    media = (response.headers.get("content-type", "") or "").split(";")[0].strip().lower()
    final = getattr(response, "url", None) or url
    if media == "application/pdf" or (not media and response.body[:5] == b"%PDF-"):
        name = urlsplit(final).path.rsplit("/", 1)[-1] or "在线文件"
        return _filename(re.sub(r"\.pdf$", "", name, flags=re.I), final, ".pdf"), response.body
    article = extract_article(response.body, response.headers.get("content-type", ""))
    if article.get("status") not in ("article_body", "partial_body") or not article.get("text"):
        raise LinkError(REASONS.get(article.get("error_code") or "", "这个网页读不到正文"))
    title = _title(response.body)
    header = [f"标题：{title or '（无标题）'}", f"来源网址：{final}",
              f"读取时间：{datetime.now(UTC).strftime('%Y-%m-%d %H:%M')} UTC",
              "说明：由 ProjectWatch 从公开网页读取的正文，" + ("可能不完整。" if article["status"] == "partial_body" else "已去除导航与广告。"), ""]
    return _filename(title, final, ".txt"), ("\n".join(header) + article["text"]).encode("utf-8")
