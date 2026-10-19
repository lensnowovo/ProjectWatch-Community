"""Opt-in public-company network probe. Credentials come only from PW_* environment.

Run with the installed project, for example:
  python scripts/studio_live_probe.py --fixtures-dir tests/fixtures/studio_live --resolve
  python scripts/studio_live_probe.py --proxy-mode system
PW_PROXY overrides the system proxy in the default (env) mode. Never print raw
exceptions, request headers, model replies, or response headers. Missing keyed
services are reported as skipped; network/parser failures make the exit code 1.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from project_watch.studio.http import Http, HttpError, Response, system_proxies, urllib_transport
from project_watch.studio.llm import LLM, ModelError
from project_watch.studio.resolve import resolve_company
from project_watch.studio.settings import ModelConfig, RegistryConfig, SearchConfig
from project_watch.studio.sources import Collectors

COMPANIES = (
    ("catl", "宁德时代", "Contemporary Amperex Technology", "Contemporary Amperex Technology"),
    ("moderna", "Moderna", "Moderna", "ModernaTX"),
)
PUBLIC_ENDPOINTS = {
    "google_news": "https://news.google.com/rss/search",
    "bing_news": "https://www.bing.com/news/search",
    "openalex_keyword": "https://api.openalex.org/works",
    "openalex_affiliation": "https://api.openalex.org/works",
    "clinical_trials": "https://clinicaltrials.gov/api/v2/studies",
    "bocha": "https://api.bochaai.com/v1/web-search",
    "brave": "https://api.search.brave.com/res/v1/web/search",
    "tavily": "https://api.tavily.com/search",
}
_SENSITIVE = re.compile(r"key|token|cookie|authorization|password|secret", re.I)


class Redactor:
    def __init__(self, secrets: list[str]) -> None:
        self.secrets = sorted((s for s in secrets if s), key=len, reverse=True)

    def text(self, value: str) -> str:
        for secret in self.secrets:
            value = value.replace(secret, "[REDACTED]")
        return value

    def clean(self, value):
        if isinstance(value, dict):
            return {k: self.clean(v) for k, v in value.items() if not _SENSITIVE.search(k)}
        if isinstance(value, list):
            return [self.clean(v) for v in value]
        if isinstance(value, str):
            value = self.text(value)
            if value.startswith(("http://", "https://")):
                parts = urlsplit(value)
                # Remove credentials and credential-shaped query arguments, including
                # those nested inside Bing's article URL. Preserve public tracking
                # wrappers so the real Bing decoder can be replayed offline.
                host = parts.hostname or ""
                host = f"[{host}]" if ":" in host else host
                netloc = host + (f":{parts.port}" if parts.port else "")
                query = urlencode([(k, self.clean(v)) for k, v in parse_qsl(parts.query)
                                   if not _SENSITIVE.search(k)
                                   and (parts.hostname != "www.bing.com" or parts.path != "/news/apiclick.aspx" or k == "url")])
                value = urlunsplit((parts.scheme, netloc, parts.path, query, ""))
            return value
        return value


def _pick(row: dict, fields: tuple[str, ...]) -> dict:
    return {k: row[k] for k in fields if k in row}


def trim_response(operation: str, response: Response) -> tuple[str, object, int]:
    """Keep at most four public metadata records; no headers or article bodies."""
    if operation in ("google_news", "bing_news"):
        from project_watch.bing_news import parse_bing_feed
        from project_watch.feeds import parse_feed

        # Use the existing hardened XML parser before touching any XML.
        (parse_bing_feed if operation == "bing_news" else parse_feed)(response.body)
        root = ET.fromstring(response.body)
        original = root.findall("./channel/item")[:4]
        rss = ET.Element("rss", {"version": "2.0"})
        channel = ET.SubElement(rss, "channel")
        for entry in original:
            item = ET.SubElement(channel, "item")
            for tag in ("title", "link", "pubDate", "source"):
                node = entry.find(tag)
                if node is not None:
                    ET.SubElement(item, tag, dict(node.attrib)).text = node.text
        return "xml", ET.tostring(rss, encoding="unicode"), len(original)
    data = response.json()
    if operation.startswith("openalex_"):
        records = []
        for work in (data.get("results") or [])[:4]:
            row = _pick(work, ("id", "doi", "display_name", "publication_date", "cited_by_count"))
            loc = work.get("primary_location") or {}
            row["primary_location"] = {
                "landing_page_url": loc.get("landing_page_url"),
                "source": _pick(loc.get("source") or {}, ("display_name",)),
            }
            row["authorships"] = [{
                "author": _pick(a.get("author") or {}, ("display_name",)),
                "institutions": [_pick(i, ("display_name",)) for i in (a.get("institutions") or [])[:6]],
                "raw_affiliation_strings": (a.get("raw_affiliation_strings") or [])[:3],
            } for a in (work.get("authorships") or [])]
            records.append(row)
        return "json", {"results": records}, len(records)
    if operation == "clinical_trials":
        records = []
        for study in (data.get("studies") or [])[:4]:
            proto = study.get("protocolSection") or {}
            sponsors = proto.get("sponsorCollaboratorsModule") or {}
            row = {
                "identificationModule": _pick(proto.get("identificationModule") or {}, ("nctId", "briefTitle")),
                "statusModule": _pick(proto.get("statusModule") or {}, ("overallStatus", "lastUpdatePostDateStruct")),
                "designModule": _pick(proto.get("designModule") or {}, ("phases",)),
                "conditionsModule": _pick(proto.get("conditionsModule") or {}, ("conditions",)),
                "sponsorCollaboratorsModule": {
                    "leadSponsor": _pick(sponsors.get("leadSponsor") or {}, ("name", "class")),
                    "collaborators": [_pick(c, ("name", "class")) for c in (sponsors.get("collaborators") or [])[:6]],
                },
            }
            records.append({"protocolSection": row})
        return "json", {"studies": records}, len(records)
    if operation == "bocha":
        rows = ((data.get("data") or {}).get("webPages") or {}).get("value") or []
        rows = [_pick(r, ("name", "url", "siteName", "datePublished", "dateLastCrawled")) for r in rows[:4]]
        return "json", {"code": data.get("code"), "data": {"webPages": {"value": rows}}}, len(rows)
    if operation == "brave":
        rows = (data.get("web") or {}).get("results") or []
        rows = [_pick(r, ("title", "url", "page_age", "meta_url")) for r in rows[:4]]
        return "json", {"web": {"results": rows}}, len(rows)
    if operation == "tavily":
        rows = [_pick(r, ("title", "url", "published_date")) for r in (data.get("results") or [])[:4]]
        return "json", {"results": rows}, len(rows)
    raise ValueError("unsupported fixture source")


def save_fixture(directory: Path, slug: str, company: str, operation: str,
                 response: Response, redactor: Redactor) -> None:
    fmt, body, count = trim_response(operation, response)
    if count < 3:
        return  # No invented padding for empty or very short responses.
    if fmt == "xml":
        root = ET.fromstring(body)
        for node in root.iter():
            if node.text:
                node.text = redactor.clean(node.text)
            node.attrib = redactor.clean(node.attrib)
        body = ET.tostring(root, encoding="unicode")
    fixture = redactor.clean({
        "provenance": {"kind": "public_live", "captured_at": datetime.now(UTC).isoformat(),
                       "endpoint": PUBLIC_ENDPOINTS[operation], "company": company,
                       "operation": operation, "records": count},
        "response": {"format": fmt, "body": body},
    })
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{slug}-{operation}.json").write_text(
        json.dumps(fixture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def probe(args) -> int:
    env = os.environ
    model = ModelConfig(env.get("PW_MODEL_PROTOCOL", "openai"), env.get("PW_MODEL_BASE_URL", "").rstrip("/"),
                        env.get("PW_MODEL_ID", ""), env.get("PW_MODEL_KEY", ""))
    search = SearchConfig(env.get("PW_SEARCH_PROVIDER", "none"), env.get("PW_SEARCH_KEY", ""))
    proxy = env.get("PW_PROXY", "") if args.proxy_mode == "env" else ""
    # Reject credential-bearing model URLs; only the dedicated key variable may
    # authenticate requests. Avoid printing config values even on validation errors.
    if model.base_url:
        parsed = urlsplit(model.base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            print(json.dumps({"status": "failed", "error": "invalid_model_base_url"}))
            return 1
    if model.protocol not in ("openai", "anthropic") or search.provider not in ("none", "bocha", "brave", "tavily"):
        print(json.dumps({"status": "failed", "error": "invalid_provider_or_protocol"}))
        return 1
    if proxy:
        parsed = urlsplit(proxy)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            print(json.dumps({"status": "failed", "error": "invalid_proxy"}))
            return 1
    proxy_parts = urlsplit(proxy)
    redactor = Redactor([model.api_key, search.api_key, proxy_parts.username or "", proxy_parts.password or ""])
    transport = urllib_transport(proxy)
    last: list[Response] = []

    def send(method, url, headers, body, timeout):
        response = transport(method, url, headers, body, min(timeout, args.timeout))
        last[:] = [response]
        return response

    http = Http(send)
    collectors, llm = Collectors(http, search), LLM(model, http)
    # Include no proxy address/userinfo, only whether urllib discovered one.
    system = system_proxies()
    print(json.dumps({"proxy_mode": "explicit" if proxy else "system",
                      "system_proxy_detected": bool(system.get("https") or system.get("http")),
                      "model_ready": llm.ready, "search_ready": collectors.has_search}))
    failed = False

    def run(company, operation, call, *, skip="", slug="", fixture=True):
        nonlocal failed
        start = time.perf_counter()
        last.clear()
        row = {"company": company, "operation": operation}
        if skip:
            row.update(status="skipped", reason=skip, count=0, first=None)
        else:
            try:
                result = call()
                if isinstance(result, list):
                    row.update(count=len(result), first=redactor.clean(result[0]) if result else None)
                elif operation == "resolve_company":
                    candidates = result.get("candidates") or []
                    row.update(count=len(candidates), first=redactor.clean(candidates[0]) if candidates else None,
                               method=result.get("method"), candidate_names=[c.get("legal_name") for c in candidates])
                else:
                    # Record shape/length, never echo an arbitrary model completion.
                    row.update(count=1, first={"type": type(result).__name__,
                                              "fields": list(result) if isinstance(result, dict) else [],
                                              "characters": len(result) if isinstance(result, str) else None})
                row["status"] = "success"
                if args.fixtures_dir and fixture and last and last[0].status == 200:
                    save_fixture(args.fixtures_dir, slug, company, operation, last[0], redactor)
            except Exception as exc:
                failed = True
                # Response bodies and exception messages can echo credentials.
                row.update(status="failed", count=0, first=None,
                           error=exc.code if isinstance(exc, (HttpError, ModelError)) else type(exc).__name__,
                           http_status=exc.status if isinstance(exc, (HttpError, ModelError)) else None)
        row["elapsed_seconds"] = round(time.perf_counter() - start, 3)
        print(json.dumps(redactor.clean(row), ensure_ascii=False), flush=True)

    companies = [("custom", args.company, args.company, args.company)] if args.company else COMPANIES
    for slug, name, english, sponsor in companies:
        run(name, "google_news", lambda name=name, slug=slug: collectors.google_news(name, lang="zh" if slug == "catl" else "en"), slug=slug)
        run(name, "bing_news", lambda name=name: collectors.bing_news(name), slug=slug)
        run(name, "openalex_keyword", lambda english=english: collectors.openalex(english, per_page=5), slug=slug)
        run(name, "openalex_affiliation", lambda english=english: collectors.openalex(english, affiliation=True, per_page=5), slug=slug)
        run(name, "clinical_trials", lambda sponsor=sponsor: collectors.clinical_trials(sponsor=sponsor, page_size=5), slug=slug)
        run(name, search.provider if collectors.has_search else "web_search",
            lambda name=name: collectors.web_search(name, count=5), slug=slug,
            skip="missing_search_configuration" if not collectors.has_search else "")
        run(name, "llm.complete", lambda name=name: llm.complete("只回答 OK。", f"公开公司连接校准：{name}", max_tokens=1024),
            skip="missing_model_configuration" if not llm.ready else "", fixture=False)
        run(name, "llm.json", lambda name=name: llm.json('输出对象 {"company":"输入公司名","ok":true}。', name, max_tokens=1024),
            skip="missing_model_configuration" if not llm.ready else "", fixture=False)
    if args.resolve:
        name = companies[0][1]
        run(name, "resolve_company", lambda: resolve_company(
            name, llm=llm, collectors=collectors, http=http, registry=RegistryConfig()), fixture=False)
    return int(failed)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--company", help="Override the two default public companies")
    parser.add_argument("--proxy-mode", choices=("env", "system"), default="env")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--fixtures-dir", type=Path, help="Opt-in trimmed public metadata snapshots")
    parser.add_argument("--resolve", action="store_true", help="Resolve the first company end to end")
    args = parser.parse_args()
    if not 0 < args.timeout <= 180:
        parser.error("timeout must be in (0, 180]")
    logging.disable(logging.CRITICAL)
    try:
        return probe(args)
    except (ValueError, HttpError, ModelError):
        print(json.dumps({"status": "failed", "error": "configuration_or_transport_error"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
