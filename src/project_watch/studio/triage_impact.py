"""Bind internal premises without discarding an otherwise usable news item."""

import re

INTERNAL_PREMISE = re.compile(
    r'(?<![A-Za-z])BP(?![A-Za-z])|内部(?:资料|计划|预测|预算)|商业计划书|'
    r'(?:公司|本公司|我们)(?:的|另|还|也)?(?:收入|销量|单价|营收|现金流)?(?:计划|预测|目标|假设|预算)|'
    r'(?:收入|销量|单价|营收|现金流)(?:预测|目标|假设|预算)',
    re.I,
)
# After the located quote is removed, a second plan, target, amount or date
# must not borrow the first quote's authority. This is a conservative binding
# guard, not an additional model inference about the investment conclusion.
_EXTRA_PREMISE = re.compile(r'计划|预测|目标|预算|假设|拟|将于|将在|明年|后年|未来|\d|[百千万亿].{0,3}(?:收入|营收|元|美元|销量)')


def bound_impact(row, source, evidence):
    impact = str(row.get('impact') or '')
    citations = row.get('impact_internal_refs') or []
    if not impact:
        return row
    # A public report may itself discuss a BP. Require the internal-premise
    # sentence verbatim in that report, not just an unrelated news citation.
    internal = bool(INTERNAL_PREMISE.search(impact)) or bool(citations)
    if not internal or impact in source:
        return row
    valid = isinstance(citations, list) and bool(citations)
    remainder = impact
    for cite in citations if isinstance(citations, list) else []:
        if not isinstance(cite, dict):
            valid = False
            break
        original = evidence.get(cite.get('ref'), {})
        quote = cite.get('quote')
        if (not original.get('evidence_version_id') or not isinstance(quote, str)
                or len(quote.strip()) < 12 or quote not in original.get('quote', '') or quote not in impact):
            valid = False
            break
        # Strip a label only when it immediately introduces this exact quote.
        labelled = r'(?:内部|公司|本公司|我们|BP中)?(?:的)?(?:计划|预测|目标|预算|假设)?[“「：:"\s]*' + re.escape(quote) + r'[”」"]*'
        remainder = re.sub(labelled, '', remainder)
    if valid:
        for clause in re.split(r'[。；;\n]', remainder):
            if _EXTRA_PREMISE.search(clause) and clause.strip(' ，,：:') not in source:
                valid = False
                break
    if valid:
        return {**row, 'triage_binding': {**row.get('triage_binding', {}),
                'impact_status': 'internal_bound', 'impact_evidence': [evidence[c['ref']] for c in citations]}}
    return {**row, 'impact': '', 'triage_binding': {**row.get('triage_binding', {}),
            'impact_status': 'unsupported_internal_premise'}}
