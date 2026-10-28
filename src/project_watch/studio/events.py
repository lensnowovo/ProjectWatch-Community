"""Group reports of the same real-world event across outlets.

Deterministic first (title shingles + time window + conflicting-facts guard),
then an optional model check for ambiguous pairs. Reports whose amounts, rounds
or dates disagree are never merged, so a 2亿 and a 5亿 round stay separate.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal

MERGE_SIMILARITY = 0.5
ASK_SIMILARITY = 0.22
WINDOW = timedelta(hours=72)
SALES_WINDOW = timedelta(days=120)
RANK = {"major": 0, "notable": 1, "routine": 2}

_PUNCT = re.compile(r"[\s\W_]+", re.UNICODE)
_AMOUNT = re.compile(r"((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*(亿|千万|百万|万|billion|million|bn|m)(?![a-z])", re.I)
_ROUND_CTX = re.compile(r"(pre[-\s]?[a-z]\+{0,2}|天使|种子|[a-z]\+{0,2})\s*轮|series\s*([a-z]\+{0,2})|\bipo\b|战略融资", re.I)
_UNIT = {"亿": 1e8, "千万": 1e7, "百万": 1e6, "万": 1e4, "billion": 1e9, "bn": 1e9,
         "million": 1e6, "m": 1e6}

MARKET_APPROVAL = re.compile(r'获批上市|批准上市|上市获批|获得.{0,8}注册证|'
                            r'上市(?:许可)?申请.{0,12}(?:获批|获(?:得)?(?:NMPA|FDA|EMA)?批准)', re.I)
_QUARTER = re.compile(r'(20\d{2})\s*(?:年\s*第?([一二三四1234])\s*季度?|[-\s年]*Q([1-4]))', re.I)
_HALF = re.compile(r'(20\d{2})\s*(?:年\s*([上下])半年|[-\s年]*H([12]))', re.I)
_QUANTITY = re.compile(r'(\d+(?:,\d{3})*(?:\.\d+)?)\s*(万|百万|千万|亿)?\s*(台|颗|件|支|套)')


def scope_dimensions(text: str) -> dict[str, set[str]]:
    """Only explicit recognized dimensions; absence is not a matching business scope.

    This small vocabulary is a conflict guard, not general entity/clinical extraction.
    It never writes normalized values back over source assertions.
    """
    period = {f'{y}Q{n or q}' for y, n, q in _QUARTER.findall(text)
              for n in [str('一二三四'.index(n)+1) if n in '一二三四' and n else n]}
    period |= {f'{y}H{h or (1 if half == "上" else 2)}' for y, half, h in _HALF.findall(text)}
    if not period:
        period = {f'{y}Y' for y in re.findall(r'(20\d{2})\s*年度', text)}
    product = set(re.findall(r'(?<![A-Za-z0-9])[A-Z]{1,10}-?\d{2,7}(?![A-Za-z0-9])', text))
    product |= set(re.findall(r'[“「]([^”」]{2,60})[”」](?:产品|药物|车型)', text))
    indication = set(re.findall(r'非小细胞肺癌|小细胞肺癌|肺癌|肝癌|乳腺癌|胃癌|糖尿病|肥胖症', text))
    region = {name for name, pattern in [('CN',r'中国|国内'), ('US',r'美国'), ('EU',r'欧洲|欧盟'),
              ('GLOBAL',r'全球')] if re.search(pattern, text)}
    metric = {name for name, pattern in [('sales',r'销量|销售量'), ('shipments',r'出货量|交付量'),
              ('revenue',r'销售额|营收|营业收入')] if re.search(pattern, text)}
    segment = {name for name, pattern in [('ADAS',r'ADAS|辅助驾驶'), ('robot',r'机器人'),
               ('total',r'总销量|总出货|总交付|合计销量')] if re.search(pattern, text, re.I)}
    regulatory = set()
    if MARKET_APPROVAL.search(text):
        regulatory.add('market_approved')
    elif re.search(r'上市(?:许可)?申请|NDA|BLA', text):
        regulatory.add('market_application')
    elif re.search(r'IND|临床试验申请|获批开展临床', text):
        regulatory.add('trial_application')
    return {'period':period, 'product':product, 'indication':indication, 'region':region,
            'metric':metric, 'segment':segment, 'regulatory':regulatory,
            'basis':{'forecast' if re.search(r'预计|预期|预测|目标|计划|有望|拟于', text) else 'reported'}}


def _quantities(text):
    scale = {'':1, '万':10000, '百万':1000000, '千万':10000000, '亿':100000000}
    return {(Decimal(n.replace(',', '')) * scale[u], unit) for n, u, unit in _QUANTITY.findall(text)}


def _actor(text):
    head = re.sub(r'^\s*(?:[【\[][^】\]]{0,30}[】\]]|[^｜|]{1,20}[｜|])\s*', '', text)
    actor = re.split(r'20\d{2}|披露|公布|宣布|实现|发布|录得|报告|完成|获得|获批', head, maxsplit=1)[0].strip(' ：:')
    return actor if re.fullmatch(r'[一-龥]{2,30}', actor) else None


def _sales_key(text):
    dims = scope_dimensions(text)
    if len(dims['period']) != 1 or len(dims['metric']) != 1 or dims['metric'] == {'revenue'}:
        return None
    if not re.fullmatch(r'20\d{2}Q[1-4]', next(iter(dims['period']))):
        return None
    if not _quantities(text) or dims['basis'] == {'forecast'}:
        return None
    # Exact headline actor only: no suffix/alias/parent-company guessing.
    actor = _actor(text)
    if not actor:
        return None
    return (actor, *(tuple(sorted(dims[k])) for k in ('period','metric','segment','product','indication','region')),
            tuple(sorted(_quantities(text))))


def lookback_days(items: list[dict]) -> int:
    """Load older quarterly reports only when the incoming batch can match them."""
    return SALES_WINDOW.days if any(_sales_key(' '.join([i['title'], i.get('summary') or '']))
                                   for i in items) else 21


def normalize(title: str) -> str:
    return _PUNCT.sub("", (title or "").lower())


def shingles(text: str) -> set[str]:
    text = normalize(text)
    if len(text) < 2:
        return {text} if text else set()
    return {text[i:i + 2] for i in range(len(text) - 1)}


def similarity(a: str, b: str) -> float:
    sa, sb = shingles(a), shingles(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def facts(text: str) -> tuple[set[float], set[str]]:
    """Amounts (normalised to a base unit) and financing rounds mentioned in the text."""
    text = text or ""
    amounts = {round(float(n.replace(",", "")) * _UNIT[u.lower()], -4) for n, u in _AMOUNT.findall(text)}
    rounds = set()
    for match in _ROUND_CTX.finditer(text):
        token = (match.group(1) or match.group(2) or match.group(0)).lower()
        rounds.add(re.sub(r"[-\s]", "", token.replace("series", "")))
    return amounts, rounds


def conflicting(a: str, b: str) -> bool:
    actor_a, actor_b = _actor(a), _actor(b)
    if actor_a and actor_b and actor_a != actor_b:
        return True
    da, db = scope_dimensions(a), scope_dimensions(b)
    if any(da[k] and db[k] and not (da[k] <= db[k] or db[k] <= da[k]) for k in da):
        return True
    qa, qb = _quantities(a), _quantities(b)
    if da['metric'] and db['metric'] and qa and qb and not (qa <= qb or qb <= qa):
        return True
    def patents(text):
        if not re.search(r"专利|\bpatent\b", text or "", re.I):
            return set(), set(), set()
        titles = {normalize(t) for t in re.findall(r'[“"「]([^”"」]{2,200})[”"」]', text)}
        kinds = set(re.findall(r"发明专利|实用新型|外观设计", text))
        numbers = {re.sub(r"\s", "", n).upper() for n in re.findall(
            r"\b(?:CN|US|WO|EP)\s*\d{6,}(?:[A-Z]\d?)?\b", text, re.I)}
        return titles, kinds, numbers

    for pa, pb in zip(patents(a), patents(b), strict=True):
        if pa and pb and not (pa & pb):
            return True  # Boilerplate patent headlines are not the same invention.

    def currencies(text):
        return {code for code, pattern in (("USD", r"美元|美金|\bUSD\b|\$"),
                ("CNY", r"人民币|\b(?:RMB|CNY)\b|[¥￥]"), ("EUR", r"欧元|\bEUR\b|€"))
                if re.search(pattern, text or "", re.I)}

    ca, cb = currencies(a), currencies(b)
    if ca and cb and not (ca & cb):
        return True  # No exchange-rate assumptions can turn conflicting units into facts.
    amounts_a, rounds_a = facts(a)
    amounts_b, rounds_b = facts(b)
    if amounts_a and amounts_b and not (amounts_a <= amounts_b or amounts_b <= amounts_a):
        return True
    return bool(rounds_a and rounds_b and rounds_a != rounds_b)


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except (ValueError, TypeError, AttributeError):
        return None


def close_in_time(a: str | None, b: str | None) -> bool:
    ta, tb = parse_time(a), parse_time(b)
    if ta is None or tb is None:
        return True  # undated reports rely on title similarity alone
    return abs(ta - tb) <= WINDOW


class Group:
    """A candidate event: either an existing stored event or a new one being built."""

    def __init__(self, *, category: str, titles: list[str], when: str | None,
                 event_id: str | None = None, evidence: list[str] | None = None) -> None:
        self.category = category
        self.titles = titles
        self.when = when
        self.event_id = event_id
        self.items: list[dict] = []
        self.evidence = list(evidence if evidence is not None else titles)

    def score(self, item: dict) -> float:
        incoming = _sales_key(' '.join([item['title'], item.get('summary') or '']))
        if incoming and any(_sales_key(t) == incoming for t in self.evidence):
            return 1.0
        return max((similarity(item["title"], t) for t in self.titles), default=0.0)

    def accepts(self, item: dict) -> bool:
        text = " ".join([item["title"], item.get("summary") or ""])
        incoming = _sales_key(text)
        same_sales = incoming and any(_sales_key(t) == incoming for t in self.evidence)
        ta, tb = parse_time(item_time(item)), parse_time(self.when)
        timely = close_in_time(item_time(item), self.when) or (same_sales and ta and tb and abs(ta-tb) <= SALES_WINDOW)
        return (item["category"] == self.category and timely
                and not any(conflicting(text, t) for t in self.evidence))

    def add(self, item: dict) -> None:
        self.items.append(item)
        self.titles.append(item["title"])
        self.evidence.append(" ".join([item["title"], item.get("summary") or ""]))
        incoming, current = parse_time(item_time(item)), parse_time(self.when)
        if incoming is not None and (current is None or incoming < current):
            self.when = item_time(item) or self.when


def item_time(item: dict) -> str | None:
    return item.get("published_at") or item.get("collected_at")


def plan_groups(new_items: list[dict], existing: list[dict]) -> tuple[list[Group], list[tuple[dict, Group]]]:
    """Return groups (existing ones carry event_id) and ambiguous (item, group) pairs."""
    groups = [Group(category=e["category"], when=e.get("event_at") or e.get("created_at"),
                    titles=[e["title"], *[t for t in (e.get("item_titles") or "").split("\x1f") if t]],
                    event_id=e["id"], evidence=[e["title"] + " " + (e.get("summary") or ""),
                    *[t for t in (e.get("item_evidence") or e.get("item_titles") or "").split("\x1f") if t]])
              for e in existing]
    ambiguous: list[tuple[dict, Group]] = []
    for item in sorted(new_items, key=lambda i: item_time(i) or ""):
        candidates = [(g.score(item), g) for g in groups if g.accepts(item)]
        best_score, best = max(candidates, key=lambda c: c[0], default=(0.0, None))
        if best is not None and best_score >= MERGE_SIMILARITY:
            best.add(item)
            continue
        group = Group(category=item["category"], titles=[], when=item_time(item))
        group.add(item)
        groups.append(group)
        if best is not None and best_score >= ASK_SIMILARITY:
            ambiguous.append((item, best))
    return groups, ambiguous


def summarise(group: Group) -> dict:
    lead = sorted(group.items, key=lambda i: (RANK.get(i.get("importance"), 2), item_time(i) or ""))[0]
    importance = min((i.get("importance") or "routine" for i in group.items), key=lambda v: RANK.get(v, 2))
    impact = next((i["impact"] for i in sorted(group.items, key=lambda i: RANK.get(i.get("importance"), 2))
                   if i.get("impact")), "")
    # Collected time may bound clustering, but is never evidence of occurrence.
    dated = [i['published_at'] for i in group.items if parse_time(i.get('published_at'))]
    when = min(dated, key=parse_time) if dated else None
    return {"title": lead["title"], "summary": lead.get("summary") or "", "impact": impact,
            "importance": importance, "category": lead["category"], "event_at": when}
