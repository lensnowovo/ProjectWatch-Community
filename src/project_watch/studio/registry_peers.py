"""Registry-first therapeutic peers: typed sponsors, located records, graded overlap.

ClinicalTrials.gov records are rendered field by field into one stored public
page per study; every product/fact cites one rendered intervention line. The
small descriptor vocabulary only locates source spellings. It is not a clinical
taxonomy; indication families only group quoted condition wording for display
and ranking. Grades are registry overlaps, not confirmed competition. Trial
sponsor is not proof of product ownership. Non-therapeutic projects never reach
the registry.
"""

from __future__ import annotations

import re
from contextlib import closing

from project_watch.studio.business_identity import exact_company_mention
from project_watch.studio.http import HttpError
from project_watch.studio.monitoring_contracts import (
    Alias,
    EvidenceInput,
    EvidencePart,
    FactInput,
    LocatedRef,
    ObjectInput,
)
from project_watch.studio.provenance import provenance

VERSION = 'registry-peers-v2'
RENDER_VERSION = 'ctgov-v2-render-v2'
MAX_PEERS, MAX_PRODUCTS, MAX_STUDY_REFS, MAX_REQUESTS, MAX_ANCHORS = 8, 3, 5, 4, 2
PRODUCT_TYPES = frozenset({'BIOLOGICAL', 'GENETIC', 'DRUG', 'COMBINATION_PRODUCT'})

_EDGE_L, _EDGE_R = r'(?<![A-Za-z0-9])', r'(?![A-Za-z0-9])'
MODALITIES = {
    'car-t': (_EDGE_L + r'CAR[\s-]?T' + _EDGE_R, r'chimeric antigen receptor[\s-]T' + _EDGE_R, r'嵌合抗原受体T'),
    'car-nk': (_EDGE_L + r'CAR[\s-]?NK' + _EDGE_R, r'chimeric antigen receptor[\s-]NK' + _EDGE_R),
    'tcr-t': (_EDGE_L + r'TCR[\s-]?T' + _EDGE_R,),
    'til': (_EDGE_L + r'TILs?' + _EDGE_R, r'tumou?r[\s-]infiltrating lymphocytes?', r'肿瘤浸润淋巴细胞'),
    'adc': (_EDGE_L + r'ADCs?' + _EDGE_R, r'antibody[\s-]drug conjugates?', r'抗体偶联药物'),
    'bispecific': (r'bispecific', r'双特异性抗体', r'双抗'),
    'antibody': (r'(?<![A-Za-z])antibod(?:y|ies)', _EDGE_L + r'mAbs?' + _EDGE_R, r'单克隆抗体', r'单抗', r'抗体'),
    'aav': (_EDGE_L + r'AAV\d*' + _EDGE_R,),
    'mrna': (_EDGE_L + r'mRNA' + _EDGE_R,),
    'sirna': (_EDGE_L + r'siRNA' + _EDGE_R,),
    'oncolytic-virus': (r'oncolytic (?:virus|virotherapy)', r'溶瘤病毒'),
}
# Registry phrasing per modality; ClinicalTrials.gov Essie expression terms.
REGISTRY_TERMS = {
    'car-t': ('"CAR-T"', '"CAR T"', '"chimeric antigen receptor"'),
    'car-nk': ('"CAR-NK"', '"CAR NK"'),
    'tcr-t': ('"TCR-T"', '"TCR T"'),
    'til': ('"tumor infiltrating lymphocytes"', 'TIL'),
    'adc': ('"antibody-drug conjugate"', 'ADC'),
    'bispecific': ('bispecific',),
    'antibody': ('antibody', '"monoclonal antibody"'),
    'aav': ('AAV',),
    'mrna': ('mRNA',),
    'sirna': ('siRNA',),
    'oncolytic-virus': ('"oncolytic virus"',),
}
CELL_SOURCES = {
    'autologous': ('自体', r'autologous'),
    'allogeneic': ('同种异体', '异体', r'allogeneic', '通用型', r'universal'),
}
_NAMED_TARGETS = (r'BCMA', r'GPRC5D', r'CLDN18\.2', r'Claudin[\s-]?18\.2', r'HER2', r'HER3', r'GPC3',
                  r'MSLN', r'EGFRvIII', r'EGFR', r'NKG2D', r'PSMA', r'DLL3', r'B7-H3', r'ROR1', r'TROP2',
                  r'Nectin-4', r'PD-L1', r'PD-1', r'c-Met', r'FcRH5', r'CLL1', r'MUC1', r'GD2', r'FAP')
TARGET_PATTERN = re.compile(_EDGE_L + r'(CD\d{1,3}|' + '|'.join(_NAMED_TARGETS) + r')(?![0-9A-Za-z.])', re.I)


def _target_code(surface):
    code = re.sub(r'\s|-(?=18)', '', surface).upper()
    return 'CLDN18.2' if code.startswith('CLAUDIN') else code


def _first(patterns, text):
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return match.group()
    return None


