"""Exact scoped company names, never fuzzy identity or group consolidation."""

from __future__ import annotations

import re
from contextlib import closing, nullcontext
from datetime import date

from project_watch.studio.monitoring_contracts import Alias, LocatedRef
from project_watch.studio.normalize import normal

GENERIC_NAMES = frozenset({'公司', '本公司', '该公司', '集团', '本集团', '该集团', '企业',
                           '本企业', '该企业', '我们', 'company', 'the company', 'group', 'the group'})


def generic_name(name: str) -> bool:
    return ' '.join(name.split()).casefold() in GENERIC_NAMES


def name_key(name: str) -> str:
    """Only parentheses typography; never fuzzy spelling, brands or legal suffixes."""
    return name.translate(str.maketrans({'（': '(', '）': ')'}))


def name_pattern(name: str) -> str:
    # Regex preserves source offsets/quotes, unlike rewriting the source text.
    return ''.join('[（(]' if char in '(（' else '[）)]' if char in ')）' else re.escape(char)
                   for char in name)


def abbreviation_matches(name: str, text: str):
    pattern = name_pattern(name) + r'''\s*[（(]\s*(?:简称\s*(?:为|[:：])?|以下简称)\s*[:：]?\s*[“"‘']?([^）)”"’'\n，,;；]{2,60}?)[”"’']?\s*[）)]'''
    return (match for match in re.finditer(pattern, text) if not generic_name(match.group(1)))


def explicit_aliases(name: str, quote: str) -> tuple[Alias, ...]:
    """Only an adjacent, explicitly labelled abbreviation of this exact name."""
    names = [m.group(1).strip() for m in abbreviation_matches(name, quote)]
    return tuple(Alias(name=n, visibility='public') for n in dict.fromkeys(names) if n != name)[:6]


def current_alias_bindings(db, project_id, subject):
    """Public user mappings or current public located explicit mappings only."""
    if subject['visibility'] != 'public':
        return {}
    today = date.today().isoformat()
    bindings = {}
    for alias in subject['aliases']:
        name = alias['name']
        if (alias['visibility'] != 'public' or name == subject['name']
                or generic_name(name)
                or (alias.get('valid_from') and alias['valid_from'] > today)
                or (alias.get('valid_to') and alias['valid_to'] < today)):
            continue
        if subject['user_override']:
            bindings[name] = []
            continue
        for ref in subject['refs']:
            if name not in {a.name for a in explicit_aliases(subject['name'], ref['quote'])}:
                continue
            current = db.execute(
                "SELECT 1 FROM studio_evidence_versions e JOIN studio_evidence_heads h ON h.evidence_version_id=e.id "
                "LEFT JOIN studio_evidence_lifecycle l ON l.project_id=e.project_id AND l.evidence_version_id=e.id "
                "WHERE e.project_id=? AND e.id=? AND e.visibility='public' AND COALESCE(l.status,'current')!='withdrawn'",
                (project_id, ref['evidence_version_id'])).fetchone()
            if current:
                bindings.setdefault(name, []).append(ref)
                break  # One current exact mapping is sufficient; no all-ref dependency.
    return bindings


def part_bindings(company, bindings, part):
    names = {company: [], **bindings}
    for match in list(abbreviation_matches(company, part['text']))[:6]:
        name = match.group(1).strip()
        if name == company:
            continue
        ref = LocatedRef(evidence_version_id=part['evidence_version_id'], part_index=part['part_index'],
            start=match.start(), end=match.end(), quote=match.group()).model_dump(mode='json')
        names.setdefault(name, [ref])
    return names


def alias_validities(subject, bindings):
    today = date.today().isoformat()
    return {a['name']: {k: a.get(k) for k in ('valid_from', 'valid_to')}
            for a in subject['aliases'] if a['name'] in bindings and a['visibility'] == 'public'
            and (not a.get('valid_from') or a['valid_from'] <= today)
            and (not a.get('valid_to') or a['valid_to'] >= today)}


