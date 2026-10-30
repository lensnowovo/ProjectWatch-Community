"""Versioned query hypotheses backed by public spans, not entity assertions."""
from contextlib import closing

from project_watch.studio.monitoring_contracts import LocatedRef

CONTRACT = 'evidence-query-v2'
RELATIONS = ('same_entity', 'former_name', 'brand', 'subsidiary', 'product', 'technical', 'unverified')
GUIDANCE = (
    '先阅读已有公司披露/官网，找到有连续原文支持的中文名、英文名、曾用名、品牌、子公司及产品代码。'
    'pw_collect可提交relationship与subject：两者对应的主体和query均须在同一quote原文中；'
    '这是查询假设，不是已核实关系。名称查询先用openalex_affiliation/europepmc_affiliation；'
    '同主体有据机构查询成功但零结果、或候选原始署名未命中该名称时，可按同一披露支持的产品词补一次europepmc_topic/openalex_topic，'
    '本轮最多一个产品维度。技术相关不得冒充公司署名。'
    '专利先以有据申请人名称patent_search，再从保存的公开引文取确切公开号，'
    '用google_patent获取具体记录。Google Patents是第三方索引，原申请人与当前权利人分开；'
    '申请/公开不等于授权，优先权日不等于公开日，同族不同版本不得当成独立发明。'
    '不猜别名、不按题名对应产品，不因来源失败重发或扩大预算。')


def context(*, query, subject, relationship, ref):
    if relationship not in RELATIONS or not isinstance(subject, str) or not 1 <= len(subject.strip()) <= 300:
        raise ValueError('invalid query relationship')
    ref = LocatedRef.model_validate(ref)
    if query not in ref.quote or subject.strip() not in ref.quote:
        raise ValueError('query and subject require a shared public excerpt')
    return dict(contract=CONTRACT, relationship=relationship, subject=subject.strip(),
                query=query, ref=ref.model_dump(), basis='query_hypothesis_not_verified_relationship')


def validate(store, project_id, value, *, db=None):
    if not isinstance(value, dict) or value.get('contract') != CONTRACT:
        raise ValueError('invalid query context')
    expected = context(**{k: value[k] for k in ('query', 'subject', 'relationship', 'ref')})
    if value != expected:
        raise ValueError('query context changed')
    ref = LocatedRef.model_validate(value['ref'])
    store.evidence.resolve(project_id, ref, include_internal=False)
    if db is None:
        with closing(store._connect()) as connection:
            return validate(store, project_id, value, db=connection)
    # Frozen citations remain historical evidence, but a retired name bridge
    # cannot authorize a *new* query or attribution against a newer disclosure.
    valid = db.execute("SELECT 1 FROM studio_evidence_versions e JOIN studio_evidence_heads h "
        "ON h.project_id=e.project_id AND h.source_key=e.source_key AND h.evidence_version_id=e.id "
        "WHERE e.project_id=? AND e.id=? AND e.visibility='public' AND NOT EXISTS "
        "(SELECT 1 FROM studio_evidence_lifecycle l WHERE l.project_id=e.project_id "
        "AND l.evidence_version_id=e.id AND l.status='withdrawn')", (project_id, ref.evidence_version_id)).fetchone()
    if not valid:
        raise ValueError('query evidence is no longer current and public')
    return expected


def require_product_fallback(role, args, *, claim=True):
    """One product dimension, bound to this job's same-subject name receipt.

    Literal affiliation matching is only a conservative search gate, never
    semantic acceptance. Unconfirmed candidates are retained for later review.
    claim=False only validates: the caller runs it before recording an action,
    so a refused (never dispatched) fallback is not left as a started action.
    """
    if args.get('relationship') != 'product' or args.get('adapter') not in ('openalex_topic', 'europepmc_topic'):
        return
    from project_watch.studio import collection_dsh, collection_targets, research_records

    parent = role.parent
    cited = parent.source(args.get('source_id', ''))
    quote, subject, query = args.get('quote', ''), args.get('subject'), args.get('query')
    if (not isinstance(quote, str) or not quote or quote not in cited['excerpt']
            or not isinstance(subject, str) or not subject or subject not in quote
            or not isinstance(query, str) or not query or query not in quote):
        raise ValueError('product_fallback_requires_public_relationship')
    eligible = { 'scope:' + t['id']: t for t in collection_targets.targets(parent)
        if t['capability'] == 'available' and t.get('query_context', {}).get('subject') == subject
        and t['query_context']['relationship'] in ('same_entity', 'former_name', 'brand', 'subsidiary')
        and t['ref']['evidence_version_id'] == cited['evidence_version_id'] and t['query'] in quote }
    receipts, matched = [], False
    for state in collection_dsh.rounds(parent):  # validates project, job and binding
        units = {u['id']: u for u in state['plan']['units']}
        for receipt in state['receipts']:
            unit = units[receipt['unit_id']]
            target = eligible.get(unit['target_id'])
            if (not target or unit['source'] not in ('openalex_affiliation', 'europepmc_affiliation')
                    or unit['query'] != target['query'] or receipt['outcome'] != 'success'):
                continue
            refs = receipt.get('evidence_refs', [])
            if receipt['result_count'] and not refs:
                continue  # missing material cannot be treated as a negative result
            for ref in refs:
                meta = research_records.evidence_metadata(parent.store, parent.project_id, ref['evidence_version_id'])
                if not meta or meta['kind'] != 'paper':
                    raise ValueError('affiliation receipt evidence unavailable')
                name = ' '.join(target['query'].casefold().split())
                matched |= any(name in ' '.join(a.casefold().split()) for a in meta['affiliations'])
            receipts.append(receipt['unit_id'])
    if not receipts or matched:
        raise ValueError('product_fallback_requires_unconfirmed_same_subject_affiliation')
    prior = role.execution.read_checkpoint('collection_product_dimension_v2')
    dimension = {'query': query, 'subject': subject, 'evidence_version_id': cited['evidence_version_id']}
    if prior is not None and prior != dimension:
        raise ValueError('one_product_dimension_per_collection')
    if prior is None and claim:
        role.execution.checkpoint('collection_product_dimension_v2', dimension)