def descriptors(text):
    """Located source spellings keyed by small codes; empty when nothing is quoted."""
    text = str(text or '')
    targets = {}
    for match in TARGET_PATTERN.finditer(text):
        targets.setdefault(_target_code(match.group(1)), match.group(1))
    found = {'targets': targets, 'modalities': {}, 'cell_sources': {}}
    for key, table in (('modalities', MODALITIES), ('cell_sources', CELL_SOURCES)):
        for code, patterns in table.items():
            surface = _first(patterns, text)
            if surface:
                found[key][code] = surface
    return found


CODE_TOKEN = re.compile(r'(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*')
_GLUED_MODALITY = re.compile(r'(?<=[A-Za-z0-9])(CAR[\s-]?T|CAR[\s-]?NK|TCR[\s-]?T)$', re.I)


def anchor_terms(name):
    """Code-like product identifiers usable as exact registry lookups.

    A modality spelled directly onto a code (source typography such as
    "XYZ-1CAR-T") is separated; nothing is translated or guessed.
    """
    terms = []
    for match in CODE_TOKEN.finditer(str(name or '')):
        token = match.group()
        glued = _GLUED_MODALITY.search(token)
        token = (token[:glued.start()] if glued else token).rstrip('-')
        # Antigen names (CD19, BCMA...) describe many products; never an identifier.
        if (len(token) >= 4 and re.search(r'\d', token) and token not in terms
                and not TARGET_PATTERN.fullmatch(token)):
            terms.append(token)
    return terms[:2]


def name_descriptors(name):
    found = descriptors(name)
    for match in CODE_TOKEN.finditer(str(name or '')):
        glued = _GLUED_MODALITY.search(match.group())
        if glued:
            code = next(c for c, patterns in MODALITIES.items() if _first(patterns[:1], glued.group(1)))
            found['modalities'].setdefault(code, glued.group(1))
    return found


_W_L, _W_R = r'(?<![A-Za-z0-9])', r'(?![A-Za-z0-9])'
T_CELL = re.compile(r'T[\s-]?cell (?:acute|lymphoma|leuk)|' + _W_L + r'T[\s-]?ALL' + _W_R + r'|T[\s-]?lymphoblastic|T细胞(?:淋巴瘤|白血病|急性)', re.I)
# Versioned condition families: a quoted condition maps to zero or more families.
INDICATION_FAMILIES = {
    'lymphoma': (r'lymphoma', _W_L + r'B?[\s-]?NHL' + _W_R, r'non[\s-]?hodgkin', r'淋巴瘤'),
    'b-all': (_W_L + r'B[\s-]?ALL' + _W_R, r'acute lymphoblastic', r'acute lymphocytic', r'急性淋巴细胞白血病',
              _W_L + r'ALL' + _W_R),
    'cll': (r'chronic lymphocytic', _W_L + r'CLL' + _W_R, _W_L + r'SLL' + _W_R, r'small lymphocytic', r'慢性淋巴细胞白血病'),
    'multiple-myeloma': (r'myeloma', r'plasma[\s-]cell', r'骨髓瘤', r'浆细胞'),
    'al-amyloidosis': (r'amyloidosis', r'淀粉样变'),
    'myeloid': (r'acute myeloid', _W_L + r'AML' + _W_R, r'myelodysplastic', _W_L + r'MDS' + _W_R, r'急性髓系'),
    'autoimmune': (r'lupus', _W_L + r'SLE' + _W_R, r'myositis', r'myopathy', r'sclerosis', r'scleroderma',
                   r'vasculitis', r'sj[oö]gren', r'myasthenia', r'neuromyelitis', r'inflammatory bowel',
                   _W_L + r'IBD' + _W_R, r'colitis', _W_L + r'UC' + _W_R, r'crohn', r'arthritis', r'nephritis',
                   _W_L + r'LN' + _W_R, r'狼疮', r'肌炎', r'硬化', r'血管炎', r'肌无力', r'结肠炎', r'关节炎', r'肾炎', r'自身免疫'),
}
INDICATION_RANK = ('different', 'unknown', 'same')


def indication_families(text):
    """Families named by quoted wording; T-cell malignancies are kept apart."""
    text = str(text or '')
    if T_CELL.search(text) and not re.search(r'B[\s-]?cell|B[\s-]?ALL|myeloma', text, re.I):
        return {'t-cell-malignancy'}
    return {family for family, patterns in INDICATION_FAMILIES.items()
            if any(re.search(pattern, text, re.I) for pattern in patterns)}


def condition_families(conditions):
    return set().union(*(indication_families(c) for c in conditions)) if conditions else set()


def indication_overlap(profile, conditions):
    ours, theirs = set(profile.get('indication_families') or ()), condition_families(conditions)
    if not ours or not theirs:
        return {'overlap': 'unknown', 'shared': [], 'families': sorted(theirs)}
    shared = sorted(ours & theirs)
    return {'overlap': 'same' if shared else 'different', 'shared': shared, 'families': sorted(theirs)}


