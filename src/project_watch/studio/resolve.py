"""Company identity resolution: name and/or BP -> candidate legal entities.

Default path: public search snippets + model reasoning (free, may be imprecise).
Optional path: a business-registry API (Tianyancha) when the user adds a key.
The investor always picks the final entity; nothing is auto-confirmed.
"""

from __future__ import annotations

import logging
from urllib.parse import urlencode

from project_watch.studio.http import Http, HttpError
from project_watch.studio.llm import LLM, ModelError
from project_watch.studio.public_profile import publication_day, recent_sources
from project_watch.studio.settings import RegistryConfig
from project_watch.studio.sources import Collectors

_log = logging.getLogger(__name__)

ENTITY_FIELDS = ("legal_name", "short_name", "credit_code", "website", "founded", "location",
                 "industry", "description", "stage")

_SYSTEM = """你是投资机构的项目研究助理，负责确认一家公司的准确工商主体。
根据用户给出的公司名称、可能的商业计划书摘录和公开搜索结果，列出最可能的候选主体（最多 4 个），
按可能性从高到低排序。不得编造统一社会信用代码、官网或成立日期：搜索结果里没有出现的信息留空字符串。
成立日期不等于来源发布日期。主营业务优先新近公司资料；旧招聘、登记范围与当前业务不同须保留时间口径，日期未知不得当作最新资料。
short_name 只填写这家公司的公司简称，不放品牌或产品名，无法确认公司简称时留空；品牌、产品等检索名称放 aliases。
输出 JSON：{"candidates":[{"legal_name":"工商全称","short_name":"公司简称","credit_code":"",
"website":"","founded":"YYYY 或 YYYY-MM-DD","location":"城市","industry":"一句话行业",
"description":"一句话主营业务","stage":"融资阶段，未知留空","aliases":["其他名称/英文名/产品名"],
"confidence":0-100,"reason":"为什么认为是/可能是这家","evidence":["支撑的搜索结果 URL"]}],
"notes":"如有同名、改名、集团与子公司等需要投资人留意的情况，在此说明"}"""


def _registry_candidates(http: Http, config: RegistryConfig, name: str) -> list[dict]:
    if config.provider != "tianyancha" or not config.api_key:
        return []
    data = http.get_json(
        "https://open.api.tianyancha.com/services/open/search/2.0?" +
        urlencode({"word": name, "pageSize": 8}),
        headers={"Authorization": config.api_key})
    if data.get("error_code") not in (0, None):
        raise HttpError("registry_error", detail=str(data.get("reason", ""))[:200])
    out = []
    for item in ((data.get("result") or {}).get("items") or []):
        out.append({
            "legal_name": str(item.get("name", "")).replace("<em>", "").replace("</em>", ""),
            "short_name": "", "credit_code": item.get("creditCode", "") or "",
            "website": "", "founded": str(item.get("estiblishTime", "") or "")[:10],
            "location": item.get("base", "") or item.get("regLocation", "") or "",
            "industry": item.get("categoryStr", "") or "", "description": "",
            "stage": "", "aliases": [], "confidence": 0,
            "reason": f"工商登记 · {item.get('regStatus', '')} · 法人 {item.get('legalPersonName', '')}",
            "evidence": [], "registry": True,
        })
    return out


def bp_company_name(llm: LLM, bp_text: str) -> str:
    data = llm.json(
        "从商业计划书开头内容中找出这家公司的名称。",
        "输出 JSON：{\"name\":\"公司名称（优先工商全称）\"}\n\n" + bp_text[:6000],
        max_tokens=300)
    return str(data.get("name", "")).strip()[:200]


def resolve_company(name: str, *, bp_text: str = "", llm: LLM, collectors: Collectors,
                    http: Http, registry: RegistryConfig) -> dict:
    name = " ".join((name or "").split())
    notes: list[str] = []
    if not name and bp_text and llm.ready:
        name = bp_company_name(llm, bp_text)
    if not name:
        raise ValueError("请输入公司名称，或上传一份能识别出公司名称的 BP")

    registry_hits: list[dict] = []
    try:
        registry_hits = _registry_candidates(http, registry, name)
    except (HttpError, ValueError) as exc:
        notes.append(f"工商数据服务查询失败：{exc}")

    evidence: list[dict] = []
    queries = [f"{name} 公司", f"{name} 融资"] if collectors.has_search else [name]
    for query in queries:
        try:
            hits = (collectors.web_search(query, count=8) if collectors.has_search
                    else collectors.bing_news(query)[:8])
        except (HttpError, ValueError) as exc:
            notes.append(f"搜索“{query}”失败：{exc}")
            continue
        evidence.extend(hits)
    if collectors.has_search and not evidence:
        try:
            evidence.extend(collectors.bing_news(name)[:8])
        except (HttpError, ValueError) as exc:
            notes.append(f"新闻检索失败：{exc}")
    if not collectors.has_search or not evidence:
        try:
            evidence.extend((collectors.google_news(name) if getattr(getattr(collectors, 'search_config', None), 'google_news', False)
                             else collectors.bing_news(name))[:8])
        except (HttpError, ValueError) as exc:
            notes.append(f"新闻检索失败：{exc}")

    if not llm.ready:
        candidates = registry_hits or [{
            "legal_name": name, "short_name": "", "credit_code": "", "website": "",
            "founded": "", "location": "", "industry": "", "description": "", "stage": "",
            "aliases": [], "confidence": 0, "reason": "尚未配置模型，按输入名称建立", "evidence": []}]
        return {"query": name, "method": "registry" if registry_hits else "manual",
                "candidates": candidates, "notes": "；".join(notes)}

    lines = [f"[{i + 1}] {e['title']} | {e['source']} | {e['url']} | 来源日期 {publication_day(e.get('published_at')) or '未知'}\n{e['snippet'][:300]}"
             for i, e in enumerate(recent_sources(evidence)[:20])]
    registry_text = "\n".join(
        f"- {c['legal_name']} | 信用代码 {c['credit_code']} | 成立 {c['founded']} | {c['location']} | {c['reason']}"
        for c in registry_hits)
    user = (f"输入名称：{name}\n\n"
            + (f"BP 摘录：\n{bp_text[:3000]}\n\n" if bp_text else "")
            + (f"工商登记查询结果（可信）：\n{registry_text}\n\n" if registry_text else "")
            + "公开搜索结果：\n" + ("\n".join(lines) or "（无）"))
    try:
        data = llm.json(_SYSTEM, user, max_tokens=2500)
    except ModelError as exc:
        raise ValueError(str(exc)) from exc
    if not isinstance(data, dict) or not isinstance(data.get("candidates", []), list):
        raise ValueError("主体识别模型返回的结构不正确，应包含 candidates 数组")
    candidates = []
    for c in (data.get("candidates") or [])[:4]:
        if not isinstance(c, dict) or not c.get("legal_name"):
            continue
        cleaned = {k: str(c.get(k) or "")[:500] for k in ENTITY_FIELDS}
        cleaned["aliases"] = [str(a)[:100] for a in (c.get("aliases") or []) if a][:10]
        cleaned["confidence"] = max(0, min(100, int(c.get("confidence") or 0)))
        cleaned["reason"] = str(c.get("reason") or "")[:500]
        cleaned["evidence"] = [str(u)[:500] for u in (c.get("evidence") or [])][:5]
        candidates.append(cleaned)
    if data.get("notes"):
        notes.append(str(data["notes"])[:500])
    return {"query": name, "method": "registry" if registry_hits else "ai_search",
            "candidates": candidates, "notes": "；".join(notes)}
