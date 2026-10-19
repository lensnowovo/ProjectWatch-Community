"""Peer race board: the company and its competitors measured on one ruler. Rules only, no model calls.

Every cell is a projection of stored, source-bound records — storylines, attributed event facts,
commercial financing records and collected reports — never a new fact:

* Commercialisation step: a selected dated product record, not a company-wide competitive rank. Plans
  and forecasts ("拟", "预计" …) never count as reached. Report titles only count when the company
  name leads the title, and are labelled as title-based with the subject still to be verified.
* Financing: vendor records first (original round / qualitative amount / investors kept as text),
  else a report title naming a round. Never summed, converted or merged across vendors.
* Evidence grade per cell: 公示 (government / exchange / registry hosts) > 数据商 > 媒体 > 公司宣传.

Absent data stays absent: unknown step, no financing record, R&D output not collected for peers.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from urllib.parse import urlsplit

from project_watch.studio.events import MARKET_APPROVAL, scope_dimensions
from project_watch.studio.peer_names import company_core as _core
from project_watch.studio.peer_names import company_name
from project_watch.studio.peer_names import headline_leads as _leads
from project_watch.studio.peer_names import peers_for as peers_for
from project_watch.studio.public_profile import safe_url

TEMPLATES: dict[str, dict] = {
    "hardware": {"label": "硬件", "steps": ["研发", "样品", "试用", "定点", "量产", "规模化"]},
    "pharma": {"label": "医药", "steps": ["临床前", "IND", "I 期", "II 期", "III 期", "上市申请", "获批"]},
    "software": {"label": "软件", "steps": ["产品", "试点", "付费客户", "规模化"]},
    "other": {"label": "其他", "steps": []},
}

# Highest step first; the first rule that matches wins.
_RULES: dict[str, list[tuple[int, re.Pattern]]] = {
    "hardware": [
        (5, re.compile(r"规模化|大批量")),
        (4, re.compile(r"量产|批量交付|批量供货|小批量|SOP")),
        (3, re.compile(r"定点|中标|订单|采购合同|供货协议")),
        (2, re.compile(r"试用|验证|送测|测试|导入|试点")),
        (1, re.compile(r"样品|送样|样机|原型机")),
    ],
    "pharma": [
        (6, MARKET_APPROVAL),
        (5, re.compile(r"NDA|BLA|上市申请|上市许可申请")),
        (4, re.compile(r"III\s*期|Ⅲ\s*期|三期临床|3期临床")),
        (3, re.compile(r"(?<![I])II\s*期(?!I)|Ⅱ\s*期|二期临床|2期临床")),
        (2, re.compile(r"(?<![I])I\s*期(?!I)|Ⅰ\s*期|一期临床|1期临床|首例受试者|首例患者")),
        (1, re.compile(r"IND|临床试验申请|默示许可|获批开展临床")),
    ],
    "software": [
        (3, re.compile(r"规模化|ARR|营收破|收入破")),
        (2, re.compile(r"签约|订单|中标|付费|采购")),
        (1, re.compile(r"试点|试用|POC|验证")),
        (0, re.compile(r"发布.{0,10}(?:产品|软件|平台)|推出.{0,10}(?:产品|软件|平台)")),
    ],
}
_PLANNED = re.compile(r"拟|计划|预计|有望|将于|将在|目标|力争|规划|筹备|传闻|或将|加速|推动|探索|助力|布局")

# Storyline stage → template step (hardware/software); pharma storylines go through the title rules.
_THREAD_STEP = {
    "hardware": {("客户与订单", "接触/试用"): 2, ("客户与订单", "定点/中标"): 3, ("客户与订单", "小批量"): 4,
                 ("客户与订单", "量产/交付"): 4, ("产品", "研发/预告"): 0, ("产品", "发布"): 1,
                 ("产品", "规模销售"): 5},
    "software": {("客户与订单", "接触/试用"): 1, ("客户与订单", "定点/中标"): 2, ("客户与订单", "小批量"): 2,
                 ("客户与订单", "量产/交付"): 2, ("产品", "发布"): 0, ("产品", "规模销售"): 3},
    "pharma": {("审评与临床", "获批"): 6},
}
# Attributed customer-channel event states (structured facts) → step.
_STATE_STEP = {
    "hardware": {"trial": 2, "agreement": 3, "signed": 3, "delivered": 4, "accepted": 4, "recognized": 4, "paid": 4},
    "software": {"trial": 1, "agreement": 2, "signed": 2, "delivered": 2, "accepted": 2, "recognized": 2, "paid": 2},
}

PUBLIC_HOSTS = ("gov.cn", "ccgp.gov.cn", "cninfo.com.cn", "sse.com.cn", "szse.cn", "bse.cn", "hkexnews.hk",
                "nmpa.gov.cn", "cde.org.cn", "clinicaltrials.gov", "chinadrugtrials.org.cn", "cnipa.gov.cn",
                "gsxt.gov.cn", "sec.gov")
GRADES = ("公示", "数据商", "媒体", "公司宣传")

_ROUND = re.compile(r"(天使\+?|种子|Pre-?A\+?|A\+{0,2}|B\+{0,2}|C\+{0,2}|D\+?|E\+?|F|Pre-IPO|战略)\s*轮", re.I)
_AMOUNT = re.compile(r"(?:超|近|逾)?(?:[\d.]+|数|[一二三四五六七八九十两]+)\s*(?:亿|千万|百万|万)(?:元|美元|人民币|港元)?")
_LEAD = re.compile(r"([一-龥A-Za-z]{2,12}?)(?:中国)?领投")


def template_for(project: dict, plan: dict | None) -> str:
    text = " ".join(str(project.get(k) or "") for k in ("industry", "description", "name"))
    if (plan or {}).get("is_biomed") or re.search(r"医药|生物|制药|药物|创新药|新药|药业|抗体|疫苗|细胞治疗|基因治疗", text):
        return "pharma"
    if re.search(r'硬件|激光雷达|芯片|半导体|光学|汽车电子|连接器|线束|天线|机器人|传感器|电池|装备', text):
        return 'hardware'
    if re.search(r"软件|SaaS|云服务|数据服务|企业服务|大模型|人工智能|AI|平台", text, re.I):
        return "software"
    return "other"




def _host(url: str) -> str:
    try:
        return (urlsplit(url or "").hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def grade_of(url: str, source: str = "", own_hosts: tuple[str, ...] = ()) -> str:
    # A source's display name is not proof that its URL is official.
    host = _host(url) if safe_url(url) else ""
    if host and any(host == h or host.endswith("." + h) for h in PUBLIC_HOSTS):
        return "公示"
    if host and any(host == h or host.endswith("." + h) for h in own_hosts if h):
        return "公司宣传"
    return "媒体"


def _day(value: str | None) -> str:
    return (value or "")[:10]


def _months_between(earlier: str, today: date) -> int | None:
    m = re.match(r"(\d{4})-(\d{2})", earlier or "")
    if not m:
        return None
    return max(0, (today.year - int(m.group(1))) * 12 + today.month - int(m.group(2)))


def _within(day: str, today: date, days: int) -> bool:
    try:
        return 0 <= (today - date.fromisoformat(day[:10])).days <= days
    except ValueError:
        return False


def classify_step(text: str, template: str) -> int | None:
    if not text or _PLANNED.search(text) or re.search(r'取消|延期|终止|暂停|否认|尚未|未能|未获|未实现|在即|即将', text):
        return None
    if template in ('hardware', 'software'):
        buyer = re.search(r'采购|购买|购入|招标|收到供应商|接收供应商', text)
        supplier = re.search(r'中标|获.{0,6}订单|赢得.{0,6}订单|获.{0,6}定点|供货|量产|批量交付', text)
        if buyer and (not supplier or buyer.start() < supplier.start()):
            return None
    for step, pattern in _RULES.get(template, []):
        if pattern.search(text):
            return step
    return None


def _event_title(e: dict) -> tuple[str, str, str]:
    src = next((s for s in e.get("sources") or [] if s.get("title")), {})
    return (src.get("title") or e.get("title") or ""), ((src.get('extra') or {}).get('resolved_url') or
            src.get("url") or e.get("url") or ""), (src.get("source") or e.get("source") or "")


def _event_day(e: dict) -> str:
    sources = e.get('sources') or []
    if any('published_at' in s for s in sources):
        return min((_day(s['published_at']) for s in sources if s.get('published_at')), default='')
    return _day(e.get('event_at') or e.get('created_at'))




def _mentions(title: str, names: list[str]) -> bool:
    return any(n and n in title for n in names)


def _candidate(step, day, grade, basis, title, url, outlets=(), scope=''):
    # Never time one drug/product's stage against a different product's next stage.
    codes = re.findall(r'(?<![A-Za-z0-9])[A-Z]{1,10}-?\d{2,7}(?![A-Za-z0-9])', title)
    return {"index": step, "date": day, "grade": grade, "basis": basis, "title": title, "url": url,
            "outlets": [o for o in outlets if o], 'scope': scope or (codes[0] if len(codes) == 1 else ''),
            'context': {k: sorted(v) for k, v in scope_dimensions(title).items() if k in ('indication','region')}}


def _outlets(e: dict) -> list[str]:
    """Distinct outlets that reported one event (a reprint by the same outlet counts once)."""
    return list(dict.fromkeys((s.get("source") or _host(s.get("url") or "")) for s in e.get("sources") or []
                              if s.get("source") or s.get("url")))


def _fact_candidates(items: list[dict], names: list[str], tpl: str) -> list[dict]:
    """Attributed, source-asserted commercial event states (structured facts) for one subject."""
    out = []
    for ev in items:
        if ev.get("support") != "source_assertion" or ev.get("state") in ('cancelled', 'delayed', 'withdrawn', 'paused'):
            continue
        subject = next((h.get("subject_name") for h in reversed(ev.get("history") or []) if h.get("subject_name")), "")
        if not subject or _core(subject) not in {_core(n) for n in names}:
            continue
        step = _STATE_STEP.get(tpl, {}).get(ev.get("state") or "") if ev.get('channel') == 'commercial' else None
        if step is None:
            step = classify_step(' '.join([ev.get('name') or '', *[h.get('state_text') or '' for h in
                                 ev.get('history', []) if h.get('state') == ev.get('state')]]), tpl)
        if step is not None and ev.get("effective_at"):
            src = (ev.get('sources') or [{}])[0]
            out.append(_candidate(step, _day(ev["effective_at"]), grade_of(src.get('url', ''), src.get('source', '')),
                                  'attributed_fact', ev.get('name') or '', src.get('url', ''),
                                   [s.get('source') or _host(s.get('url', '')) for s in ev.get('sources', [])],
                                   scope=ev.get('name') or ''))
    return out


def _best_stage(cands: list[dict], steps: list[str], today: date | None = None) -> dict | None:
    today = today or datetime.now(UTC).date()
    cands = [c for c in cands if c["index"] is not None and _within(c['date'], today, 36500)]
    if not cands:
        return None
    top = max(c["index"] for c in cands)
    at_top = sorted((c for c in cands if c["index"] == top), key=lambda c: (GRADES.index(c["grade"]), c["date"]))
    best = dict(at_top[0])
    best["label"] = steps[top]
    # Keep the selected record's date and outlets attached to its title/URL.
    # Another product reaching the same step is not an independent report of this event.
    # Pace only from the step directly below, so a product launch is never read as the previous customer step.
    below = [c for c in cands if c["index"] == top - 1 and c["date"] <= best["date"]
             and best.get('scope') and c.get('scope') == best['scope']
             and c.get('context') == best.get('context')]
    if below:
        prev = min(below, key=lambda c: c["date"])
        best["previous"] = {"label": steps[prev["index"]], "date": prev["date"]}
    return best


def _funding_from_title(title: str) -> dict | None:
    if "融资" not in title or _PLANNED.search(title):
        return None
    rnd = _ROUND.search(title)
    if not rnd:
        return None
    amount = _AMOUNT.search(title)
    lead = _LEAD.search(title)
    return {"round": rnd.group(0).replace(" ", ""), "amount": amount.group(0) if amount else "",
            "lead": lead.group(1) if lead else ""}


def _latest_funding(company_names: list[str], records: list[dict], events: list[dict], own_hosts) -> dict | None:
    vendor = [r for r in records if r.get("kind") == "funding" and _core(r.get("company", "")) in company_names
              and (r.get("date") or r.get("round"))]
    vendor.sort(key=lambda r: r.get("date") or "", reverse=True)
    best = None
    if vendor:
        r = vendor[0]
        same = [v["provider"] for v in vendor if v.get("round") == r.get("round")]
        best = {"round": r.get("round") or "未披露", "amount": r.get("amount") or "未披露", "date": _day(r.get("date")),
                "lead": r.get("investors") or "", "grade": "数据商", "providers": list(dict.fromkeys(same)),
                "title": "", "url": "", "stale": bool(r.get("stale"))}
    if best:
        return best  # A newer title cannot replace the configured data vendor's record.
    for e in sorted(events, key=_event_day, reverse=True):
        title, url, source = _event_title(e)
        found = _funding_from_title(title)
        if not found or not _leads(title, company_names):
            continue
        day = _event_day(e)
        if best and (best["date"] >= day or best["round"] == found["round"]):
            break
        best = {**found, "date": day, "grade": grade_of(url, source, own_hosts), "providers": [],
                "title": title, "url": url, "stale": False}
        break
    return best


def _key_move(events: list[dict], names: list[str], today: date, own_hosts, *, require_mention: bool) -> dict | None:
    rank = {"major": 0, "notable": 1, "routine": 2}
    pool = []
    for e in events:
        title, url, source = _event_title(e)
        day = _event_day(e)
        if not _within(day, today, 90) or (require_mention and not _leads(title, names)):
            continue
        topic = title + ' ' + (e.get('summary') or '')
        if re.search(r'股价|股票交易|盘中|涨幅|跌幅|涨停|跌停|资金流向|融资余额|融券|减持|套现|增持股份', topic):
            continue
        if re.search(r'深度研究|深度观察|研报|研究报告|访谈|成色|财报点评|中期报告|半年度报告|季度报告|年度报告', topic):
            continue
        if re.search(r'业务更新|业务最新情况|业务进展更新', topic):
            continue  # Publication itself does not establish an actual business milestone.
        pool.append((rank.get(e.get("importance") or "routine", 2), -date.fromisoformat(day).toordinal(), {
            "title": e.get("summary") or title, "url": url, "date": day, "grade": grade_of(url, source, own_hosts),
            "importance": e.get("importance") or "routine", "impact": e.get("impact") or "",
            "sources": len(e.get("sources") or []) or 1}))
    pool.sort(key=lambda x: (x[0], x[1]))
    return pool[0][2] if pool else None


def _public_funding(facts: list[dict], today: date) -> dict | None:
    """Cited dossier fallback, never a private numeric fact or funding receipt."""
    candidates = []
    for fact in facts:
        text = fact.get('text') or ''
        refs = [r for r in fact.get('refs') or [] if safe_url(r.get('url') or '') and r.get('quote')]
        if fact.get('kind') != 'financing' or not refs or _PLANNED.search(text):
            continue
        day = _day(fact.get('date'))
        if day and not _within(day, today, 36500):
            continue
        # Model wording alone cannot introduce an amount/round absent from the quote.
        ref = next((r for r in refs if re.search(r'融资|投资|轮', r['quote'])
                    and not _PLANNED.search(r['quote'])
                    and not re.search(r'取消|终止|否认|尚未|未完成|未能|未获|传闻', r['quote'])), None)
        if ref is None or re.search(r'取消|终止|否认|尚未|未完成|未能|未获', text):
            continue
        quote = ref['quote']
        rnd, amount = _ROUND.search(quote), _AMOUNT.search(quote)
        candidates.append({'round': rnd.group(0).replace(' ', '') if rnd else '未披露',
                           'amount': amount.group(0) if amount else '未披露', 'date': day,
                           'lead': '', 'grade': grade_of(ref['url'], ref.get('source', '')), 'providers': [],
                           'title': quote, 'url': ref['url'], 'stale': False, 'basis': 'public_profile'})
    return max(candidates, key=lambda f: f['date']) if candidates else None


# ---------------------------------------------------------------- extended info: team and customers

_TEAM = re.compile(r"出任|任命|聘任|升任|加盟|履新|离职|卸任|辞任|离任|新任|(?:加入|就任).{0,10}(?:CEO|CTO|CFO|COO|总裁|首席|董事|合伙人)")
_COUNTERPART = re.compile(r"(?:获得?|拿下|斩获|中标|入围|进入|签约|签下)(?P<who>[^，,。；;：:！!]{2,18}?)(?:的)?"
                          r"(?P<what>定点|订单|采购|项目|供应链|供货|框架协议|战略合作)")
_PARTNER = re.compile(r"与(?P<who>[^，,。；;：:！!]{2,18}?)(?:签署|签订|达成|开展|建立)(?P<what>[^，,。；;]{0,8}?(?:合作|协议|订单))")


def _fact_line(f: dict) -> dict:
    ref = (f.get("refs") or [{}])[0]
    return {"text": f"{f.get('label') + '：' if f.get('label') else ''}{f.get('text') or ''}", "date": f.get("date") or _day(ref.get("published_at")),
            "source": ref.get("source") or "", "url": ref.get("url") or "", "grade": grade_of(ref.get("url") or "", ref.get("source") or "")}


def _from_titles(events: list[dict], names: list[str]) -> tuple[list[dict], list[dict]]:
    team, customers, seen = [], [], set()
    for e in events:
        title, url, source = _event_title(e)
        if not _leads(title, names) or title in seen:
            continue
        seen.add(title)
        line = {"text": title, "date": _event_day(e), "source": "、".join(_outlets(e)[:2]) or source,
                "url": url, "grade": grade_of(url, source)}
        m = _COUNTERPART.search(title) or _PARTNER.search(title)
        if m and not _PLANNED.search(title):
            customers.append({**line, "who": m.group("who").strip(), "what": m.group("what").strip()})
        elif _TEAM.search(title):
            team.append(line)
    return team[:4], customers[:5]


def extras(names: list[str], events: list[dict], public_facts: list[dict] | None = None,
           relations: list[dict] | None = None) -> dict:
    """团队背景 and 客户 / 合作方 from public records; empty lists mean no public record, not absence."""
    team, customers = _from_titles(events, names)
    facts = public_facts or []
    team = [_fact_line(f) for f in facts if f.get("kind") == "person"][:5] + team
    customers = [{**_fact_line(f), "who": f.get("text") or "", "what": f.get("label") or ""} for f in facts
                 if f.get("kind") in ("customer", "partner") or re.search(r"客户|合作方", f.get("label") or "")] + customers
    for relation in relations or []:
        if (relation.get('visibility') != 'public' or relation.get('support') != 'source_assertion'
                or relation.get('subject_name') not in names or relation.get('role') not in ('customer', 'partner', 'pilot_partner')):
            continue
        source = next(iter(relation.get('sources') or []), {})
        role = {'customer':'客户', 'partner':'合作方', 'pilot_partner':'试点合作方'}[relation['role']]
        customers.insert(0, {'who':relation['peer_name'], 'what':role,
                            'text':f"{relation['subject_name']} · {relation['peer_name']}（{role}）", 'date':'',
                            'source':source.get('source',''), 'url':source.get('url',''),
                            'grade':grade_of(source.get('url',''), source.get('source',''))})
    return {"team": team[:6], "customers": customers[:6]}


def build(*, project: dict, plan: dict | None, peers: list[dict], company_events: list[dict],
          competitor_events: list[dict], threads: list[dict], event_states: list[dict],
          commercial_records: list[dict], rd_items: list[dict], today: date | None = None,
          template: str | None = None, public_facts: list[dict] | None = None,
          relations: list[dict] | None = None, quantity_events: list[dict] | None = None) -> dict:
    today = today or datetime.now(UTC).date()
    peers = [p for p in peers if company_name(p.get('name'))]
    tpl = template if template in TEMPLATES else template_for(project, plan)
    steps = TEMPLATES[tpl]["steps"]
    site = (project.get("website") or "").strip()
    own_hosts = (_host(site if "//" in site else "https://" + site),) if site else ()

    self_names = list(dict.fromkeys(n for n in (project.get("short_name"), project.get("name"), project.get("legal_name"),
                                                _core(project.get("legal_name") or ""), *(project.get("aliases") or [])) if n))
    # ---- the company itself
    cands = []
    for t in threads:
        if t.get('support') != 'source_assertion':
            continue  # legacy model storylines have not passed located actor/date checks
        for s in t.get("steps") or []:
            step = _THREAD_STEP.get(tpl, {}).get((t.get("kind"), s.get("stage")))
            if step is None:
                step = classify_step(s.get("title") or "", tpl)
            if step is not None and not _PLANNED.search(s.get('title') or ''):
                cands.append(_candidate(step, _day(s.get("date")), grade_of(s.get("url") or "", "", own_hosts),
                                        "storyline", s.get("title") or "", s.get("url") or ""))
    cands += _fact_candidates(event_states, self_names, tpl)
    for e in company_events:
        title, url, source = _event_title(e)
        if _leads(title, self_names):
            step = classify_step(title, tpl)
            if step is not None:
                cands.append(_candidate(step, _event_day(e),
                                        grade_of(url, source, own_hosts), "title", title, url, _outlets(e)))
    from project_watch.studio.sources import paper_affiliated

    # Topic/affiliation-query hits are not proof the company authored a work.
    own_papers = [i for i in rd_items if i.get("category") == "paper" and paper_affiliated(i, self_names)]
    papers = sum(1 for i in own_papers if _within(_day(i.get("published_at")), today, 365)) if own_papers else None
    patents = sum(1 for i in rd_items if i.get("category") == "patent" and _within(_day(i.get("published_at") or i.get("collected_at")), today, 365))
    if not any(i.get("category") == "patent" for i in rd_items):
        patents = None  # no patent source has ever returned anything: "not collected", not zero
    rows = [_row(name=project.get("short_name") or project.get("name") or "", note="本公司", is_self=True,
                 stage=_best_stage(cands, steps, today), funding=_latest_funding([_core(n) for n in self_names] + self_names,
                                                                          commercial_records, company_events, own_hosts),
                move=_key_move(company_events, self_names, today, own_hosts, require_mention=True),
                 rd={"papers": papers, "patents": patents, "collected": True}, today=today)]
    rows[0]["extras"] = extras(self_names, company_events, public_facts, relations)
    if rows[0]['funding'] is None:
        rows[0]['funding'] = _public_funding(public_facts or [], today)
        rows[0]['months_since_funding'] = _months_between(rows[0]['funding']['date'], today) if rows[0]['funding'] else None
        rows[0]['recent']['funding'] = bool(rows[0]['funding'] and _within(rows[0]['funding']['date'], today, 30))

    # ---- competitors
    for peer in peers:
        names = list(dict.fromkeys(n for n in [peer["name"], _core(peer["name"]), *(peer.get("aliases") or [])] if n))
        mine = [e for e in competitor_events if _mentions(_event_title(e)[0], names)]
        pc = _fact_candidates(event_states, names, tpl)
        for e in mine:
            title, url, source = _event_title(e)
            if _leads(title, names):
                step = classify_step(title, tpl)
                if step is not None:
                    pc.append(_candidate(step, _event_day(e),
                                         grade_of(url, source), "title", title, url, _outlets(e)))
        rows.append(_row(name=peer["name"], note=peer.get("note") or "", is_self=False, stage=_best_stage(pc, steps, today),
                         funding=_latest_funding(names, commercial_records, mine, ()),
                         move=_key_move(mine, names, today, (), require_mention=True),
                         rd={"papers": None, "patents": None, "collected": False}, today=today))
        rows[-1]["extras"] = extras(names, mine, relations=relations)
        rows[-1]['relation_type'] = peer.get('relation_type') or 'unknown'

    known = [r for r in rows if r["stage"]]
    from project_watch.studio.comparisons import quantities
    names = [{'name': rows[0]['name'], 'aliases': self_names}, *peers]
    numeric = quantities(names, quantity_events if quantity_events is not None else [*company_events, *competitor_events], today=today)
    return {"template": tpl, "template_label": TEMPLATES[tpl]["label"], "steps": steps, "rows": rows, 'quantities': numeric,
            "comparable": 0, "staged": len(known), "generated_at": datetime.now(UTC).isoformat(),
            "grades": list(GRADES), "position": position(rows, today)}


# ---------------------------------------------------------------- position line (位置 · 进展 · 资本 · 威胁)

_ROUND_ORDER = (("种子", 0), ("天使", 1), ("PRE-A", 2), ("A", 3), ("B", 4), ("C", 5), ("D", 6), ("E", 7), ("F", 8))


def round_rank(text: str | None) -> float | None:
    t = (text or "").upper().replace(" ", "").replace("轮", "")
    if not t or "战略" in t or "PRE-IPO" in t:
        return 9.0 if "PRE-IPO" in t else None
    for key, rank in _ROUND_ORDER:
        if t.startswith(key):
            return rank + 0.5 * t[len(key):].count("+")
    return None


def _short(text: str, n: int = 40) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def position(rows: list[dict], today: date) -> dict:
    """Rule-written summary: where the company stands, how it moved, its capital, the nearest threat.

    Selected stages are not paired product/indication/region/period metrics, so
    this view states records without ranking companies or financing strength.
    """
    me = next((r for r in rows if r["is_self"]), None)
    if not me:
        return {"sentence": "", "parts": [], "chips": []}
    peers = [r for r in rows if not r["is_self"]]
    parts, chips = [], []

    # 位置 + 进展
    s = me["stage"]
    if s:
        # These are per-company selected records, not paired product/indication,
        # region/period metrics. Counting rows cannot turn them into a rank.
        where = f"公开记录显示“{s['label']}”" + (f"（{s['scope']}）" if s.get('scope') else '')
        chips.append({"label": "已披露阶段", "value": s["label"], "note": s["date"][:7], "tone": ""})
        if _within(s["date"], today, 90) and s.get('previous'):
            moved = f"近 90 天上了一级台阶（{s['label']}）"
            chips[-1]["note"] = "↑ 近 90 天上一级"
            chips[-1]["tone"] = "up"
        elif _within(s["date"], today, 90):
            moved = f"近 90 天有“{s['label']}”的公开记录"
            chips[-1]["note"] = "没有前一级日期，暂不判断推进速度"
        else:
            moved = ''  # Source absence cannot establish stalled product progress.
        parts.append({"kind": "position", "text": where + (f"，{moved}" if moved else "")})
    # No stage record: say nothing rather than a placeholder (absence of records is not absence of progress).

    # 资本
    f = me["funding"]
    if f:
        m = me["months_since_funding"]
        head = (f"公开档案记录：{_short(f['title'])}" if f.get('basis') == 'public_profile' else
                f"刚完成{f['round']}" if m is not None and m <= 3 else f"{f['round']}已过去 {m} 个月" if m is not None else f"最近一轮为{f['round']}")
        parts.append({"kind": "capital", "text": head})
        chips.append({"label": "资本", "value": f["round"], "note": f"{m} 个月前" if m is not None else "", "tone": ""})
    # No financing record: likewise left out.

    # Recent peer records are not, by themselves, comparative business threats.
    from project_watch.studio.red_flags import classify as negative

    rank = {"major": 0, "notable": 1, "routine": 2}
    threats, openings = [], []
    for r in peers:
        mv = r["move"]
        if mv and _within(mv["date"], today, 30) and negative(mv["title"]):
            # A competitor's trouble is not a threat to the company; it is reported as an opening.
            if negative(mv["title"])[0] != "低":
                openings.append((-date.fromisoformat(mv['date']).toordinal(), mv["title"], mv["date"]))
            mv = None
        if mv and _within(mv["date"], today, 30) and mv["importance"] != "routine":
            threats.append((rank[mv["importance"]], -date.fromisoformat(mv['date']).toordinal(), r["name"], mv["title"], mv["date"]))
        st = r["stage"]
        if st and _within(st["date"], today, 30):
            threats.append((0, -date.fromisoformat(st['date']).toordinal(), r["name"],
                            st.get('title') or f"{r['name']}到达“{st['label']}”", st["date"]))
    threats.sort()
    if threats:
        _, _, who, what, day = threats[0]
        parts.append({"kind": "threat", "text": f"同行近期进展：{_short(what)}（{day[5:10]}）", "who": who})
        chips.append({"label": "近 30 天同行进展", "value": f"{len({t[2] for t in threats})} 家公司", "note": who, "tone": ""})
    if openings:
        openings.sort()
        parts.append({"kind": "opening", "text": f"同行负面消息：{_short(openings[0][1])}（{openings[0][2][5:10]}）"})
    sentence = "。".join(p["text"] for p in parts) + "。" if parts else ""
    return {"sentence": sentence, "parts": parts, "chips": chips, "ranked": False}


def _row(*, name, note, is_self, stage, funding, move, rd, today) -> dict:
    months = _months_between(funding["date"], today) if funding and funding.get("date") else None
    pace = None
    if stage and stage.get("previous"):
        m = re.match(r"(\d{4})-(\d{2})", stage["date"])
        pace = _months_between(stage["previous"]["date"], date(int(m.group(1)), int(m.group(2)), 1)) if m else None
    return {"name": name, "note": note, "is_self": is_self, "stage": stage, "pace_months": pace,
            "funding": funding, "months_since_funding": months, "move": move, "rd": rd,
            "recent": {"stage": bool(stage and _within(stage["date"], today, 30)),
                       "funding": bool(funding and funding.get("date") and _within(funding["date"], today, 30)),
                       "move": bool(move and _within(move["date"], today, 30))}}