def _norm_name(value):
    return re.sub(r'[^0-9a-z㐀-鿿]', '', str(value or '').casefold())


def product_profiles(service, project_id, inputs):
    """Every eligible target product with cited descriptors, indications and anchor terms."""
    allowed = set(inputs.get('fact_ids', []))
    profiles = []
    for product_id in inputs.get('products', []):
        product = service.objects.get(project_id, product_id, include_internal=False)
        facts = [f for f in service.objects.facts(project_id, product_id, include_internal=False)
                 if f['id'] in allowed and f['predicate'] in ('use_case', 'delivery') and f['value']]
        found = descriptors('\n'.join(f['value'] for f in facts))
        for dim, values in name_descriptors(product['name']).items():
            for code, surface in values.items():
                found[dim].setdefault(code, surface)
        # Families only from quoted sentences naming this product: a company
        # pipeline overview ("血液肿瘤及自身免疫…") is not this product's indication.
        quotes = [r['quote'] for r in product['refs']] + [r['quote'] for f in facts for r in f['refs']]
        cited = [sentence for quote in quotes for sentence in re.split(r'[。；;\n]|(?<=[.!?])\s', quote)
                 if product['name'] in sentence]
        families = condition_families(cited)
        profiles.append({'product_id': product_id, 'product_name': product['name'],
                         'fact_ids': sorted(f['id'] for f in facts), **found,
                         'basis': 'cited_facts', 'anchor_terms': anchor_terms(product['name']), 'anchor_nct_ids': [],
                         'indication_families': sorted(families), 'indication_basis': ['cited_text'] if families else [],
                         'own_conditions': [], 'own_names': []})
    return profiles


def complete(profile):
    return bool(profile['targets'] and profile['modalities'])


def target_profiles(service, project_id, inputs):
    """Target products whose accepted current cited facts name a target and modality."""
    return [p for p in product_profiles(service, project_id, inputs) if complete(p)]


def _mentions(term, text):
    return re.search(r'(?<![A-Za-z0-9])' + re.escape(term) + r'(?![A-Za-z0-9])', text, re.I) is not None


_GENERIC_WORDS = re.compile(r'(?i)\b(?:cells?|injection|therapy|product|autologous|allogeneic|module|targeted|'
                            r'targeting|anti|human|derived|fully|dual|target|t)\b')


def distinctive(name):
    """A registered name that identifies one asset, not a generic description."""
    text = str(name or '')
    for found in descriptors(text).values():
        for surface in found.values():
            text = text.replace(surface, ' ')
    return len(_norm_name(_GENERIC_WORDS.sub(' ', text))) >= 4


def own_records(profile, studies, matches):
    """Rendered own-product lines from the target's own registry records.

    `matches(text)` selects interventions that are the target's product (exact
    code) or, for records whose sponsor is the target itself, share a target or
    modality. Their names exclude the same asset elsewhere; their attributed
    conditions become target indications with basis own_registry_record.
    """
    names, conditions, ncts = [], [], []
    for study in studies:
        record = render_study(study, matches)
        if not record:
            continue
        for item in record['products']:
            names.extend(n for n in item['registered_names'] if distinctive(n))
            conditions.extend(item['conditions'])
            if record['nct'] not in ncts:
                ncts.append(record['nct'])
    return names, conditions, ncts


def anchor_profile(profile, studies):
    """Fill missing descriptors only from records naming this exact product code.

    The record is about the target's own product; descriptors become retrieval
    and grading hints with basis own_registry_record, not new target facts.
    """
    targets, modalities, sources = {}, {}, {}
    def exact(text):
        return any(_mentions(term, text) for term in profile['anchor_terms'])
    for study in studies:
        for item in (_protocol(study).get('armsInterventionsModule') or {}).get('interventions') or []:
            if not isinstance(item, dict):
                continue
            names = ' '.join([_clean(item.get('name')), *(_clean(o) for o in item.get('otherNames') or [])])
            if not exact(names):
                continue
            found = descriptors(names + ' ' + _clean(item.get('description'), 400))
            targets.update({k: v for k, v in found['targets'].items() if k not in targets})
            modalities.update({k: v for k, v in found['modalities'].items() if k not in modalities})
            sources.update({k: v for k, v in found['cell_sources'].items() if k not in sources})
    names, conditions, ncts = own_records(profile, studies, exact)
    if not ncts:
        return profile
    merged = {**profile, 'anchor_nct_ids': ncts[:5]}
    for key, values in (('targets', targets), ('modalities', modalities), ('cell_sources', sources)):
        if not profile[key] and values:
            merged[key] = values
            merged['basis'] = 'own_registry_record'
    return add_own(merged, names, conditions)


