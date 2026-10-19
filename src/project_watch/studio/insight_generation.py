"""Optional private deep-judgment generation; injected by the Studio composition root."""

from __future__ import annotations

import hashlib
import json
from contextlib import closing
from datetime import UTC, datetime, timedelta

from project_watch.studio.insight import SOURCE_LABEL, VERDICTS, counts, held, load, save

MIN_HOURS_BETWEEN_AUTO = 20
CONFIDENCE = ("high", "medium", "low")

SYSTEM = """你是一级市场投资研究员。根据给定的编号素材，对一家被监控公司做交叉分析，回答六个问题：
1. 竞争位置：与给出的竞争对手相比，处于领先还是落后？比较要落到具体维度（产品阶段、获批/商业化进度、收入规模、融资与现金、技术指标等）。
2. 技术方向：公司押注的技术路线是什么？处于前沿、较先进、主流、成熟还是趋于落后？
3. 共识程度：这条路线是行业与学术界的共识方向、正在形成共识，还是非共识？学术界（论文）与产业界（融资、竞品、产品）的信号要分开说。
4. 行业发展：这个行业整体处于快速扩张、增长、平稳、整合还是降温？主要驱动与阻力是什么？
5. 公司运营：公司自身的产品与管线推进、收入与现金、团队与融资，整体在向好、平稳还是承压？
6. 产业链：客户与渠道、关键供应商与原料、合作方与授权方、投资方的变化，对公司整体是有利、平稳还是承压？

规则：
- 每一条依据都必须引用至少一个素材编号（如 N3、D2、A5），只能引用给出的编号；没有依据的话不要写。
- 依据不足时 verdict 写 "unclear"，并在 gaps 里写明缺什么信息。不要用常识或训练记忆补齐事实。
- confidence 只能是 high / medium / low：多个独立来源一致才可 high；只有单一来源或推断为主时 low。
- 同时给出支持与反对的依据；写出“什么信息出现会改变这个判断”。
- 公司自身事实优先使用内部资料（D、M）与融资数据库（F，如提供）；公开报道（N、C、I、W）未经公司确认；竞品新闻不能写成本公司的事。
- 用简体中文，句子简短具体，包含数字和时间。
- headline、summary 与各条 text 只写给投资人看的中文结论：判断用“领先／略领先／相当／略落后／落后／无法判断”等中文词，
  不要出现 verdict 取值（如 unclear、ahead）等英文代号，不要提“题设”“素材”“编号”或提示词本身；出处只放在 refs 里。

输出 JSON：
{"headline": "一句话总判断（≤60字）",
 "position": {"verdict": "leading|ahead|on_par|behind|lagging|unclear", "confidence": "high|medium|low",
   "summary": "≤80字", "peers": ["对比的公司名"],
   "for": [{"text": "支持该判断的依据", "refs": ["N1"]}], "against": [{"text": "相反的依据", "refs": ["C2"]}],
   "change_if": ["什么新信息会改变判断"]},
 "technology": {"route": "押注的技术路线（≤30字）", "maturity": "frontier|advanced|mainstream|mature|dated|unclear",
   "consensus": "consensus|forming|contrarian|unclear", "confidence": "high|medium|low", "summary": "≤80字",
   "academic": [{"text": "学术界信号", "refs": ["A1"]}], "industry": [{"text": "产业界信号", "refs": ["C1"]}],
   "risks": [{"text": "技术路线风险", "refs": ["D3"]}]},
 "industry": {"verdict": "expanding|growing|stable|consolidating|cooling|unclear", "confidence": "high|medium|low",
   "summary": "≤80字", "drivers": [{"text": "驱动", "refs": ["I1"]}], "headwinds": [{"text": "阻力", "refs": ["I2"]}]},
 "operations": {"verdict": "improving|steady|pressured|unclear", "confidence": "high|medium|low", "summary": "≤80字",
   "for": [{"text": "向好的依据", "refs": ["D1"]}], "against": [{"text": "承压的依据", "refs": ["M2"]}], "change_if": ["什么新信息会改变判断"]},
 "chain": {"verdict": "favorable|steady|pressured|unclear", "confidence": "high|medium|low", "summary": "≤80字",
   "for": [{"text": "有利的依据", "refs": ["D3"]}], "against": [{"text": "不利的依据", "refs": ["N4"]}], "change_if": ["什么新信息会改变判断"]},
 "gaps": ["缺少的关键信息"]}"""


def _day(value) -> str:
    return str(value or "")[:10]


