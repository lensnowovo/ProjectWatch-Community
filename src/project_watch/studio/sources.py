"""External collectors: news, web search, papers, clinical trials, patents.

Free sources work without any key (Google News RSS, Bing News RSS, OpenAlex,
ClinicalTrials.gov). A search API key (Bocha / Brave / Tavily) adds web pages,
patents and better Chinese coverage. Each collector returns plain dicts so the
pipeline can triage them uniformly.
"""

from __future__ import annotations

import copy
import hashlib
import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from project_watch.studio.http import Http, HttpError
from project_watch.studio.settings import RegistryConfig, SearchConfig

_log = logging.getLogger(__name__)

CATEGORIES = ("company", "industry", "competitor", "policy", "paper", "trial", "patent")
PAPER_ABSTRACT_CHARS = 8000
PAPER_ABSTRACT_WORDS = 4096


def _openalex_abstract(index) -> dict:
    """Restore the documented inverted index, never invent missing word positions.

    Position/cardinality bounds avoid allocations based on an untrusted maximum
    offset. Missing and malformed abstracts remain metadata-only records.
    """
    empty = {"status": "invalid", "text": "", "truncated": False}
    if index is None or index == {}:
        return {**empty, "status": "missing"}
    if not isinstance(index, dict) or len(index) > PAPER_ABSTRACT_WORDS:
        return empty
    words = {}
    for token, positions in index.items():
        if not isinstance(token, str) or not token.strip() or len(token) > 1000 or not isinstance(positions, list) or not positions:
            return empty
        if len(positions) > PAPER_ABSTRACT_WORDS:
            return empty
        for pos in positions:
            if type(pos) is not int or not 0 <= pos < PAPER_ABSTRACT_WORDS or pos in words:
                return empty
            words[pos] = token
            if len(words) > PAPER_ABSTRACT_WORDS:
                return empty
    if not words or min(words) != 0 or max(words) != len(words) - 1:
        return empty
    text = " ".join(words[p] for p in range(len(words)))
    return {"status": "available", "text": text[:PAPER_ABSTRACT_CHARS],
            "truncated": len(text) > PAPER_ABSTRACT_CHARS}


def paper_affiliated(item: dict, names: list[str]) -> bool:
    """Company association needs returned affiliation text, not a search query.

    Names are caller-provided current public identities. Do not use model-guessed
    affiliation queries, title co-mentions or topic hits as company authorship.
    """
    extra = item.get("extra") or {}
    if isinstance(extra, str):
        import json
        extra = json.loads(extra)
    institutions = extra.get("institutions") or []
    affiliations = extra.get("affiliations") or []
    for name in names:
        if not isinstance(name, str) or len(name.strip()) < 2:
            continue
        normalized = " ".join(name.split()).casefold()
        if any(isinstance(record, str) and " ".join(record.split()).casefold() == normalized for record in institutions):
            return True
        # A delimiter separates an affiliation name from its address/department;
        # token-prefix or institution-name continuation is not an identity match.
        pattern = r"(?<![\w])" + re.escape(name.strip()) + r"\s*(?=$|[,，;；()（）])"
        if any(isinstance(record, str) and re.search(pattern, record, re.I) for record in affiliations):
            return True
    return False


def item_key(url: str, title: str) -> str:
    parts = urlsplit(url or "")
    normalized = urlunsplit((parts.scheme, parts.netloc.lower(), parts.path.rstrip("/"),
                             parts.query, "")) if parts.netloc else ""
    basis = normalized or " ".join((title or "").lower().split())
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def _iso(value) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=UTC)
        return dt.astimezone(UTC).isoformat()
    text = str(value).strip()
    if isinstance(value, (int, float)) or text.isdigit():
        number = float(value)
        if number > 1e11:  # epoch milliseconds (Tianyancha, cninfo)
            number /= 1000
        if 9e8 < number < 4e9:
            return datetime.fromtimestamp(number, UTC).isoformat()
        return None
    for parser in (datetime.fromisoformat, parsedate_to_datetime):
        try:
            dt = parser(text.replace("Z", "+00:00"))
            return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).astimezone(UTC).isoformat()
        except (ValueError, TypeError, OverflowError):
            pass
    return None