def add_own(profile, names, conditions):
    families = condition_families(conditions)
    merged = {**profile, 'own_names': list(dict.fromkeys([*profile['own_names'], *names]))[:20],
              'own_conditions': list(dict.fromkeys([*profile['own_conditions'], *conditions]))[:20]}
    if families - set(profile['indication_families']):
        merged['indication_families'] = sorted(set(profile['indication_families']) | families)
    if families and 'own_registry_record' not in merged['indication_basis']:
        merged['indication_basis'] = [*merged['indication_basis'], 'own_registry_record']
    return merged


def registry_queries(profiles):
    queries = []
    for profile in profiles:
        targets = list(profile['targets'])[:2]
        terms = [t for code in profile['modalities'] for t in REGISTRY_TERMS.get(code, ())]
        if not targets or not terms:
            continue
        target = targets[0] if len(targets) == 1 else '(' + ' OR '.join(targets) + ')'
        intervention = f'{target} AND ({" OR ".join(dict.fromkeys(terms))})'
        for location in ('China', ''):
            query = {'intervention': intervention, 'location': location}
            if query not in queries:
                queries.append(query)
    return queries[:MAX_REQUESTS]


def _clean(value, limit=None):
    text = ' '.join(str(value or '').split())
    return text[:limit] if limit else text


def _protocol(study):
    proto = study.get('protocolSection') if isinstance(study, dict) else None
    return proto if isinstance(proto, dict) else {}


ALTERNATIVES = re.compile(_W_L + r'or' + _W_R + r'|或', re.I)


def grouped_alternatives(name, others):
    """One registered label covering several distinct products is not one asset."""
    if ALTERNATIVES.search(name):
        return True
    if descriptors(name)['targets']:
        return False
    sets = {frozenset(descriptors(o)['targets']) for o in others if descriptors(o)['targets']}
    return len(sets) >= 2


def render_study(study, relevant, excluded=None):
    """Deterministic field rendering, plus eligible experimental intervention lines.

    `relevant(text)` decides whether an intervention's own text shares a target
    or modality. Comparator/no-intervention arms, unrelated drugs and labels
    grouping alternative products are omitted. Conditions are attributed to a
    product for the whole study only when every eligible product shares the same
    experimental arms; otherwise only conditions its own arm text names.
    """
    proto = _protocol(study)
    ident = proto.get('identificationModule') or {}
    nct = _clean(ident.get('nctId'), 20)
    if not re.fullmatch(r'NCT\d{8}', nct):
        return None
    lead = (proto.get('sponsorCollaboratorsModule') or {}).get('leadSponsor') or {}
    sponsor, cls = _clean(lead.get('name'), 200), _clean(lead.get('class'), 30).upper()
    status_module = proto.get('statusModule') or {}
    status = _clean(status_module.get('overallStatus'), 40)
    updated = _clean((status_module.get('lastUpdatePostDateStruct') or {}).get('date'), 20)
    conditions = [c for c in (_clean(c, 100) for c in (proto.get('conditionsModule') or {}).get('conditions') or []) if c][:12]
    phases = [p for p in (_clean(p, 20) for p in (proto.get('designModule') or {}).get('phases') or []) if p][:3]
    brief, official = _clean(ident.get('briefTitle'), 500), _clean(ident.get('officialTitle'), 500)
    arms_module = proto.get('armsInterventionsModule') or {}
    arms = [a for a in arms_module.get('armGroups') or [] if isinstance(a, dict)]
    experimental = {_clean(a.get('label')): _clean(a.get('description'), 300) for a in arms
                    if _clean(a.get('type')).upper() == 'EXPERIMENTAL'}
    lines = [f'ClinicalTrials.gov {nct}；最后更新：{updated or "未登记"}', f'标题：{brief}',
             f'申办方：{sponsor}（{cls or "UNKNOWN"}）', f'登记适应症（全试验）：{"；".join(conditions) or "未登记"}']
    if official and official != brief:
        lines.insert(2, f'正式标题：{official}')
    eligible, grouped = [], 0
    for item in arms_module.get('interventions') or []:
        if not isinstance(item, dict):
            continue
        kind, name = _clean(item.get('type'), 30).upper(), _clean(item.get('name'), 200)
        labels = [_clean(label) for label in item.get('armGroupLabels') or []]
        mine = [label for label in labels if label in experimental]
        if not name or kind not in PRODUCT_TYPES or (arms and not mine):
            continue
        others = [o for o in (_clean(o, 100) for o in item.get('otherNames') or []) if o and o != name][:4]
        description = _clean(item.get('description'), 400)
        own = ' '.join([name, *others, description])
        # A small-molecule/supportive drug only counts when its own name says so;
        # descriptions of conditioning or immunosuppression mention the therapy.
        if not relevant(own if kind != 'DRUG' else ' '.join([name, *others])):
            continue
        if excluded and excluded(name, others, own):
            continue
        if grouped_alternatives(name, others):
            grouped += 1
            continue
        eligible.append({'kind': kind, 'name': name, 'others': others, 'description': description,
                         'own': own, 'arms': mine})
    shared_arms = len({tuple(sorted(e['arms'])) for e in eligible}) <= 1
    title = official or brief
    products = []
    for item in eligible:
        parts = [f'{sponsor} 试验组干预（{item["kind"]}）：{item["name"]}']
        if item['others']:
            parts.append(f'别名：{"、".join(item["others"])}')
        if item['description']:
            parts.append(f'说明：{item["description"]}')
        if len(eligible) == 1 and title:
            # One investigational product: the study title describes it.
            parts.append(f'试验标题：{title}')
            item['own'] += ' ' + title
        if shared_arms or len(conditions) <= 1:
            attributed, basis = conditions, 'study'
            parts.append(f'登记适应症：{"；".join(conditions) or "未登记"}')
        else:
            arm_text = ' '.join(f'{label} {experimental[label]}' for label in item['arms'])
            arm_families = indication_families(arm_text)
            attributed = [c for c in conditions if c.casefold() in arm_text.casefold()
                          or indication_families(c) & arm_families]
            basis = 'arm' if attributed else 'unattributed'
            parts.append('所属试验组：' + '；'.join(f'{label}：{experimental[label]}' if experimental[label] else label
                                              for label in item['arms']))
            parts.append(f'本组适应症：{"；".join(attributed)}' if attributed else '本组适应症：试验组未注明')
        parts += [f'阶段：{"/".join(phases) or "N/A"}', f'状态：{status or "未登记"}', f'登记号：{nct}']
        # Registry otherNames sometimes list conditioning drugs; only names that
        # carry an identifier or a target/modality are aliases of this product.
        aliases = [o for o in item['others'] if anchor_terms(o) or any(descriptors(o)[k] for k in ('targets', 'modalities'))]
        products.append({'name': item['name'], 'others': aliases, 'registered_names': [item['name'], *item['others']],
                         'own': item['own'],
                         'conditions': list(attributed), 'indication_basis': basis, 'line_index': len(lines)})
        lines.append('；'.join(parts))
    text = '\n'.join(lines)
    offsets, cursor = [], 0
    for line in lines:
        offsets.append((cursor, cursor + len(line)))
        cursor += len(line) + 1
    sponsor_index = next(i for i, line in enumerate(lines) if line.startswith('申办方：'))
    return {'nct': nct, 'url': f'https://clinicaltrials.gov/study/{nct}', 'sponsor': sponsor, 'class': cls,
            'conditions': conditions, 'phases': phases, 'status': status, 'text': text, 'grouped': grouped,
            'sponsor_span': offsets[sponsor_index], 'products': [{**p, 'span': offsets[p['line_index']],
                                                                  'line': lines[p['line_index']]} for p in products]}


