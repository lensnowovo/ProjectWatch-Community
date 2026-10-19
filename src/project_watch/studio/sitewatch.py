"""Official-website change monitoring: snapshot visible text blocks, diff them, explain the change.

Fetching goes through the same guarded transport as article reading (public IPs only, size
limits, no JavaScript, no cookies). robots.txt is honoured for every host, each page is read
at most once per CHECK_HOURS, and discovery only follows same-site links on the homepage
whose text clearly marks them as news / product / team / careers pages (at most MAX_PAGES).
"""

from __future__ import annotations

import difflib
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

MAX_PAGES = 6
CHECK_HOURS = 20
MAX_BLOCKS = 400
USER_AGENT = "ProjectWatch"

PAGE_KINDS = (
    ("news", "新闻动态", re.compile(r"新闻|动态|资讯|公告|媒体|news|press|media|blog|insight", re.I)),
    ("product", "产品与服务", re.compile(r"产品|方案|解决方案|服务|技术|product|solution|technology|platform", re.I)),
    ("team", "团队与管理层", re.compile(r"团队|管理层|领导|创始|关于|简介|about|team|leadership|company", re.I)),
    ("careers", "招聘", re.compile(r"招聘|加入|人才|职位|join|career|jobs|hiring", re.I)),
)
KIND_LABEL = {"home": "首页", **{k: label for k, label, _ in PAGE_KINDS}, "custom": "自定义页面"}

_SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "iframe", "form", "button", "select"}
_BLOCK_TAGS = {"p", "li", "h1", "h2", "h3", "h4", "h5", "td", "th", "dt", "dd", "blockquote", "figcaption", "a", "div", "section", "article"}
_VOLATILE = re.compile(r"^[\d\s:/.\-年月日号时分秒,，:：]+$|copyright|©|版权所有|备案|icp|cookie|浏览量|阅读量|点击", re.I)