def _item(kind: str, title: str, url: str, *, source: str, snippet: str = "",
          published_at=None, extra: dict | None = None) -> dict:
    return {
        "kind": kind,
        "title": " ".join((title or "").split())[:300],
        "url": (url or "")[:2048],
        "source": (source or "")[:120],
        "snippet": " ".join((snippet or "").split())[:1200],
        "published_at": _iso(published_at),
        "extra": extra or {},
        "key": item_key(url, title),
    }


CN_REGULATORY_HOSTS = ("chinadrugtrials.org.cn", "cde.org.cn", "nmpa.gov.cn")
REGISTRY_CHANGE_DAYS = 730  # older registry changes are history, not news


def _plain(text) -> str:
    return " ".join(re.sub(r"</?em>|<[^>]{1,40}>", "", str(text or "")).split())


def _host_in(url: str, hosts: tuple[str, ...]) -> bool:
    host = (urlsplit(url or "").hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in hosts)


class SearchCache:
    """Bounded one-hour process cache. Concurrent identical queries are charged once.

    A key fingerprint, not the key, is part of the cache signature. Rotation,
    different providers, query types and result counts cannot reuse an old response.
    Failures are never cached; results are copied so caller edits cannot contaminate it.
    """

    def __init__(self, ttl: float = 3600, capacity: int = 512) -> None:
        self.ttl, self.capacity = ttl, capacity
        self.entries: OrderedDict = OrderedDict()
        self.guard = threading.Lock()
        self.locks = [threading.Lock() for _ in range(32)]

    def get(self, signature: tuple, fetch) -> tuple[list[dict], bool]:
        with self.locks[hash(signature) % len(self.locks)]:
            with self.guard:
                entry = self.entries.get(signature)
                if entry and time.monotonic() - entry[0] < self.ttl:
                    self.entries.move_to_end(signature)
                    return copy.deepcopy(entry[1]), True
            hits = fetch()
            with self.guard:
                self.entries[signature] = (time.monotonic(), copy.deepcopy(hits))
                self.entries.move_to_end(signature)
                while len(self.entries) > self.capacity:
                    self.entries.popitem(last=False)
            return hits, False


