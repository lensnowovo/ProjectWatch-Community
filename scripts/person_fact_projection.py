"""Offline person-field projection experiment; NOT wired into runtime.

Source anchors and controlled wording limit what free-form model prose can add.
They do not establish semantic entailment, entity resolution or recall. Original
model fields remain in the caller's private receipt; no provider calls here.
"""
from __future__ import annotations

import re
from copy import deepcopy

from project_watch.studio.public_profile import (
    _CALENDAR,
    _calendar_value,
    _date_covers,
    _supported_occurrence_date,
)
from scripts.person_support_context import validate_person_proposal_partial

PROJECTION_SCHEMA = 'person-field-projection-v1'
RELATION_CUES = {
    'appointment': ('任命', '获任', '出任', 'appointed'),
    'departure': ('离职', '离任', '卸任', '辞任', 'resigned', 'departed'),
    'listed': ('包括', '名单', '列出', '列示', 'listed', 'include'),
    'first_role': ('首任', 'first'),
    'precedence': ('接任', '接替', 'succeeded'),
}


def _has(text, value):
    return value.casefold() in text.casefold()


def _negative_cue(quote, relation, value):
    # Bounded lexical polarity support, not ownership, irony or entailment.
    if relation == 'role':
        # A negated qualified role does not deny holding the unqualified role.
        # Only a direct negation of this value/role verb supports "未担任".
        # Other constructions remain held, not silently reclassified.
        return bool(re.search(
            r'(?:不是|并非|并不是|未|没有|不曾|从未)\s*(?:担任|出任|任职为|为|是)?\s*'
            + re.escape(value) + r'|\b(?:not|never)\s+(?:(?:an?|the)\s+)?'
            + re.escape(value) + r'\b', quote, re.IGNORECASE))
    cues = RELATION_CUES.get(relation, (value,))
    cue_pattern = '|'.join(re.escape(cue) for cue in cues)
    return bool(re.search(
        r'(?:不是|并非|并不是|未|没有|不曾|从未|\bnever\b|\bnot\b)[^，。；;!?！？]{0,8}(?:'
        + cue_pattern + r')|不(?:包括|列入|列于|接任|接替|担任)', quote, re.IGNORECASE))


def _role_anchor(quote, fact):
    """Bounded direct expressions, not a parser or a general entailment claim.

    Do not combine separated person/office anchors or let another person's
    negation govern this one. Unknown wording stays held; raw fields/quotes do
    not get repaired. Only the declared subject and bounded company shorthand
    may intervene; shorthand is not an entity-resolution guarantee.
    """
    actor = re.escape(fact['label'])
    subject = re.escape(fact['subject'])
    office = fact['value']
    first = fact['relation'] == 'first_role'
    if first and office.startswith('首任'):
        office = office[2:]
    if not office.strip():
        return False
    office = re.escape(office)
    left = r'(?<![A-Za-z0-9_])' if fact['label'].isascii() else ''
    right = r'(?=$|[\s，。；;,:：!?！？()（）]|兼)'
    cn_subject = rf'(?:(?:{subject}|本公司|公司)\s*的?\s*)?'
    calendar = r'(?:\d{4}年(?:\d{1,2}月(?:\d{1,2}日)?)?|\d{4}-\d{2}(?:-\d{2})?)'
    cn_time = rf'(?:(?:曾于\s*{calendar}|于\s*{calendar}|曾|现|时|仍|已|正式|目前|现在|此前|才)\s*)*'
    cn_qualifier = r'首任\s*' if first else r'(?:首任|唯一)?\s*'
    # Direct negation of an ordinary role cannot borrow a first/only modifier.
    negative_qualifier = r'首任\s*' if first else ''
    positive = rf'{left}{actor}\s*{cn_time}(?:担任|出任|任职为|任|是|为)\s*{cn_subject}{cn_qualifier}{office}{right}'
    negative = (
        rf'{left}{actor}\s*{cn_time}(?:(?:并不是|不是|并非)\s*|'
        rf'(?:从未|不曾|没有|并未|未)\s*(?:担任|出任|任职为|任)\s*)'
        rf'{cn_subject}{negative_qualifier}{office}{right}'
    )
    en_qualifier = r'(?:the\s+)?first\s+' if first else r'(?:(?:an?|the)\s+)?'
    en_positive = rf'{left}{actor}\s+(?:is|was|served\s+as)\s+{en_qualifier}{office}{right}'
    en_negative = rf'{left}{actor}\s+(?:is|was)\s+not\s+{en_qualifier}{office}{right}'
    polarities = set()
    for polarity, patterns in [('positive', [positive, en_positive]), ('negative', [negative, en_negative])]:
        for pattern in patterns:
            for match in re.finditer(pattern, quote, re.IGNORECASE):
                # A leading explicit denial is not an affirmative role witness.
                # Hold it rather than reclassifying the model's fields.
                if re.search(r'(?:并非|并不是|不是)\s*$', quote[:match.start()]):
                    continue
                polarities.add(polarity)
    # Direct title-name apposition, not an office found elsewhere in a clause.
    apposition = (
        rf'(?:^|[，。；;])\s*{cn_subject}{cn_qualifier}{office}\s*{actor}'
        r'(?=$|[\s，。；;,:：!?！？()（）]|卸任|离职|离任|担任|出任|在|于)'
    )
    if re.search(apposition, quote, re.IGNORECASE):
        polarities.add('positive')
    reverse = (
        rf'(?:^|[，。；;])\s*{cn_subject}(?:现任|曾任|时任)?\s*{cn_qualifier}{office}'
        rf'\s*(?:为|是)\s*{actor}{right}'
    )
    if re.search(reverse, quote, re.IGNORECASE):
        polarities.add('positive')
    # Explicit company announcement with a limited biographical insertion.
    # No arbitrary gap joining an office to a later, unrelated person.
    described = (
        rf'(?:^|[，。；;])\s*{subject}公告{office}是'
        rf'(?:服务{subject}(?:多年|\d{{1,2}}年)、)?'
        rf'(?:技术|研发|商业|运营|财务)出身的{actor}{right}'
    )
    if not first and re.search(described, quote, re.IGNORECASE):
        polarities.add('positive')
    return polarities == {fact['polarity']}


