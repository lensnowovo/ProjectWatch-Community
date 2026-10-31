"""Query names from owner-confirmed company names. Deterministic; no model.

Owners sometimes type a note into the name field, e.g.
``Duality Biotherapeutics, Inc.（映恩生物，开曼注册主体）``. Sending that whole
string to an affiliation index returns nothing. A trailing parenthetical is
treated as an annotation only when the text before it is a complete Latin-script
name and the parenthetical is Chinese; then its comma-separated parts that look
like names are kept as additional names and notes are dropped. A Chinese legal
name's own parenthetical (``华源智因（北京）生物科技有限公司``) is never split.
"""
import re

_TRAILING = re.compile(r'^(?P<base>.*?\S)\s*[（(](?P<inner>[^（）()]*)[）)]\s*$')
_NOTE = re.compile(r'注册|主体|前身|简称|曾用|原名|下称|以下|开曼|英属|离岸|上市|控股主体|运营主体|中国区|总部')
_LATIN = re.compile(r'[A-Za-z]')
_SPLIT = re.compile(r'[，,、；;／/]')


def cjk(text):
    return any('㐀' <= ch <= '鿿' or '豈' <= ch <= '﫿' for ch in text)


def clean(name):
    """One raw name → ordered distinct query names; never invents a name."""
    name = ' '.join(str(name or '').split())
    if not name:
        return []
    match = _TRAILING.match(name)
    if match and _LATIN.search(match['base']) and not cjk(match['base']) and cjk(match['inner']):
        names = [match['base']]
        for part in _SPLIT.split(match['inner']):
            part = part.strip()
            if 2 <= len(part) <= 40 and not _NOTE.search(part):
                names.append(part)
        return list(dict.fromkeys(names))
    return [name]


def query_names(names):
    """Clean every confirmed name, keep owner order, drop duplicates."""
    result = []
    for name in names:
        result.extend(clean(name))
    return list(dict.fromkeys(result))


def latin_first(names):
    """English-language indexes: Latin-script names first, stable."""
    return sorted(names, key=cjk)


_SUFFIX = re.compile(r'[,\s]+(?:Co\.?,?\s*Ltd\.?|Ltd\.?|Limited|Inc\.?|Incorporated|LLC|L\.L\.C\.|Corp\.?|Corporation|'
                     r'GmbH|AG|S\.A\.|SAS|PLC|plc|Pte\.?\s*Ltd\.?|B\.V\.|N\.V\.|Co\.)$', re.I)


def affiliation_core(name):
    """Latin name without its trailing legal form(s): what papers actually print."""
    if cjk(name) or not _LATIN.search(name):
        return name
    core = name
    while True:
        stripped = _SUFFIX.sub('', core).rstrip(' ,')
        # "Duality Biologics (Suzhou)": papers print the location in many forms.
        stripped = re.sub(r'\s*\([A-Za-z .\-]{2,40}\)$', '', stripped).rstrip(' ,')
        if stripped == core or len(stripped) < 3:
            return core
        core = stripped


def affiliation_names(names, limit=3):
    """For OpenAlex / Europe PMC: Latin first, legal forms removed, distinct."""
    return list(dict.fromkeys(affiliation_core(n) for n in latin_first(names)))[:limit]