SCHEMA = """
CREATE TABLE IF NOT EXISTS studio_registry_peers (
 project_id TEXT NOT NULL REFERENCES projects(id), sponsor_key TEXT NOT NULL,
 object_id TEXT NOT NULL REFERENCES studio_monitor_objects(id), PRIMARY KEY(project_id,sponsor_key)
);
CREATE TABLE IF NOT EXISTS studio_registry_products (
 project_id TEXT NOT NULL REFERENCES projects(id), peer_id TEXT NOT NULL, name_key TEXT NOT NULL,
 object_id TEXT NOT NULL REFERENCES studio_monitor_objects(id), PRIMARY KEY(project_id,peer_id,name_key)
);
"""


def _key(name):
    return ' '.join(name.split()).casefold()


def _is_self(sponsor, names):
    key = _key(sponsor)
    return any(key == _key(n) or exact_company_mention(n, sponsor) or exact_company_mention(sponsor, n)
               for n in names if n and len(n) >= 2)


LEVELS = ('same_target', 'same_modality', 'same_target_and_modality')
PHASES = ('NA', 'EARLY_PHASE1', 'PHASE1', 'PHASE2', 'PHASE3', 'PHASE4')


def grade(profiles, found, conditions=None):
    """Best registry overlap against any target profile; never a verdict.

    With conditions, indication overlap is ranked before target/modality so a
    same-disease programme outranks the same target in another disease area.
    """
    best = None
    for profile in profiles:
        targets = sorted(set(profile['targets']) & set(found['targets']))
        modalities = sorted(set(profile['modalities']) & set(found['modalities']))
        if not targets and not modalities:
            continue
        level = ('same_target_and_modality' if targets and modalities
                 else 'same_modality' if modalities else 'same_target')
        ours, theirs = set(profile['cell_sources']), set(found['cell_sources'])
        cell = 'unknown' if not ours or not theirs else 'same' if ours & theirs else 'different'
        indication = indication_overlap(profile, conditions or [])
        row = {'level': level, 'shared_targets': targets, 'shared_modalities': modalities,
               'cell_source': cell, 'target_product_id': profile['product_id'],
               'indication_overlap': indication['overlap'], 'shared_indication_families': indication['shared'],
               'indication_families': indication['families']}
        rank = (INDICATION_RANK.index(indication['overlap']), LEVELS.index(level))
        if best is None or rank > best[0]:
            best = (rank, row)
    return best[1] if best else None