class _Snapshot(HTMLParser):
    """Visible text blocks plus same-page links (text, href). Nothing is executed or followed."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._skip = 0
        self._stack: list[tuple[str, bool]] = []
        self._buffer: list[str] = []
        self._href: str | None = None
        self._link_text: list[str] = []

    def _flush(self) -> None:
        text = " ".join(" ".join(self._buffer).split())
        self._buffer = []
        if 2 <= len(text) <= 400:
            self.blocks.append(text)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        style = re.sub(r"\s+", "", attributes.get("style") or "").lower()
        hidden = tag in _SKIP_TAGS or "hidden" in attributes or "display:none" in style or "visibility:hidden" in style
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self._stack.append((tag, hidden))
            self._skip += int(hidden)
        if self._skip:
            return
        if tag in _BLOCK_TAGS or tag == "br":
            self._flush()
        if tag == "a":
            self._href = attributes.get("href")
            self._link_text = []

    def handle_endtag(self, tag):
        visible = not self._skip
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == tag:
                self._skip -= sum(int(hidden) for _tag, hidden in self._stack[index:])
                del self._stack[index:]
                break
        if visible and tag in _BLOCK_TAGS:
            self._flush()
        if visible and tag == "a" and self._href:
            self.links.append((" ".join(" ".join(self._link_text).split()), self._href))
            self._href = None

    def handle_data(self, data):
        if self._skip:
            return
        self._buffer.append(data)
        if self._href is not None:
            self._link_text.append(data)


def decode(body: bytes, content_type: str) -> str | None:
    if content_type.split(";")[0].strip().lower() not in {"text/html", "application/xhtml+xml", ""}:
        return None
    declared = re.search(r"charset\s*=\s*[\"']?([\w-]+)", content_type, re.I) or \
        re.search(rb"charset\s*=\s*[\"']?([\w-]+)", body[:4096], re.I)
    encoding = declared.group(1) if declared else "utf-8"
    encoding = encoding.decode() if isinstance(encoding, bytes) else encoding
    for candidate in (encoding, "utf-8", "gb18030"):
        try:
            return body.decode(candidate)
        except (LookupError, UnicodeDecodeError):
            continue
    return None


def snapshot(html: str) -> tuple[list[str], list[tuple[str, str]]]:
    parser = _Snapshot()
    try:
        parser.feed(html)
        parser.close()
        parser._flush()
    except (ValueError, RecursionError):
        return [], []
    seen, blocks = set(), []
    for b in parser.blocks:
        if b not in seen and not _VOLATILE.search(b):
            seen.add(b)
            blocks.append(b)
    return blocks[:MAX_BLOCKS], parser.links


def _meaningful(text: str) -> bool:
    return len(re.findall(r"[\u4e00-\u9fffA-Za-z]", text)) >= 4


def diff(old: list[str], new: list[str]) -> tuple[list[str], list[str]]:
    """Blocks added and removed (order kept). Re-ordering alone is not a change."""
    old_set, new_set = set(old), set(new)
    added = [b for b in new if b not in old_set and _meaningful(b)]
    removed = [b for b in old if b not in new_set and _meaningful(b)]
    # Tiny edits of the same sentence count once, as an addition.
    removed = [r for r in removed if not any(difflib.SequenceMatcher(None, r, a).ratio() > 0.85 for a in added[:50])]
    return added[:60], removed[:60]


def normalise_url(url: str) -> str | None:
    url = (url or "").strip()
    if not url:
        return None
    if re.match(r"^[a-z][a-z0-9+.\-]*:", url, re.I) and not re.match(r"^https?://", url, re.I):
        return None  # ftp:, javascript:, mailto: … never fetched
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    try:
        parts = urlsplit(url)
        _port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        return None
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, ""))


def _site(host: str) -> str:
    host = (host or "").lower().removeprefix("www.")
    return host  # Do not guess registrable domains (e.g. unrelated *.co.uk sites).


def discover(home_url: str, links: list[tuple[str, str]]) -> list[dict]:
    """Same-site links whose label clearly names a news/product/team/careers page (one per kind)."""
    home = urlsplit(home_url)
    found: dict[str, dict] = {}
    for text, href in links:
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        url = normalise_url(urljoin(home_url, href))
        if not url or _site(urlsplit(url).hostname or "") != _site(home.hostname or "") or url == normalise_url(home_url):
            continue
        label = f"{text} {urlsplit(url).path}"
        for kind, name, pattern in PAGE_KINDS:
            if kind not in found and pattern.search(label) and len(text) <= 20:
                found[kind] = {"url": url, "label": text or name, "kind": kind}
                break
    return list(found.values())[: MAX_PAGES - 1]


class Robots:
    """robots.txt per host, fetched once per run through the guarded transport."""

    def __init__(self, fetch, *, fatal_errors=()) -> None:
        self.fetch = fetch
        self.fatal_errors = fatal_errors
        self._cache: dict[str, RobotFileParser | None] = {}

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        key = f"{parts.scheme}://{parts.netloc}"
        if key not in self._cache:
            parser = RobotFileParser()
            try:
                body, _ctype, status = self.fetch(key + "/robots.txt")
                if status == 200:
                    parser.parse(body.decode("utf-8", "ignore").splitlines())
                elif status in (404, 410):
                    parser.parse([])  # missing robots.txt: everything allowed
                else:
                    self._cache[key] = None
                    return False
                self._cache[key] = parser
            except Exception as exc:
                if isinstance(exc, self.fatal_errors):
                    raise
                self._cache[key] = None  # unreachable: be conservative only for this run
        parser = self._cache[key]
        return parser is not None and parser.can_fetch(USER_AGENT, url)


def change_text(label: str, added: list[str], removed: list[str]) -> str:
    lines = [f"官网“{label}”页面发生变化。"]
    if added:
        lines.append("新增内容：\n" + "\n".join(f"+ {a}" for a in added[:20]))
    if removed:
        lines.append("删除内容：\n" + "\n".join(f"- {r}" for r in removed[:20]))
    return "\n".join(lines)


def change_title(label: str, added: list[str], removed: list[str]) -> str:
    head = next((a for a in added if len(a) >= 6), added[0] if added else "")
    if head:
        return f"官网{label}更新：{head[:60]}"
    return f"官网{label}删除了 {len(removed)} 处内容：{removed[0][:40]}" if removed else f"官网{label}更新"


# ---------------------------------------------------------------- finding the official site

# Media, directories, registries, recruiting, marketplaces and public bodies are never a company's own site.
_NOT_OFFICIAL = {
    "baidu.com", "zhihu.com", "qcc.com", "tianyancha.com", "aiqicha.com", "qixin.com", "qichacha.com", "sina.com.cn",
    "sina.cn", "weibo.com", "qq.com", "sohu.com", "163.com", "ifeng.com", "36kr.com", "linkedin.com", "weixin.qq.com",
    "cninfo.com.cn", "eastmoney.com", "stcn.com", "cls.cn", "toutiao.com", "douyin.com", "bilibili.com", "xueqiu.com",
    "pharmcube.com", "ebiotrade.com", "biomart.cn", "chemnet.com", "zaobao.com.sg", "yicai.com", "caixin.com",
    "thepaper.cn", "jiemian.com", "doi.org", "wikipedia.org", "crunchbase.com", "itjuzi.com", "pedaily.cn",
    "vbdata.cn", "cyzone.cn", "huxiu.com", "prnasia.com", "businesswire.com", "globenewswire.com", "zhipin.com",
    "liepin.com", "51job.com", "zhaopin.com", "lagou.com", "kanzhun.com", "maimai.cn", "jd.com", "tmall.com",
    "taobao.com", "github.com", "medsci.cn", "dxy.cn", "yaozh.com", "bioon.com", "cn-healthcare.com", "vcbeat.top",
    "clinicaltrials.gov", "chinadrugtrials.org.cn", "bjnews.com.cn", "people.com.cn", "xinhuanet.com", "chinadaily.com.cn",
    "cctv.com", "youtube.com", "facebook.com", "x.com", "twitter.com", "instagram.com", "bloomberg.com", "reuters.com",
    "qianzhan.com", "gelonghui.com", "futunn.com", "hexun.com", "cnstock.com", "21jingji.com", "nbd.com.cn",
    "chem17.com", "jinglingshuju.com", "sogou.com", "job1001.com", "baike.com", "so.com", "1688.com", "made-in-china.com", "11467.com", "qianlima.com", "bmlink.com",
    "mp.weixin.qq.com", "zhuanlan.zhihu.com", "csdn.net", "jianshu.com", "docin.com", "doc88.com", "wenku.baidu.com",
}
_PUBLIC_SUFFIX = (".gov.cn", ".gov", ".edu.cn", ".edu", ".ac.cn", ".org.cn")
_OFFICIAL_HINT = re.compile(r"官网|官方网站|首页|official\s*(?:site|website)|home\s*page", re.I)


def _not_official(host: str) -> bool:
    host = host.lower().removeprefix("www.")
    return host.endswith(_PUBLIC_SUFFIX) or any(host == d or host.endswith("." + d) for d in _NOT_OFFICIAL)


def site_names(*values) -> list[str]:
    """Distinctive names a company's own homepage would carry (short Chinese names need >= 3 characters)."""
    out = []
    for value in values:
        for name in ([value] if isinstance(value, str) else (value or [])):
            name = " ".join(str(name or "").split())
            latin = re.fullmatch(r"[\x00-\x7f]+", name) is not None
            if len(name) >= (4 if latin else 3) and name.casefold() not in (n.casefold() for n in out):
                out.append(name)
    return out


def official_candidates(hits: list[dict], names: list[str], limit: int = 3) -> list[str]:
    """Homepages of search hits that name the company and are not media / directory / public-body sites."""
    folded = [n.casefold() for n in names]
    scored: dict[str, int] = {}
    for hit in hits:
        url = normalise_url(str(hit.get("url") or ""))
        parts = urlsplit(url) if url else None
        if not parts or not parts.hostname or _not_official(parts.hostname):
            continue
        text = f"{hit.get('title') or ''} {hit.get('snippet') or ''}".casefold()
        if not any(n in text for n in folded):
            continue
        score = 2 + bool(_OFFICIAL_HINT.search(text)) + (parts.path in ("", "/"))
        home = urlunsplit((parts.scheme, parts.netloc, "/", "", ""))
        scored[home] = max(scored.get(home, 0), score)
    return sorted(scored, key=lambda h: -scored[h])[:limit]


def names_company(html: str, names: list[str]) -> str:
    """The first company name found anywhere in the homepage source (title, footer…), or ''."""
    folded = html.casefold()
    return next((n for n in names if n.casefold() in folded), "")
