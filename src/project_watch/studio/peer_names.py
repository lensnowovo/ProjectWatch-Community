"""Reject exact non-company placeholders, not unfamiliar company names."""

import re
import unicodedata
from datetime import UTC, date, datetime

PLACEHOLDERS = frozenset({'未知', '暂无', '无', '不详', '未披露', '待补充', '待确认', '未知公司',
                          'unknown', 'none', 'null', 'n/a', 'na', 'tbd', '-', '—', '–', '暂无竞对', '暂无竞品'})


def company_name(value) -> bool:
    if not isinstance(value, str):
        return False
    name = ''.join(unicodedata.normalize('NFKC', value).casefold().split())
    return bool(name) and name not in PLACEHOLDERS


def clean_plan(plan: dict) -> dict:
    return {**plan, 'competitors': [dict(row, name=row['name'].strip())
            for row in plan.get('competitors') or [] if isinstance(row, dict) and company_name(row.get('name'))]}


_SUFFIX = re.compile(r"(股份有限公司|有限责任公司|有限公司|集团|控股|科技|技术|股份)$")


def company_core(name: str) -> str:
    core = re.sub(r"[（(][^）)]*[）)]", "", name or "").strip()
    for _ in range(3):
        core = _SUFFIX.sub("", core)
    return core or (name or "").strip()


def headline_leads(title: str, names: list[str]) -> bool:
    """Conservative headline attribution, not a general grammatical/entity resolver."""
    head = re.sub(r"^[【\[][^】\]]{0,12}[】\]]\s*", "", title).strip()
    for name in sorted({n for n in names if n}, key=len, reverse=True):
        if not head.startswith(name):
            continue
        tail = head[len(name):]
        if re.search(r'[A-Za-z0-9]$', name) and re.match(r'[A-Za-z0-9]', tail):
            return False
        if re.match(r'\s*(?:的)?(?:客户|供应商|子公司|母公司|竞争对手|合作伙伴|控股股东|投资方)', tail):
            return False
        # A short alias must not absorb a different, longer legal entity. Stop at
        # the first action/punctuation so a company mentioned later is not rejected.
        continuation = re.split(r'[\s：:,，。]|完成|宣布|实现|发布|披露|公布|获|与|签|录得|报告', tail, maxsplit=1)[0]
        return not re.search(r'(?:股份有限公司|有限责任公司|有限公司|集团)$', continuation)
    return False


def peers_for(project: dict, plan: dict | None, relationships: dict | None, limit: int = 6, *,
              public_objects: list[dict] | None = None, today: date | None = None) -> list[dict]:
    """Planned competitors plus confirmed / supported relationship peers, minus company-wide exclusions."""
    excluded = {company_core(n) for r in (relationships or {}).get("exclusions") or [] if r.get("company_wide")
                for n in (r.get("peer_names") or [r.get("peer_name")]) if n}
    own = {company_core(project.get("name") or ""), company_core(project.get("legal_name") or "")}
    day = (today or datetime.now(UTC).date()).isoformat()
    identities = {}
    owners: dict[str, set[str]] = {}
    for obj in public_objects or []:
        if obj.get('kind') != 'company' or obj.get('visibility') != 'public':
            continue
        names = [obj['name'], *(a['name'] for a in obj.get('aliases', []) if a.get('visibility') == 'public'
            and (not a.get('valid_from') or a['valid_from'] <= day) and (not a.get('valid_to') or day <= a['valid_to']))]
        identities[obj['id']] = names
        for name in names:
            owners.setdefault(name.casefold(), set()).add(obj['id'])

    def identity_names(name):
        ids = owners.get(name.casefold(), set())
        if len(ids) != 1:
            return []
        return [n for n in identities[next(iter(ids))] if len(owners[n.casefold()]) == 1]

    def allowed(name):
        if not company_name(name):
            return False
        return not any(company_core(n) in excluded | own for n in [name, *identity_names(name)])
    out: dict[str, dict] = {}
    for row in (relationships or {}).get("items") or []:
        if row.get("origin") == "user_override" or (row.get("support") == "supported" and row.get("status") == "current"):
            name = row.get("peer_name") or ""
            core = company_core(name)
            if name and allowed(name) and core not in out:
                known_names = identities.get(row.get('peer_id'))
                aliases = [n for n in row.get("peer_names") or [] if n != name
                           and (known_names is None or n in known_names)]
                out[core] = {"name": name, "aliases": aliases,
                             "note": "已确认跟踪" if row.get("origin") == "user_override" else "有竞争依据",
                             "relation_type": row.get('relation_type') or 'unknown'}
    for c in (plan or {}).get("competitors") or []:
        name = c.get("name") or ""
        core = company_core(name)
        if name and allowed(name) and core not in out:
            out[core] = {"name": name, "aliases": [], "note": (c.get("reason") or "监控方案中的竞对")[:24],
                         "relation_type": 'unknown'}
    for peer in out.values():
        peer['aliases'] = list(dict.fromkeys([*peer['aliases'], *identity_names(peer['name'])]))
        peer['aliases'] = [n for n in peer['aliases'] if n != peer['name'] and
                           (n.casefold() not in owners or len(owners[n.casefold()]) == 1)]
    return list(out.values())[:limit]