class Collectors:
    def __init__(self, http: Http, search: SearchConfig, registry: RegistryConfig | None = None,
                 *, search_cache: SearchCache | None = None) -> None:
        self.http = http
        self.search_config = search
        self.registry_config = registry or RegistryConfig()
        # Safe per-call outcomes: persisted in coverage, never provider response bodies/keys.
        self.search_checks: list[dict] = []
        self.search_cache = search_cache

    # ---------- China: listed-company announcements (free, official) ----------
    def cninfo(self, keyword: str, *, fulltext: bool = True, limit: int = 20) -> list[dict]:
        """巨潮资讯 announcement search. Full-text mode finds listed companies that mention the
        (often unlisted) company: investments, contracts, related-party deals, customer disclosures."""
        response = self.http.get(
            "https://www.cninfo.com.cn/new/fulltextSearch/full?" + urlencode({
                "searchkey": keyword, "sdate": "", "edate": "", "isfulltext": "true" if fulltext else "false",
                "sortName": "pubdate", "sortType": "desc", "pageNum": 1, "pageSize": limit}),
            headers={"Accept": "application/json", "Referer": "https://www.cninfo.com.cn/new/fulltextSearch"})
        if response.status != 200:
            raise HttpError("http_failure", status=response.status)
        data = response.json()
        out = []
        for a in (data.get("announcements") or [])[:limit]:
            if not isinstance(a, dict) or not a.get("adjunctUrl"):
                continue
            sec = _plain(a.get("secName"))
            title = _plain(a.get("announcementTitle"))
            when = a.get("announcementTime")
            excerpt = _plain(a.get("announcementContent"))
            out.append(_item("filing", f"{sec}：{title}" if sec and sec not in title else title,
                             "https://static.cninfo.com.cn/" + str(a["adjunctUrl"]).lstrip("/"),
                             source=f"巨潮资讯 · {sec}" if sec else "巨潮资讯",
                             snippet=(f"{a.get('secCode') or ''} {sec} 公告：{title}"
                                      + (f" · 全文检索摘录：{excerpt}" if excerpt else "")).strip(),
                             published_at=when,
                             extra={"sec_code": a.get("secCode") or "", "sec_name": sec,
                                    "matched": "fulltext" if fulltext else "title"}))
        return out

    # ---------- China: drug trial registration / review (via the configured search service) ----------
    def cn_regulatory(self, name: str) -> list[dict]:
        """药物临床试验登记与信息公示平台 / CDE / NMPA pages that mention the sponsor. The registry
        itself is script-protected, so it is reached only through the user's search service."""
        if not self.has_search:
            return []
        out, seen = [], set()
        for site in ("chinadrugtrials.org.cn", "cde.org.cn", "nmpa.gov.cn"):
            for r in self.web_search(f"site:{site} {name}", count=8):
                if _host_in(r["url"], CN_REGULATORY_HOSTS) and r["key"] not in seen:
                    seen.add(r["key"])
                    out.append(dict(r, kind="trial", source=r["source"] or site))
        return out

    # ---------- China: business registry changes (Tianyancha key) ----------
    @property
    def has_registry(self) -> bool:
        return self.registry_config.provider == "tianyancha" and bool(self.registry_config.api_key)

    def registry_changes(self, legal_name: str) -> list[dict]:
        """工商变更记录：股东、注册资本、法定代表人、经营范围、名称等。"""
        if not self.has_registry or not legal_name:
            return []
        data = self.http.get_json(
            "https://open.api.tianyancha.com/services/open/ic/changeinfo/2.0?" +
            urlencode({"keyword": legal_name, "pageSize": 20, "pageNum": 1}),
            headers={"Authorization": self.registry_config.api_key})
        if data.get("error_code") not in (0, None):
            raise HttpError("registry_error", detail=str(data.get("reason", ""))[:200])
        cutoff = datetime.now(UTC).timestamp() - REGISTRY_CHANGE_DAYS * 86400
        out = []
        for c in ((data.get("result") or {}).get("items") or []):
            if not isinstance(c, dict):
                continue
            when = _iso(c.get("changeTime"))
            if when and datetime.fromisoformat(when).timestamp() < cutoff:
                continue
            what = _plain(c.get("changeItem")) or "工商信息"
            before, after = _plain(c.get("contentBefore"))[:400], _plain(c.get("contentAfter"))[:400]
            item = _item("registry", f"{legal_name} 工商变更：{what}",
                         "https://www.tianyancha.com/search?" + urlencode({"key": legal_name}),
                         source="工商登记（天眼查）", snippet=f"变更前：{before or '—'}；变更后：{after or '—'}",
                         published_at=when, extra={"change_item": what})
            item["key"] = hashlib.sha256(f"registry|{legal_name}|{what}|{when}|{after}".encode()).hexdigest()[:32]
            out.append(item)
        return out

    # ---------- news (free) ----------
    def google_news(self, query: str, *, lang: str = "zh") -> list[dict]:
        from project_watch.feeds import parse_feed

        params = ({"q": query, "hl": "zh-CN", "gl": "CN", "ceid": "CN:zh-Hans"} if lang == "zh"
                  else {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"})
        response = self.http.get("https://news.google.com/rss/search?" + urlencode(params))
        if response.status != 200:
            raise HttpError("http_failure", status=response.status)
        entries = parse_feed(response.body)
        # Parse source metadata only after the bounded/DTD-safe feed validation.
        outlets = {node.findtext("link", ""): " ".join(node.findtext("source", "").split())
                   for node in ET.fromstring(response.body).findall("./channel/item")}
        items = []
        for entry in entries:
            outlet = outlets.get(entry.url, "")
            title = entry.title
            if outlet:
                suffix = f" - {outlet}"
                if title.endswith(suffix):
                    title = title[:-len(suffix)]
            else:
                prefix, separator, inferred = title.rpartition(" - ")
                if separator and prefix and inferred:
                    title, outlet = prefix, inferred
            items.append(_item("news", title or entry.title, entry.url, source=outlet or "Google News",
                               snippet="", published_at=entry.published_at))
        return items

    def news(self, query: str, *, search_count: int = 0, limit: int = 30) -> list[dict]:
        """Readable-first news: Bing News RSS (direct publisher links), plus the
        configured search service when `search_count` > 0. Google News only when the
        user enabled it, since its links do not resolve to readable originals.
        A source failing is tolerated while another one answers.
        """
        items, errors = [], []
        sources = [lambda: self.bing_news(query)]
        if search_count and self.has_search:
            sources.append(lambda: self.web_search(query, news=True, count=search_count))
        if getattr(self.search_config, 'google_news', False):
            sources.append(lambda: self.google_news(query)[:15])
        for source in sources:
            try:
                items.extend(source())
            except (HttpError, ValueError) as exc:
                # A held provider already refused definitively (quota/key); another
                # source may still answer. Unknown outcomes and budgets still stop.
                if isinstance(exc, HttpError) and exc.code in ('request_outcome_unknown', 'request_budget'):
                    raise
                errors.append(exc)
        if errors and not items:
            raise errors[0]
        seen, out = set(), []
        for item in items:
            if item['url'] not in seen:
                seen.add(item['url'])
                out.append(item)
        return out[:limit]

    def bing_news(self, query: str) -> list[dict]:
        from project_watch.bing_news import parse_bing_feed

        response = self.http.get("https://www.bing.com/news/search?" +
                                 urlencode({"q": query, "format": "rss", "sortbydate": "1"}))
        if response.status != 200:
            raise HttpError("http_failure", status=response.status)
        return [_item("news", e.title, e.url, source=urlsplit(e.url).netloc, snippet=e.summary,
                      published_at=e.published_at) for e in parse_bing_feed(response.body)]

    # ---------- web search (keyed) ----------
    @property
    def has_search(self) -> bool:
        return bool(self.search_config.ready_providers())

    def original_search(self, lead: dict, *, count: int = 5) -> list[dict]:
        from project_watch.studio.article_lookup import matches_original, title_query

        query = title_query(lead.get('title'))
        if not query:
            return []
        return self.web_search(query, count=count,
                               accept=lambda hit: matches_original(lead, hit, different_publisher=True))

    def web_search(self, query: str, *, news: bool = False, count: int = 10, accept=None) -> list[dict]:
        results, seen, errors = [], {}, []
        successes = 0
        keys = self.search_config.credentials()
        providers = self.search_config.enabled()
        if self.search_config.mode == "auto":
            preferred = "bocha" if re.search(r"[\u3400-\u9fff]", query) else "tavily"
            providers = sorted(providers, key=lambda p: p != preferred)
        for provider in providers:
            key = keys.get(provider, "")
            try:
                if not key:
                    raise HttpError("search_key_missing")
                def fn(service=provider, credential=key):
                    return self._provider_search(service, credential, query, news=news, count=count)
                if self.search_cache is None:
                    hits, cached = fn(), False
                else:
                    signature = (provider, hashlib.sha256(key.encode()).hexdigest(), query.strip(), news, count)
                    hits, cached = self.search_cache.get(signature, fn)
                successes += 1
                acceptable = sum(bool(accept(hit)) for hit in hits) if accept else len(hits)
                check = {"provider": provider, "ok": True, "count": len(hits), "cached": cached}
                if accept:
                    check['acceptable_count'] = acceptable
                self.search_checks.append(check)
                for hit in hits:
                    if hit["key"] in seen:
                        provenance = seen[hit["key"]]["extra"]["search_providers"]
                        if provider not in provenance:
                            provenance.append(provider)
                    else:
                        hit["extra"]["search_providers"] = [provider]
                        seen[hit["key"]] = hit
                        results.append(hit)
                # Original lookup requires a usable donor, not just nonempty leads.
                # Ordinary searches retain their existing nonempty-success routing.
                if acceptable and self.search_config.mode == "auto":
                    break
            except (HttpError, ValueError, KeyError, TypeError) as exc:
                if isinstance(exc, HttpError) and exc.code in (
                    "request_outcome_unknown", "request_budget", "provider_backoff"
                ):
                    raise  # Do not route an uncertain/held paid request to a second provider.
                error = HttpError(getattr(exc, "code", "invalid_search_response"),
                                  status=getattr(exc, "status", None))
                errors.append(error)
                self.search_checks.append({"provider": provider, "ok": False,
                                           "code": error.code, "status": error.status})
        if errors and not successes:
            raise errors[0]
        return results

    def _provider_search(self, provider: str, key: str, query: str, *,
                         news: bool, count: int) -> list[dict]:
        if provider == "bocha":
            data = self._ok(self.http.post_json(
                "https://api.bochaai.com/v1/web-search",
                {"query": query, "freshness": "oneYear" if news else "noLimit",
                 "summary": True, "count": count},
                headers={"Authorization": f"Bearer {key}"}, timeout=30))
            pages = (((data.get("data") or {}).get("webPages") or {}).get("value")) or []
            return [_item("web", p.get("name", ""), p.get("url", ""),
                          source=p.get("siteName") or urlsplit(p.get("url", "")).netloc,
                          snippet=p.get("summary") or p.get("snippet", ""),
                          published_at=p.get("datePublished")) for p in pages]
        if provider == "brave":
            path = "news/search" if news else "web/search"
            data = self._ok(self.http.get(
                f"https://api.search.brave.com/res/v1/{path}?" +
                urlencode({"q": query, "count": count}),
                headers={"X-Subscription-Token": key, "Accept": "application/json"}))
            results = data.get("results") if news else (data.get("web") or {}).get("results")
            return [_item("web", r.get("title", ""), r.get("url", ""),
                          source=(r.get("meta_url") or {}).get("hostname") or urlsplit(r.get("url", "")).netloc,
                          snippet=r.get("description", ""), published_at=r.get("page_age"))
                    for r in results or []]
        if provider == "tavily":
            data = self._ok(self.http.post_json(
                "https://api.tavily.com/search",
                {"query": query, "max_results": count, "topic": "news" if news else "general",
                 "include_published_date": True},
                headers={"Authorization": f"Bearer {key}"}, timeout=30))
            return [_item("web", r.get("title", ""), r.get("url", ""),
                          source=urlsplit(r.get("url", "")).netloc, snippet=r.get("content", ""),
                          published_at=r.get("published_date")) for r in data.get("results") or []]
        return []

    @staticmethod
    def _ok(response):
        if response.status == 401:
            raise HttpError("search_key_invalid", status=response.status)
        if response.status == 403:
            try:
                data = response.json()
            except HttpError:
                data = {}
            if data.get("message") == "You do not have enough money or package quota":
                raise HttpError("search_quota_exhausted", status=response.status)
            raise HttpError("search_access_denied", status=response.status)
        if response.status != 200:
            raise HttpError("http_failure", status=response.status)
        return response.json()

    # ---------- papers (free) ----------
    def openalex(self, query: str, *, affiliation: bool = False, author: bool = False, per_page: int = 15) -> list[dict]:
        params = {"per-page": per_page, "sort": "publication_date:desc",
                  "select": "id,doi,display_name,publication_date,primary_location,authorships,cited_by_count,abstract_inverted_index"}
        if affiliation:
            params["filter"] = f"raw_affiliation_strings.search:{query}"
        elif author:
            params["filter"] = f"raw_author_name.search:{query}"
        else:
            params["search"] = query
        data = self.http.get_json("https://api.openalex.org/works?" + urlencode(params))
        items = []
        for work in data.get("results") or []:
            if not isinstance(work, dict):
                continue
            location = work.get("primary_location") or {}
            venue = (location.get("source") or {}).get("display_name") or "OpenAlex"
            authorships = [a for a in (work.get("authorships") or [])[:100] if isinstance(a, dict)]
            authors = [str((a.get("author") or {}).get("display_name") or "")[:200]
                       for a in authorships[:6]]
            institutions = sorted({inst.get("display_name", "")
                                   for a in authorships
                                   for inst in (a.get("institutions") or [])[:30]
                                   if isinstance(inst, dict) and isinstance(inst.get("display_name"), str)} - {""})[:20]
            affiliations = list(dict.fromkeys(s[:500] for a in authorships
                                             for s in (a.get("raw_affiliation_strings") or [])[:4]
                                             if isinstance(s, str) and s.strip()))[:20]
            abstract = _openalex_abstract(work.get("abstract_inverted_index"))
            url = work.get("doi") or location.get("landing_page_url") or work.get("id", "")
            items.append(_item("paper", work.get("display_name", ""), url, source=venue,
                               snippet=abstract["text"] or "作者：" + "、".join(a for a in authors if a),
                               published_at=work.get("publication_date"),
                               extra={"institutions": institutions,
                                      "affiliations": affiliations, "authors": authors,
                                      "openalex_id": str(work.get("id") or "")[:200],
                                      "paper_search": {"query": query[:120], "mode": "affiliation" if affiliation else "author" if author else "topic"},
                                      "paper_abstract": abstract,
                                      "cited_by": work.get("cited_by_count", 0)}))
        return items

    # ---------- clinical trials (free) ----------
    def clinical_trials(self, *, sponsor: str | None = None, term: str | None = None,
                        page_size: int = 20) -> list[dict]:
        params = {"pageSize": page_size, "sort": "LastUpdatePostDate:desc", "format": "json"}
        if sponsor:
            params["query.spons"] = sponsor
        if term:
            params["query.term"] = term
        data = self.http.get_json("https://clinicaltrials.gov/api/v2/studies?" + urlencode(params))
        items = []
        for study in data.get("studies") or []:
            proto = study.get("protocolSection") or {}
            ident = proto.get("identificationModule") or {}
            status = proto.get("statusModule") or {}
            design = proto.get("designModule") or {}
            sponsor_name = ((proto.get("sponsorCollaboratorsModule") or {}).get("leadSponsor") or {}).get("name", "")
            collaborators = [c.get("name", "") for c in
                             (proto.get("sponsorCollaboratorsModule") or {}).get("collaborators") or []
                             if c.get("name")][:6]
            conditions = (proto.get("conditionsModule") or {}).get("conditions") or []
            nct = ident.get("nctId", "")
            phases = "/".join(design.get("phases") or []) or "N/A"
            items.append(_item(
                "trial", ident.get("briefTitle", ""), f"https://clinicaltrials.gov/study/{quote(nct)}",
                source="ClinicalTrials.gov",
                snippet=(f"{nct} · {phases} · {status.get('overallStatus', '')} · 适应症：{'、'.join(conditions[:4])} · 申办方：{sponsor_name}"
                         + (f" · 合作方：{'、'.join(collaborators)}" if collaborators else "")),
                published_at=(status.get("lastUpdatePostDateStruct") or {}).get("date"),
                extra={"nct_id": nct, "phase": phases, "status": status.get("overallStatus"),
                       "sponsor": sponsor_name, "collaborators": collaborators,
                       "conditions": conditions[:6]}))
        return items

    def registry_studies(self, intervention: str = "", *, location: str = "", page_size: int = 50,
                         industry_only: bool = False, sponsor: str = "") -> list[dict]:
        """Raw structured ClinicalTrials.gov v2 study records (free, no key, read-only).

        Returned as provided for local deterministic rendering; callers decide
        sponsor class, arm and intervention eligibility. Not a relevance claim.
        """
        params = {"pageSize": max(1, min(page_size, 100)), "format": "json"}
        if intervention:
            params["query.intr"] = intervention
        if sponsor:
            params["query.spons"] = sponsor
        if location:
            params["query.locn"] = location
        if industry_only:
            # Server-side narrowing only; callers still check sponsor class locally.
            params["filter.advanced"] = "AREA[LeadSponsorClass]INDUSTRY"
        response = self.http.get("https://clinicaltrials.gov/api/v2/studies?" + urlencode(params),
                                 headers={"Accept": "application/json"})
        if response.status != 200:
            raise HttpError("http_failure", status=response.status)
        data = response.json()
        studies = data.get("studies") if isinstance(data, dict) else None
        return [s for s in studies or [] if isinstance(s, dict)][:100]

    # ---------- patents (needs web search) ----------
    def patents(self, assignee: str) -> list[dict]:
        if not self.has_search:
            return []
        results = self.web_search(f"site:patents.google.com {assignee}", count=10)
        out = []
        for r in results:
            if (urlsplit(r['url']).hostname or '').lower() != 'patents.google.com':
                continue
            # A query assignee is not the patent applicant. Only preserve a labeled source excerpt.
            match = re.search(r'(?:申请人|专利权人|Current Assignee|Original Assignee)\s*[:：]?\s*([^。；;\n]{2,160})',
                              r.get('snippet') or '', re.I)
            extra = {**(r.get('extra') or {}), 'assignee_query': assignee}
            if match:
                extra.update(applicants=[match[1].strip()], applicant_quote=match[0], applicant_basis='search_excerpt')
            out.append(dict(r, kind='patent', extra=extra))
        return out