def _clip(text, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def gather(pipeline, project_id: str, llm=None) -> dict:
    """Number every source the company is monitored through. Returns the prompt, the numbered map and a fingerprint."""
    from project_watch.studio import finance
    from project_watch.studio.peer_names import peers_for

    store = pipeline.store
    project = store.get_project(project_id) or {}
    view = pipeline.model_view(llm)
    profile = view.get_profile(project_id) or {}
    numbered: dict[str, dict] = {}

    def add(prefix: str, entry: dict) -> None:
        code = f"{prefix}{sum(1 for k in numbered if k[0] == prefix) + 1}"
        numbered[code] = entry

    def internal(text, refs) -> dict:
        ref = (refs or [{}])[0] or {}
        return {"kind": "internal", "text": _clip(text, 220), "source_id": ref.get("source_id"),
                "source_name": ref.get("source_name") or "内部资料", "locator": ref.get("locator") or ""}

    if profile.get("summary"):
        add("D", {"kind": "internal", "text": _clip(profile["summary"], 400), "source_name": "内部资料概述", "locator": ""})
    for section in profile.get("sections", []):
        for point in section.get("points", [])[:6]:
            add("D", {**internal(f"{section.get('title', '')}：{point.get('text', '')}", point.get("refs")), "group": section.get("title", "")})
    for key in ("highlights", "risks", "milestones"):
        for point in profile.get(key, [])[:6]:
            add("D", {**internal(point.get("text", ""), point.get("refs")), "group": {"highlights": "亮点", "risks": "风险", "milestones": "里程碑"}[key]})

    metrics = view.list_metrics(project_id)
    fin = finance.view(metrics)
    k = fin["key_numbers"]
    for label, cell in (("营业收入", k.get("revenue")), ("净利润", k.get("net_profit")), ("账上现金", k.get("cash"))):
        if cell:
            ref = (cell.get("refs") or [{}])[0]
            add("M", {"kind": "internal", "text": f"{label} {cell['period']} {cell['value']:,.0f} {cell['unit']}",
                      "source_id": ref.get("source_id"), "source_name": ref.get("source_name"), "locator": ref.get("locator")})
    if k.get("revenue_growth") is not None:
        add("M", {"kind": "internal", "text": f"收入同比 {k['revenue_growth']}%（{(k.get('revenue') or {}).get('period', '')}）", "source_name": "规则计算"})
    if (runway := k.get("runway")) and runway.get("months") is not None:
        add("M", {"kind": "internal", "text": f"现金跑道约 {runway['months']} 个月（{runway['text']}）", "source_name": "规则计算"})
    for row in fin["ratios"]:
        latest = sorted(row["cells"].items())[-1:] if row["cells"] else []
        for period, cell in latest:
            if cell["basis"] == "actual":
                add("M", {"kind": "internal", "text": f"{row['name']} {period} {cell['value']}%", "source_name": "规则计算"})

    # Professional databases (烯牛 / 医药魔方 / Wind): financing rounds of the company and its peers — only when the
    # owner allowed vendor results to be sent to the model (设置 → 专业数据来源 → 交给 AI 分析).
    commercial_config = pipeline.settings_store.load().commercial
    records = []
    if commercial_config.model_use:
        try:
            from project_watch.studio.commercial import CommercialStore, targets

            records = CommercialStore(store).view(project_id, commercial_config,
                                                  targets=targets(pipeline, project)).get("records", [])
        except Exception:  # Optional source; analysis continues without it.
            records = []
    for record in [r for r in records if r.get("kind") == "funding"][:20]:
        who = "本公司" if record.get("relationship") in (None, "", "company", "self") else f"同行（{record.get('relationship')}）"
        add("F", {"kind": "public", "text": _clip(f"{who} {record.get('company', '')} {record.get('date', '')} {record.get('round') or '融资'} "
                                                  f"{record.get('amount') or '金额未披露'}；投资方：{record.get('investors') or '未披露'}", 220),
                  "title": record.get("title") or f"{record.get('company', '')} {record.get('round', '')}", "url": record.get("url") or "",
                  "date": _day(record.get("date")), "source": record.get("provider", ""), "group": "融资"})

    def public(event: dict, label: str) -> dict:
        return {"kind": "public", "text": _clip(event.get("summary") or event.get("title"), 200), "title": _clip(event.get("title"), 120),
                "url": event.get("url") or "", "date": _day(event.get("published_at") or event.get("event_at") or event.get("collected_at")),
                "source": event.get("source") or "", "group": label}

    for prefix, category, label, limit in (("N", "company", "公司新闻", 30), ("C", "competitor", "竞品动态", 30),
                                           ("I", "industry", "行业", 20), ("I", "policy", "政策", 12)):
        for event in store.list_events(project_id=project_id, category=category, since_days=540, limit=limit):
            add(prefix, public(event, label))
    for prefix, category, label, limit in (("A", "paper", "论文", 20), ("P", "patent", "专利", 12), ("T", "trial", "临床试验", 12)):
        for item in store.list_items(project_id=project_id, category=category, limit=limit):
            add(prefix, public(item, label))

    landscape = store.get_landscape(project_id) or {}
    industry = landscape.get("industry") if isinstance(landscape.get("industry"), dict) else {}
    if industry.get("overview"):
        add("L", {"kind": "public", "text": _clip(industry["overview"], 300), "title": "行业摘要（AI 整理）", "url": "", "group": "行业整理"})
    for fact in (industry.get("facts") or [])[:12]:
        ref = (fact.get("refs") or [{}])[0] or {}
        add("L", {"kind": "public", "text": _clip(f"{fact.get('kind', '')}：{fact.get('text', '')}", 200), "title": ref.get("title", ""),
                  "url": ref.get("url", ""), "date": _day(ref.get("date")), "group": "行业整理"})
    for company in ((landscape.get("competition") or {}).get("companies") or [])[:8]:
        cells = "；".join(f"{dim}：{_clip(cell.get('text'), 60)}" for dim, cell in (company.get("cells") or {}).items() if cell.get("text"))
        if cells:
            add("L", {"kind": "public", "text": _clip(f"{company.get('name', '')}｜{cells}", 260), "title": company.get("name", ""), "url": "", "group": "竞争整理"})
    for fact in ((store.get_public_profile(project_id) or {}).get("facts") or [])[:12]:
        ref = (fact.get("refs") or [{}])[0] or {}
        add("W", {"kind": "public", "text": _clip(f"{fact.get('label', '')}：{fact.get('text', '')}", 200), "title": ref.get("title", ""),
                  "url": ref.get("url", ""), "date": _day(ref.get("published_at")), "group": "公开档案"})

    try:
        peers = [p["name"] for p in peers_for(project, store.get_plan(project_id),
                                              pipeline.relationships.view(project_id, include_internal=False),
                                              public_objects=pipeline.objects.page(project_id, include_internal=False, current_aliases_only=True))]
    except Exception:  # Peer discovery is optional context; the analysis still runs without it.
        peers = []

    lines = [f"[{code}] ({SOURCE_LABEL[code[0]]}{'·' + e['group'] if e.get('group') else ''}{' ' + e['date'] if e.get('date') else ''}) {e['text']}"
             for code, e in numbered.items()]
    text = (f"被监控公司：{project.get('legal_name') or project.get('name')}（{project.get('industry') or '行业未注明'}，{project.get('stage') or '阶段未注明'}）\n"
            f"已知竞争对手：{'、'.join(peers) if peers else '（尚未确认，请从素材中识别并说明依据）'}\n"
            f"素材统计：" + "，".join(f"{SOURCE_LABEL[p]} {n}" for p, n in counts(numbered).items()) + "\n\n" + "\n".join(lines))
    fingerprint = hashlib.sha256(json.dumps([(c, e["text"]) for c, e in numbered.items()], ensure_ascii=False).encode()).hexdigest()[:40]
    return {"numbered": numbered, "text": text, "fingerprint": fingerprint, "peers": peers}

def _ref(code: str, numbered: dict) -> dict | None:
    entry = numbered.get(str(code).strip().upper())
    if not entry:
        return None
    out = {"code": str(code).strip().upper(), "kind": entry["kind"], "label": SOURCE_LABEL[str(code).strip().upper()[0]],
           "text": entry.get("title") or entry["text"], "date": entry.get("date", "")}
    if entry["kind"] == "internal":
        out.update(source_id=entry.get("source_id"), source_name=entry.get("source_name"), locator=entry.get("locator"))
    else:
        out.update(url=entry.get("url", ""), source=entry.get("source", ""))
    return out


def _points(values, numbered: dict, limit: int = 6) -> list[dict]:
    out = []
    for value in values or []:
        if not isinstance(value, dict) or not str(value.get("text") or "").strip():
            continue
        refs = [r for r in (_ref(code, numbered) for code in value.get("refs") or []) if r]
        if refs:  # A judgment without a valid source is not kept.
            out.append({"text": _clip(value["text"], 240), "refs": refs[:4]})
    return out[:limit]


def _confidence(value, points: list[dict]) -> str:
    """Model confidence is capped by evidence: high needs 3+ distinct sources, medium needs 2."""
    distinct = {r["code"] for p in points for r in p["refs"]}
    value = value if value in CONFIDENCE else "low"
    if value == "high" and len(distinct) < 3:
        value = "medium"
    if value == "medium" and len(distinct) < 2:
        value = "low"
    return value


def normalize(data: dict, numbered: dict) -> dict:
    data = data if isinstance(data, dict) else {}

    def section(name: str, lists: tuple[str, ...]) -> dict:
        raw = data.get(name) if isinstance(data.get(name), dict) else {}
        out = {key: _points(raw.get(key), numbered) for key in lists}
        evidence = [p for key in lists for p in out[key]]
        out["summary"] = _clip(raw.get("summary"), 160)
        out["confidence"] = _confidence(raw.get("confidence"), evidence)
        return out, raw, evidence

    position, raw, evidence = section("position", ("for", "against"))
    position["verdict"] = raw.get("verdict") if raw.get("verdict") in VERDICTS["position"] and evidence else "unclear"
    position["peers"] = [_clip(p, 30) for p in (raw.get("peers") or []) if isinstance(p, str) and p.strip()][:8]
    position["change_if"] = [_clip(t, 120) for t in (raw.get("change_if") or []) if isinstance(t, str) and t.strip()][:4]

    technology, raw, evidence = section("technology", ("academic", "industry", "risks"))
    technology["route"] = _clip(raw.get("route"), 60)
    technology["maturity"] = raw.get("maturity") if raw.get("maturity") in VERDICTS["maturity"] and evidence else "unclear"
    # Consensus needs a signal from at least one side (papers or industry); risks alone do not establish it.
    signals = technology["academic"] + technology["industry"]
    technology["consensus"] = raw.get("consensus") if raw.get("consensus") in VERDICTS["consensus"] and signals else "unclear"

    industry, raw, evidence = section("industry", ("drivers", "headwinds"))
    industry["verdict"] = raw.get("verdict") if raw.get("verdict") in VERDICTS["industry"] and evidence else "unclear"

    rings = {}
    for name in ("operations", "chain"):
        part, raw, evidence = section(name, ("for", "against"))
        part["verdict"] = raw.get("verdict") if raw.get("verdict") in VERDICTS[name] and evidence else "unclear"
        part["change_if"] = [_clip(t, 120) for t in (raw.get("change_if") or []) if isinstance(t, str) and t.strip()][:4]
        rings[name] = part

    for part in (position, technology, industry, *rings.values()):
        if not any(isinstance(v, list) and v and isinstance(v[0], dict) for v in part.values()):
            part["confidence"] = "low"
    cited = sorted({r["code"] for part in (position, technology, industry, *rings.values()) for v in part.values()
                    if isinstance(v, list) for p in v if isinstance(p, dict) for r in p.get("refs", [])},
                   key=lambda c: (c[0], int(c[1:]) if c[1:].isdigit() else 0))
    return {"headline": _clip(data.get("headline"), 90), "position": position, "technology": technology, "industry": industry,
            "operations": rings["operations"], "chain": rings["chain"],
            "gaps": [_clip(t, 120) for t in (data.get("gaps") or []) if isinstance(t, str) and t.strip()][:8],
            "cited": [_ref(code, numbered) for code in cited]}

def run(pipeline, llm, project_id: str) -> dict:
    if held(pipeline.store, project_id):
        raise ValueError("该项目有结果不明的模型请求等待确认恢复；请先处理原任务，深度分析不会绕过它。")
    gathered = gather(pipeline, project_id, llm)
    if not gathered["numbered"]:
        raise ValueError("还没有可分析的素材：请先上传资料或等待首轮信息收集完成。")
    data = llm.json(SYSTEM, gathered["text"], max_tokens=6000)
    return save(pipeline.store, project_id, normalize(data, gathered["numbered"]), gathered,
                model=getattr(getattr(llm, "config", None), "model", ""))

def due(pipeline, project_id: str, *, now: datetime | None = None) -> str | None:
    """Fingerprint to analyse when sources changed materially and the last run is old enough; None otherwise.

    Waits while collection or analysis jobs for the project are still running, so one pass sees settled inputs.
    """
    store = pipeline.store
    if held(store, project_id):
        return None
    with closing(store._connect()) as db:
        busy = db.execute("SELECT 1 FROM studio_jobs WHERE project_id=? AND status IN ('queued','running') "
                          "AND kind IN ('internal','external','external_followup','brief','landscape','public_profile','insight') LIMIT 1",
                          (project_id,)).fetchone()
    if busy:
        return None
    previous = load(store, project_id)
    moment = now or datetime.now(UTC)
    if previous and moment - datetime.fromisoformat(previous["generated_at"]) < timedelta(hours=MIN_HOURS_BETWEEN_AUTO):
        return None
    gathered = gather(pipeline, project_id)
    if not gathered["numbered"] or (previous and previous.get("fingerprint") == gathered["fingerprint"]):
        return None
    return gathered["fingerprint"]
