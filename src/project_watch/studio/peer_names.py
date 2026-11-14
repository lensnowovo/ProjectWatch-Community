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


# Product codes such as YL201, HS-20093, MHB-088C, BL-B01D1, I-DXd.
_CODE = re.compile(r'(?<![A-Za-z0-9-])(?:[A-Z]{1,6}(?:-[A-Z]{1,3})?-?\d{2,6}(?:[A-Za-z]\d{0,2})?'
                   r'|[A-Z]-[A-Z]{1,3}[a-z]{1,2})(?![A-Za-z0-9-])')
# Targets and mutations name a mechanism many companies share, not one company's product.
_TARGET = re.compile(r'(?:CD|IL|CLDN|PD|HER|EGFR|FGFR|KRAS|NRAS|TROP|B7|CTLA|LAG|TIM|VEGFR?|BCMA|GPRC|DLL|CCR|'
                     r'CXCR|TNF|IFN|TGF|JAK|BTK|CDK|PARP|ALK|ROS|RET|FLT|BCL|HDAC|ERBB|MUC|TSLP|GPC|CEACAM|'
                     r'NECTIN|FR|ROR|SSTR|PSMA|KIT)-?\d+[A-Za-z]?|[A-Z]\d{1,4}[A-Z]', re.I)


def _target(value: str) -> bool:
    return bool(_TARGET.fullmatch(value.strip()))


def peer_codes(name: str) -> list[str]:
    """Product codes written in a drafted competitor's parenthetical note."""
    notes = ' '.join(re.findall(r"[（(]([^）)]*)[）)]", name or ""))
    codes = [c for c in _CODE.findall(notes) if not _target(c)]
    # Headlines write MHB-088C as MHB088C too; I-DXd without its hyphen is no code.
    joined = [c.replace('-', '') for c in codes if '-' in c and re.search(r'\d', c)]
    return list(dict.fromkeys([*codes, *joined]))


def usable_alias(alias) -> bool:
    """A name that can identify one company in a headline, not a category word."""
    if not isinstance(alias, str):
        return False
    alias = alias.strip()
    if not 2 <= len(alias) <= 40 or re.search(r'[（()）:：,，;；]', alias) or not company_name(alias):
        return False
    # ADC, GSK, MSD: too short to tell one company apart; CD20, G12C: a shared target
    return not re.fullmatch(r'[A-Za-z]{1,3}', alias) and not _target(alias)


_LATIN_SUFFIX = re.compile(r'\s+(?:Pharma|Pharmaceuticals?|Therapeutics|Medicine|Biologics|Biotech(?:nology)?|'
                           r'Biosciences?|Bio|Holdings?|Group|Inc\.?|Co\.?|Ltd\.?|Limited|Corp(?:oration)?\.?)$', re.I)


def brand_variants(alias: str) -> list[str]:
    """'Hansoh Pharma' also as 'Hansoh': headlines drop the corporate word."""
    short = alias
    for _ in range(2):
        short = _LATIN_SUFFIX.sub('', short).strip()
    return [alias, short] if short != alias and re.fullmatch(r'[A-Za-z][A-Za-z .&-]{3,}', short) else [alias]


def peer_search_query(peer: dict) -> str:
    """Short name plus one own product code: a long drafted description is a poor news query."""
    if isinstance(peer.get('query'), str) and peer['query'].strip():
        return peer['query'].strip()
    name = peer.get('name') or ''
    short = re.split(r'\s*[/／]\s*', company_core(name))[0]
    codes = peer_codes(name) or [a for a in peer.get('aliases') or [] if _CODE.fullmatch(a or '')]
    return ' '.join(filter(None, [short, codes[0] if codes else ''])) or name


def headline_leads(title: str, names: list[str]) -> bool:
    """Conservative headline attribution, not a general grammatical/entity resolver."""
    head = re.sub(r"^[【\[][^】\]]{0,12}[】\]]\s*", "", title).strip()
    for name in sorted({n for n in names if n}, key=len, reverse=True):
        # Latin names lead case-insensitively ("Ifinatamab …" for ifinatamab).
        if head[:len(name)].casefold() != name.casefold():
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

    core_owners: dict[str, set[str]] = {}
    for oid, names in identities.items():
        for name in names:
            core_owners.setdefault(company_core(name).casefold(), set()).add(oid)

    def key_for(name, oid=None):
        if oid in identities:
            return 'id:' + oid
        ids = owners.get(name.casefold()) or core_owners.get(company_core(name).casefold(), set())
        if len(ids) == 1:
            return 'id:' + next(iter(ids))
        return 'name:' + company_core(name).casefold()

    out: dict[str, dict] = {}
    for row in (relationships or {}).get("items") or []:
        if row.get("origin") == "user_override" or (row.get("support") == "supported" and row.get("status") == "current"):
            name = row.get("peer_name") or ""
            core = key_for(name, row.get('peer_id'))
            if name and allowed(name) and core not in out:
                known_names = identities.get(row.get('peer_id'))
                aliases = [n for n in row.get("peer_names") or [] if n != name
                           and (known_names is None or n in known_names)]
                out[core] = {"name": name, "aliases": aliases,
                             "note": "已确认跟踪" if row.get("origin") == "user_override" else "有竞争依据",
                             "relation_type": row.get('relation_type') or 'unknown'}
    for c in (plan or {}).get("competitors") or []:
        name = c.get("name") or ""
        core = key_for(name)
        # Codes from the drafted note and recorded names (English, traditional, own products).
        drafted = [*peer_codes(name), *(a for a in c.get("aliases") or [] if usable_alias(a))]
        if name and allowed(name) and core not in out:
            out[core] = {"name": name, "aliases": drafted, "note": (c.get("reason") or "监控方案中的竞对")[:24],
                         "relation_type": 'unknown'}
        elif name and allowed(name) and core in out:
            out[core]['aliases'].extend([name, *drafted] if name != out[core]['name'] else drafted)
    confirmed = {}
    for key, peer in out.items():
        confirmed[key] = {n.casefold() for n in identity_names(peer['name'])}
        peer['aliases'] = list(dict.fromkeys([*peer['aliases'], *identity_names(peer['name'])]))
        peer['aliases'] = [n for n in peer['aliases'] if n != peer['name'] and
                           (n.casefold() not in owners or len(owners[n.casefold()]) == 1)]
    # A drafted name two peers share, or one naming the company itself, identifies nobody.
    # Names from a public identity were already kept to one owner and stay.
    counts: dict[str, int] = {}
    for peer in out.values():
        for n in {a.casefold() for a in peer['aliases']}:
            counts[n] = counts.get(n, 0) + 1
    own_names = {n.casefold() for n in own | {project.get(k) or '' for k in ('name', 'legal_name', 'short_name')}
                 if len(n) >= 2}

    def names_own(alias):
        return any(alias in n or n in alias for n in own_names)

    for key, peer in out.items():
        peer['aliases'] = [n for n in peer['aliases'] if n.casefold() in confirmed[key]
                           or (counts[n.casefold()] == 1 and not names_own(n.casefold()))]
    return list(out.values())[:limit]