def bind_document_parts(company, aliases, validity, parts):
    """Share definitions across selected parts of one exact evidence version.

    Private parts have already passed provider-bound permission checks. A local
    definition overrides a user alias's expiry because it is an independent
    current source assertion, not a claim that the manual alias is still valid.
    """
    definitions = {}
    for part in parts:
        local = part_bindings(company, {}, part)
        local.pop(company, None)
        document = definitions.setdefault(part['evidence_version_id'], {})
        for name, refs in local.items():
            document.setdefault(name, refs)
    for part in parts:
        local = definitions[part['evidence_version_id']]
        part['company_names'] = {company: [], **aliases, **local}
        part['company_validity'] = {n: validity[n] for n in aliases if n in validity and n not in local}


def exact_company_mention(name, quote):
    """Avoid accepting a short name merely embedded in a larger named actor.

    This is a lexical guard, not proof of the sentence's semantic ownership;
    the extraction prompt and bilateral review retain that responsibility.
    """
    for match in re.finditer(name_pattern(name), quote):
        before, after = quote[:match.start()], quote[match.end():]
        if name[0].isascii() and name[0].isalnum() and before and re.match(r'[A-Za-z0-9_]', before[-1]):
            continue
        if name[-1].isascii() and name[-1].isalnum() and after and re.match(r'[A-Za-z0-9_]', after[0]):
            continue
        if re.match(r'\s*(?:的)?(?:集团|子公司|旗下|控股|股份|有限|科技|公司)', after):
            continue
        return True
    return False


def ownership_match(company, proposed_company, quote, names):
    same_company = name_key(proposed_company) == name_key(company)
    if not same_company and proposed_company not in names:
        return None
    candidates = [proposed_company] if not same_company else list(names)
    for name in candidates:
        if not generic_name(name) and exact_company_mention(name, quote):
            return name, [LocatedRef(**r) for r in names[name]]
    return None


def ownership_refs(company, proposed_company, quote, names):
    """None means unsupported; [] is canonical/human mapping with no extra quote."""
    matched = ownership_match(company, proposed_company, quote, names)
    return matched[1] if matched else None


def current_located_names(store, project_id, company, refs, *, include_internal, db=None):
    """Same current located mapping for extraction and downstream ownership gates.

    Only supplied product/company refs can establish a local abbreviation here;
    never borrow a name from another company's object or a generic news record.
    """
    with nullcontext(db) if db is not None else closing(store._connect()) as connection:
        if db is None:
            connection.execute('BEGIN')
        names = {company['name']: [], **current_alias_bindings(connection, project_id, company)}
        current = []
        for ref in refs:
            row = connection.execute(
                "SELECT e.visibility FROM studio_evidence_versions e JOIN studio_evidence_heads h ON h.evidence_version_id=e.id "
                "LEFT JOIN studio_evidence_lifecycle l ON l.project_id=e.project_id AND l.evidence_version_id=e.id "
                "WHERE e.project_id=? AND e.id=? AND COALESCE(l.status,'current')!='withdrawn'",
                (project_id, ref['evidence_version_id'])).fetchone()
            if not row or (not include_internal and row[0] != 'public'):
                continue
            current.append(ref)
            for alias in explicit_aliases(company['name'], ref['quote']):
                names.setdefault(alias.name, [ref])
    return names, current


def has_current_product_ownership(store, project_id, company, product, *, include_internal, db=None):
    """Shared extraction-era/legacy ownership check; reads only, no migration.

    A missing date on an old fact does not perpetuate an expired alias. An exact
    current independent document definition can still establish the ownership.
    This is the existing necessary lexical gate, not semantic verification.
    """
    names, refs = current_located_names(store, project_id, company, product['refs'],
                                      include_internal=include_internal, db=db)
    return any(normal(product['name']) in normal(r['quote'])
               and ownership_refs(company['name'], company['name'], r['quote'], names) is not None
               for r in refs)