def _rank(row):
    return (INDICATION_RANK.index(row['indication_overlap']), LEVELS.index(row['level']))


def _phase_rank(phases):
    return max((PHASES.index(p) for value in phases for p in value.split('/') if p in PHASES), default=-1)


def merge_assets(items):
    """Group one sponsor's lines that share an exact code token or normalized name.

    Source-backed aliases only: registry name/otherNames of the same records.
    """
    groups = []
    for item in items:
        names = [item['name'], *item['others']]
        # Generic aliases ("BCMA CAR-T") are shared by different programmes; the
        # same registered primary name within one sponsor is still one asset.
        keys = {_norm_name(item['name'])} | {_norm_name(n) for n in item['others'] if distinctive(n)}
        codes = {t.casefold() for n in names for t in anchor_terms(n)}
        target = next((g for g in groups if g['keys'] & keys or g['codes'] & codes), None)
        if target is None:
            target = {'name': item['name'], 'keys': set(), 'codes': set(), 'names': [], 'items': []}
            groups.append(target)
        target['keys'] |= keys
        target['codes'] |= codes
        target['names'].extend(n for n in names if n not in target['names'])
        target['items'].append(item)
    return groups


class RegistryPeers:
    name = 'clinical_trials'

    def __init__(self, store, objects, evidence):
        self.store, self.objects, self.evidence = store, objects, evidence
        with closing(store._connect()) as db, db:
            db.executescript(SCHEMA)

    def run(self, service, project_id, inputs, collectors, *, execution, guard, self_names, blocked):
        result = {'version': VERSION, 'status': 'not_applicable', 'queries': [], 'requests': 0,
                  'study_count': 0, 'excluded': {'non_industry': 0, 'self': 0, 'user_removed': 0, 'own_product': 0,
                                                 'grouped_alternatives': 0},
                  'peers': [], 'failures': [], 'target_products': [],
                  'assertion_basis': 'registry_records_not_competition_verdict'}
        if not hasattr(collectors, 'registry_studies'):
            return result
        industry_filter = {'enabled': True}
        def fetch(intervention='', location='', page_size=50, peers=False, sponsor=''):
            guard()
            filtered = peers and industry_filter['enabled']
            options = {'location': location, 'page_size': page_size}
            if filtered:
                options['industry_only'] = True
            if sponsor:
                options['sponsor'] = sponsor
            try:
                return collectors.registry_studies(intervention, **options)
            except HttpError as exc:
                if exc.code == 'request_outcome_unknown' or any(u['unknown'] for u in execution.usage().values()):
                    raise
                if filtered and exc.code == 'http_failure' and exc.status == 400:
                    # A rejected server filter is a known answer; fall back once to
                    # the unfiltered query, still checking sponsor class locally.
                    industry_filter['enabled'] = False
                    result['industry_filter'] = 'rejected'
                    return fetch(intervention, location, page_size, peers, sponsor)
                result['failures'].append('registry:' + exc.code)
                return None
            finally:
                result['requests'] += 1
        profiles = []
        anchors = 0
        for profile in product_profiles(service, project_id, inputs):
            # Missing descriptors may come from the target's own exact product
            # code in the registry; complete cited profiles never need this.
            if (profile['anchor_terms'] and (profile['targets'] or profile['modalities']) and anchors < MAX_ANCHORS
                    and (not complete(profile) or not profile['indication_families'])):
                anchors += 1
                studies = fetch(' OR '.join(f'"{t}"' for t in profile['anchor_terms']), page_size=20)
                profile = anchor_profile(profile, studies or [])
            if complete(profile):
                profiles.append(profile)
        # The target's own sponsor records (latin-script names it is known by)
        # name its registered assets and indications; they never become peers.
        sponsors = [n for n in dict.fromkeys(self_names) if n and re.search(r'[A-Za-z]{2}', n)
                    and not re.search(r'["()]', n) and not re.fullmatch(r'[\d.\s]+[A-Za-z]{1,3}', n)][:3]
        if profiles and sponsors:
            own = fetch(sponsor=' OR '.join(f'"{n}"' for n in sponsors), page_size=50) or []
            own = [s for s in own if _is_self(_clean(((_protocol(s).get('sponsorCollaboratorsModule') or {})
                                                      .get('leadSponsor') or {}).get('name')), self_names)]
            for index, profile in enumerate(profiles):
                def shares(text, profile=profile):
                    # Only the same target and modality: another own programme
                    # must not lend its indications to this product.
                    graded = grade([profile], descriptors(text))
                    return bool(graded) and graded['level'] == 'same_target_and_modality'
                names, conditions, ncts = own_records(profile, own, shares)
                if ncts:
                    profiles[index] = add_own({**profile, 'anchor_nct_ids': list(dict.fromkeys(
                        [*profile['anchor_nct_ids'], *ncts]))[:8]}, names, conditions)
        result['target_products'] = [{k: p[k] for k in ('product_id', 'product_name', 'targets', 'modalities',
            'cell_sources', 'basis', 'anchor_nct_ids', 'indication_families', 'indication_basis', 'own_conditions',
            'own_names')} for p in profiles]
        queries = registry_queries(profiles)
        if not queries:
            if result['failures']:
                result['status'] = 'failed'
            return result
        result['queries'] = queries
        own_ncts = {n for p in profiles for n in p['anchor_nct_ids']}
        own_terms = [t for p in profiles for t in p['anchor_terms']]
        own_names = {_norm_name(n) for p in profiles for n in p['own_names'] if len(_norm_name(n)) >= 4}
        own_codes = {t.casefold() for p in profiles for n in p['own_names'] for t in anchor_terms(n)}
        studies, seen = [], set()
        for query in queries:
            batch = fetch(query['intervention'], query['location'], peers=True)
            if batch is None:
                if result['failures'][-1] in ('registry:request_budget', 'registry:provider_backoff'):
                    break
                continue
            for study in batch:
                nct = _clean((_protocol(study).get('identificationModule') or {}).get('nctId'))
                if nct and nct not in seen:
                    seen.add(nct)
                    if nct in own_ncts:
                        result['excluded']['own_product'] += 1
                    else:
                        studies.append(study)
        guard()
        if result['failures'] and not studies:
            result['status'] = 'failed'
            return result
        result['study_count'] = len(studies)
        def relevant(text):
            return grade(profiles, descriptors(text)) is not None
        def own_asset(name, others, text):
            # The target's own product, possibly run by a partner or subsidiary:
            # an exact code from its own name, or a name its own records use.
            if any(_mentions(term, text) for term in own_terms) or any(
                    _norm_name(n) in own_names or {t.casefold() for t in anchor_terms(n)} & own_codes
                    for n in (name, *others)):
                result['excluded']['own_product'] += 1
                return True
            return False
        result.setdefault('industry_filter', 'applied' if industry_filter['enabled'] else 'rejected')
        candidates = {}
        for study in studies:
            record = render_study(study, relevant, own_asset)
            if not record or not record['sponsor']:
                continue
            if record['class'] != 'INDUSTRY':
                result['excluded']['non_industry'] += 1
                continue
            if _is_self(record['sponsor'], self_names):
                result['excluded']['self'] += 1
                continue
            if _key(record['sponsor']) in blocked:
                result['excluded']['user_removed'] += 1
                continue
            result['excluded']['grouped_alternatives'] += record['grouped']
            if not record['products']:
                continue
            candidates.setdefault(_key(record['sponsor']), []).append(record)
        # Rank sponsors by indication overlap, then target/modality overlap and
        # stage rather than search order; bounded slots go to the closest programmes.
        def score(records):
            rows = [grade(profiles, descriptors(item['own']), item['conditions']) for r in records for item in r['products']]
            best = max((_rank(g) for g in rows if g), default=(-1, -1))
            return (*best, _phase_rank([p for r in records for p in r['phases']]), len(records))
        ranked = sorted(candidates.items(), key=lambda kv: score(kv[1]), reverse=True)
        result['excluded']['beyond_peer_limit'] = max(0, len(ranked) - MAX_PEERS)
        peers = dict(ranked[:MAX_PEERS])
        result['peers'] = self._save(project_id, peers, profiles, execution)
        result['status'] = 'partial' if result['failures'] else 'completed'
        return result

    def _save(self, project_id, peers, profiles, execution):
        output, stored = [], {}
        # Evidence commits first: object/fact validation resolves refs on its own connection.
        for records in peers.values():
            for record in records:
                details = provenance(record['url'], record['text'], read_scope='partial')
                with closing(self.store._connect()) as db, db:
                    db.execute('BEGIN IMMEDIATE')
                    execution._guard(db)
                    stored[record['nct']] = self.evidence.save(EvidenceInput(
                        project_id=project_id, source_key='registry:ctgov:' + record['nct'], visibility='public',
                        source_type='public_page', read_scope='partial', extraction_version=RENDER_VERSION, **details,
                        parts=(EvidencePart(locator_kind='stored_text', locator=record['url'], text=record['text']),)), db=db)
        with closing(self.store._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            execution._guard(db)
            for key, records in peers.items():
                saved = [(record, stored[record['nct']]) for record in records]
                first, evidence = saved[0]
                start, end = first['sponsor_span']
                sponsor_ref = LocatedRef(evidence_version_id=evidence['id'], part_index=0, start=start, end=end,
                                         quote=first['text'][start:end])
                peer = self._object(db, project_id, 'studio_registry_peers', (project_id, key),
                    ObjectInput(kind='company', name=first['sponsor'], purpose='related', visibility='public',
                                refs=(sponsor_ref,)))
                lines = []
                for record, evidence in saved:
                    for item in record['products']:
                        start, end = item['span']
                        ref = LocatedRef(evidence_version_id=evidence['id'], part_index=0, start=start, end=end,
                                         quote=item['line'])
                        lines.append({**item, 'record': record, 'evidence': evidence, 'ref': ref})
                groups = merge_assets(lines)
                def group_rank(group):
                    graded = [grade(profiles, descriptors(i['own']), i['conditions']) for i in group['items']]
                    best = max((_rank(g) for g in graded if g), default=(-1, -1))
                    return (*best, _phase_rank([p for i in group['items'] for p in i['record']['phases']]))
                groups.sort(key=group_rank, reverse=True)
                rows = []
                for group in groups[:MAX_PRODUCTS]:
                    items = group['items'][:MAX_STUDY_REFS]
                    name_key = _key(group['name'])
                    aliases = tuple(Alias(name=n, visibility='public') for n in group['names'][1:7])
                    product = self._object(db, project_id, 'studio_registry_products', (project_id, peer['id'], name_key),
                        ObjectInput(kind='product', name=group['name'], parent_id=peer['id'], visibility='public',
                                    aliases=aliases, refs=tuple(i['ref'] for i in items)))
                    found = {'targets': {}, 'modalities': {}, 'cell_sources': {}}
                    fact_ids, conditions, trials = [], [], []
                    for item in items:
                        record, ref = item['record'], item['ref']
                        local = descriptors(item['own'])
                        for dim in found:
                            for code, surface in local[dim].items():
                                found[dim].setdefault(code, surface)
                        conditions.extend(c for c in item['conditions'] if c not in conditions)
                        trials.append({'nct': record['nct'], 'url': record['url'], 'product_name': item['name'],
                                       'phase': '/'.join(record['phases']), 'status': record['status'],
                                       'conditions': item['conditions'], 'indication_basis': item['indication_basis']})
                        values = [('registry_target', s) for s in local['targets'].values()]
                        values += [('registry_modality', s) for s in local['modalities'].values()]
                        values += [('registry_cell_source', s) for s in local['cell_sources'].values()]
                        values += [('indication', c) for c in item['conditions']]
                        if record['phases']:
                            values.append(('development_phase', '/'.join(record['phases'])))
                        if record['status']:
                            values.append(('trial_status', record['status']))
                        for predicate, value in values:
                            if not value or value not in ref.quote:
                                continue
                            fact = self.objects.save_fact(project_id, FactInput(
                                subject_id=product['id'], predicate=predicate, text=value, value=value[:100],
                                subject_attribution='explicit', basis='actual', visibility='public', refs=(ref,)), db=db)
                            fact_ids.append(fact['id'])
                    graded = grade(profiles, found, conditions)
                    if not graded:
                        continue
                    rows.append({'product_id': product['id'], 'name': product['name'], 'aliases': group['names'][1:7],
                                 **graded, 'targets': sorted(found['targets']), 'modalities': sorted(found['modalities']),
                                 'indications': conditions, 'trials': trials,
                                 'phases': list(dict.fromkeys(t['phase'] for t in trials if t['phase'])),
                                 'statuses': list(dict.fromkeys(t['status'] for t in trials if t['status'])),
                                 'nct_ids': list(dict.fromkeys(t['nct'] for t in trials)),
                                 'urls': list(dict.fromkeys(t['url'] for t in trials)),
                                 'evidence_ids': list(dict.fromkeys(i['evidence']['id'] for i in items)),
                                 'fact_ids': list(dict.fromkeys(fact_ids))})
                if rows:
                    output.append({'id': peer['id'], 'name': peer['name'], 'products': rows,
                                   'sponsor_basis': 'registry_lead_sponsor_not_verified_owner'})
        return output

    def _object(self, db, project_id, table, key, item):
        column = 'sponsor_key' if table == 'studio_registry_peers' else 'name_key'
        where = 'project_id=? AND sponsor_key=?' if column == 'sponsor_key' else 'project_id=? AND peer_id=? AND name_key=?'
        row = db.execute(f'SELECT object_id FROM {table} WHERE {where}', key).fetchone()
        if row:
            obj = self.objects.get(project_id, row[0], db=db)
            if obj['user_override']:
                return obj
            refs = tuple(dict.fromkeys([*item.refs, *(LocatedRef(**r) for r in obj['refs'])]))[:20]
            aliases = tuple(dict.fromkeys([*(Alias(**a) for a in obj['aliases']), *item.aliases]))[:30]
            update = ObjectInput.model_validate({k: obj[k] for k in ObjectInput.model_fields}).model_copy(
                update={'refs': refs, 'aliases': aliases})
            if update.model_dump(mode='json') != {k: obj[k] for k in ObjectInput.model_fields}:
                obj = self.objects.save(project_id, update, actor='automatic', object_id=obj['id'],
                                        expected_version=obj['version'], db=db)
            return obj
        obj = self.objects.save(project_id, item, actor='automatic', db=db)
        db.execute(f'INSERT INTO {table} VALUES ({",".join("?" for _ in range(len(key) + 1))})', (*key, obj['id']))
        return obj