def _history_list_anchor(quote, fact):
    """One explicit history-list form with exact member boundaries."""
    subject, value = re.escape(fact['subject']), re.escape(fact['value'])
    name = r'[\u4e00-\u9fff]{1,10}?'
    pattern = (
        rf'(?:^|[，。；;])\s*{subject}'
        r'(?:进入了职业经理人的管理阶段[，,])?'
        rf'(?:已经历了|先后经历了)(?P<members>{name}(?:(?:、|和|及|与){name})+)'
        rf'(?:[一二三四五六七八九十\d]+位)?{value}(?=$|[，。；;])'
    )
    return fact['polarity'] == 'positive' and any(
        fact['label'] in re.split(r'、|和|及|与', match['members'])
        for match in re.finditer(pattern, quote)
    )


def _departure_office_anchor(quote, fact):
    """Keep a declared departure office only in a bounded actor-office clause."""
    actor, office = re.escape(fact['label']), re.escape(fact['value'])
    right = r'(?=$|[\s，。；;]|之后|职务)'
    direct = rf'{actor}(?P<neg>从未|未|没有|并未)?(?:卸任|辞任|离任){office}{right}'
    witnessed = set()
    for match in re.finditer(direct, quote):
        if re.search(r'(?:并非|不是|并不是)\s*$', quote[:match.start()]):
            continue
        witnessed.add('negative' if match['neg'] else 'positive')
    tenure = (
        rf'{actor}在{office}(?:位置上也只短暂停留了(?:半年|\d{{1,2}}年)就|'
        r'岗位工作(?:半年|\d{1,2}年)后)(?:离任|离职)'
    )
    if re.search(tenure, quote):
        witnessed.add('positive')
    return witnessed == {fact['polarity']}


def _anchors(fact):
    """Bounded relation witnesses within quotes, not a full semantic decision."""
    relation, actor, value = fact['relation'], fact['label'], fact['value']
    anchored = []
    for ref in fact['refs']:
        quote = ref['quote']
        if not _has(quote, actor):
            continue
        # Company-target departure may use source context; a declared office
        # must be present before it can survive or be rendered.
        if (relation != 'departure' or value != fact['subject']) and not _has(quote, value):
            continue
        if relation == 'departure' and value != fact['subject'] and not _departure_office_anchor(quote, fact):
            continue
        history_list = relation == 'listed' and _history_list_anchor(quote, fact)
        if (relation in RELATION_CUES and not history_list
                and not any(_has(quote, cue) for cue in RELATION_CUES[relation])):
            continue
        # Absence of a bounded negative cue is not proof of positive semantics.
        # It nevertheless prevents a declared positive field from reversing an
        # explicit local negative (and a negative from an affirmative quote).
        if history_list:
            supported = True
        elif relation in ('role', 'first_role'):
            supported = _role_anchor(quote, fact)
        else:
            supported = _negative_cue(quote, relation, value) == (fact['polarity'] == 'negative')
        if not supported:
            continue
        anchored.append(deepcopy(ref))
    if not anchored:
        raise ValueError('projection_witness')
    return anchored


