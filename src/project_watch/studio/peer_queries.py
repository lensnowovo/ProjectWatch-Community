"""Company/industry query wording only: no private ranking or matching."""

import re

_ACTIVITY_SUFFIX = re.compile(r'(?:的)?(?:(?:研发|设计|生产|制造|开发|销售|服务|运营)[与和及、]?)+$')
_QUERY_FILLER = re.compile(r'\s*(?:市场|动态|趋势|行业|技术|新闻|进展|格局|规模|政策)\s*')


def industry_terms(label):
    """(domain head, more specific detail words) of an industry label; retrieval hints only."""
    label = str(label or '')
    if re.search(r'业$', label) and '、' in label:
        # National classification style ("计算机、通信和其他电子设备制造业"):
        # keep its listed items, dropping "其他" and the sector suffix.
        label = re.sub(r'(?:制造|服务|批发|零售)?业$', '', label)
        items = [re.sub(r'[和及与]?其他', ' ', item).strip() for item in re.split(r'[、，,]', label)]
        return '', [i for i in items if i]
    detail = re.search(r'[（(]([^）)]{2,30})[）)]', label)
    words = [w.strip() for w in re.split(r'[、，,/及]', re.sub(r'相关$', '', detail.group(1))) if w.strip()] if detail else []
    head = re.split(r'[及、（(，,/；;]| 与 ', label)[0].strip()
    return _ACTIVITY_SUFFIX.sub('', head).strip(), words


def industry_core(label):
    """Most specific noun phrase of a project's industry label, a retrieval hint only.

    A parenthetical detail ("设备制造（天线、连接器相关）") is kept with its domain head
    (R45: "连接器 线缆组件" alone found power-cable makers for an automotive supplier).
    """
    head, words = industry_terms(label)
    if not head:
        return ' '.join(words)[:30]
    return ' '.join([head, *words[:2]])[:30]


def industry_phrase(label, description='', plan=None):
    """The leads query's industry phrase. A national classification list
    ("计算机、通信和其他电子设备制造业") found telecom giants for an automotive
    connector maker (R46b); the plan's first industry query or the description's
    leading phrase is more specific."""
    head, _ = industry_terms(label)
    if head or not label:
        return industry_core(label)
    for query in (plan or {}).get('industry_queries') or []:
        words = [w for w in _QUERY_FILLER.sub(' ', str(query)).split() if w]
        if words:
            return ' '.join(words[:2])[:30]
    lead = re.split(r'[，,；;。]', str(description or ''))[0]
    if lead:
        core = industry_core(lead)
        if core:
            return core
    return industry_core(label)


def industry_word(label):
    """One discriminating domain word for disambiguating a bare company name."""
    head, words = industry_terms(label)
    if not head:
        return ''  # a national classification list is too broad to disambiguate a name
    return (words[0] if words else head)[:12]


def query_name(short, aliases, legal):
    """A searchable company name: a two-character short name ("天籁") is too generic (R45)."""
    short = ' '.join(str(short or '').split())
    if short and not (len(short) <= 2 and re.fullmatch(r'[\u4e00-\u9fff]+', short)):
        return short
    for alias in aliases or []:
        if short in alias and 3 <= len(alias) <= 12:
            return alias
    core = re.sub(r'^[\u4e00-\u9fff]{2,3}(?:省|市|自治区)', '', str(legal or ''))
    core = re.sub(r'(?:股份)?有限(?:责任)?公司$|集团$', '', core)
    return core if short in core and len(core) >= 3 else (short or str(legal or ''))