def _display_date(value, refs):
    """Keep the cited spelling, without adding finer precision to display."""
    if not value:
        return ''
    wanted = _calendar_value(value)
    for ref in refs:
        for match in _CALENDAR.finditer(ref['quote']):
            if not _date_covers(wanted, _calendar_value(match[0])):
                continue
            if wanted[2] is not None or wanted[3]:
                return match[0]
            if wanted[1] is not None:
                group = 'cn_month' if match['cn_month'] else 'iso_month'
                end = match.end(group) + (1 if group == 'cn_month' else 0)
            else:
                end = match.end('year') + (1 if '年' in match[0] else 0)
            return ref['quote'][match.start():end]
    raise ValueError('projection_display_date')


def _render(fact, refs):
    actor, value, relation = fact['label'], fact['value'], fact['relation']
    negative = fact['polarity'] == 'negative'
    timing = _display_date(fact['date'], refs)
    if relation == 'listed':
        statement = f'{actor}{"未列于" if negative else "列于"}{value}名单'
    elif relation == 'role':
        verb = '未担任' if negative else '曾任' if fact['time_scope'] == 'historical' else '担任'
        if not negative and value in ('创始人', '联合创始人'):
            statement = f'{"历史资料记载，" if fact["time_scope"] == "historical" else ""}{actor}为{value}'
        else:
            statement = f'{actor}{verb}{value}'
    elif relation == 'first_role':
        office = value if value.startswith('首任') else '首任' + value
        statement = f'{actor}{"不是" if negative else "为"}{office}'
    elif relation == 'appointment':
        formal = any(_has(ref['quote'], cue) for ref in refs for cue in ('任命', '获任', 'appointed'))
        verb = ('未被任命为' if negative else '被任命为') if formal else ('未出任' if negative else '出任')
        statement = f'{actor}{"在" + timing if timing else ""}{verb}{value}'
    elif relation == 'departure':
        statement = f'{actor}{"在" + timing if timing else ""}{"未离任" if negative else "已离任"}'
        if value != fact['subject']:
            statement += value + '职务'
    else:  # precedence: value is an explicitly named predecessor, not prose.
        statement = f'{actor}{"未接任" if negative else "接任"}{value}的相关工作'
    if timing and relation not in ('appointment', 'departure'):
        statement = f'{timing}：{statement}'
    prefix = f"据{fact['reported_on']}报道，" if fact['reported_on'] else '日期不明的来源列示，'
    return prefix + statement + '。'


def project_person_facts(context: dict, proposal: dict) -> dict:
    """Project valid declared fields; keep original prose only in raw receipts."""
    checked = validate_person_proposal_partial(context, proposal)
    sources = {source['ref']: source for source in context['sources']}
    projected, rejections = [], deepcopy(checked['rejections'])
    for fact in checked['facts']:
        try:
            refs = _anchors(fact)
            if fact['date'] and not _supported_occurrence_date(fact['date'], [r['quote'] for r in refs]):
                raise ValueError('projection_occurrence')
            if fact['reported_on'] and fact['reported_on'] not in {
                sources[r['ref']]['publication']['date'] for r in refs
            }:
                raise ValueError('projection_reporting')
            text = _render(fact, refs)
        except ValueError as error:
            index = int(fact['id'][1:]) - 1
            rejections.append({'id': fact['id'], 'code': str(error),
                               'raw': deepcopy(checked['raw_facts'][index])})
        else:
            projected.append({**fact, 'text': text, 'refs': refs,
                              'projection_schema': PROJECTION_SCHEMA,
                              'basis': 'declared_fields_with_quote_anchors'})
    rejections.sort(key=lambda rejection: rejection['id'])
    status = ('partial' if projected else 'rejected') if rejections else ('complete' if projected else 'empty')
    return {**checked, 'facts': projected, 'rejections': rejections, 'status': status,
            'projection_schema': PROJECTION_SCHEMA,
            'semantic_verification': 'not_proven', 'recall_verification': 'not_proven'}
