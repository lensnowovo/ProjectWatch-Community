"""Versioned query hypotheses backed by public spans, not entity assertions."""
from contextlib import closing

from project_watch.studio.monitoring_contracts import LocatedRef

CONTRACT = 'evidence-query-v2'
RELATIONS = ('same_entity', 'former_name', 'brand', 'subsidiary', 'product', 'technical', 'unverified')
GUIDANCE = (
    'prior_professional_queries是已保存的查询回执，不重复已执行查询；成功零结果只说明该检索词未命中。'
    '优先从原文发现研发机构/申请人的具体名称，不能用上市主体名无结果代替研发机构检索。'
    '每次pw_collect必须携带刚阅读的source_id和连续quote，不能只给名称；引用来源不确定时先pw_read。'
    '宿主返回remaining_name_queries时，先补齐这些已公开名称在可用索引中的缺失查询，再结束采集。'
    '先阅读已有公司披露/官网，找到有连续原文支持的中文名、英文名、曾用名、品牌、子公司及产品代码。'
    'pw_collect可提交relationship与subject：两者对应的主体和query均须在同一quote原文中；'
    '这是查询假设，不是已核实关系。名称查询先用openalex_affiliation/europepmc_affiliation；'
    '同主体有据机构查询成功但零结果、或候选原始署名未命中该名称时，可按同一披露支持的产品词补一次europepmc_topic/openalex_topic，'
    '本轮最多一个产品维度。技术相关不得冒充公司署名。'
    '专利先以有据申请人名称patent_search，再从保存的公开引文取确切公开号，'
    '用google_patent获取具体记录。Google Patents是第三方索引，原申请人与当前权利人分开；'
    '申请/公开不等于授权，优先权日不等于公开日，同族不同版本不得当成独立发明。'
    '不猜别名、不按题名对应产品，不因来源失败重发或扩大预算。')


def _attempted(role):
    from project_watch.studio import collection_dsh

    attempted = set()
    for ledger in collection_dsh.rounds(role.parent):
        receipts = {r['unit_id'] for r in ledger['receipts']}
        attempted.update((u['source'], ' '.join(u['query'].casefold().split()))
                         for u in ledger['plan']['units'] if u['id'] in receipts)
    handoff = role.execution.read_checkpoint('update_handoff_v1') or {}
    attempted.update((r['adapter'], ' '.join(r['query'].casefold().split()))
                     for r in handoff.get('professional_query_history', []))
    for action in role.data['actions'].values():
        operation = action['operation']
        if operation['tool'] == 'pw_collect':
            args = operation['args']
            attempted.add((args['adapter'], ' '.join(args['query'].casefold().split())))
    return attempted


def remaining_name_queries(role):
    """Expose unqueried routes for public organization-name hypotheses.

    A successful Chinese query says nothing about its published English name.
    Do not translate/invent names or retry any received/failed/unknown query.
    The model still submits each normal evidence-bound tool call explicitly.
    """
    from project_watch.studio import collection_dsh, collection_targets
    from project_watch.studio.collection_host import SCOPE, latin_first

    scope = role.execution.read_checkpoint(SCOPE) or {}
    adapters = ('openalex_affiliation', 'europepmc_affiliation', 'patent_search')
    available = {a for a in adapters if scope.get('capabilities', {}).get(a) == 'available'
                 and scope.get('source_decisions', {}).get(a, {}).get('selection', 'enabled') == 'enabled'}
    if not available:
        return []
    def key(adapter, query):
        return adapter, ' '.join(query.casefold().split())
    attempted = _attempted(role)
    refs = collection_dsh._refs(role.parent)
    candidates = []
    targets = collection_targets.targets(role.parent)
    blocked = {key(t['adapter'], t['query']) for t in targets if t['capability'] != 'available'}
    for target in targets:
        ctx = target.get('query_context') or {}
        if (target['adapter'] not in adapters or target['capability'] != 'available'
                or ctx.get('relationship') not in ('same_entity', 'former_name', 'brand', 'subsidiary')):
            continue
        ref = target['ref']
        sid = next((sid for sid, links in refs.items()
                    if any(r['evidence_version_id'] == ref['evidence_version_id'] for r in links)), None)
        if sid is None:
            continue  # Do not manufacture a source ID or silently widen evidence.
        for adapter in adapters:
            identity = key(adapter, target['query'])
            if adapter in available and identity not in attempted and identity not in blocked:
                attempted.add(identity)
                candidates.append(dict(adapter=adapter, query=target['query'], source_id=sid,
                    quote=ref['quote'], subject=ctx['subject'], relationship=ctx['relationship'],
                    reason='已公开机构名称尚未在该可用索引查询；仅检索假设，不是归属结论'))
    order = latin_first([c['query'] for c in candidates])
    return sorted(candidates, key=lambda c: order.index(c['query']))


def remaining_patent_documents(role):
    """A bounded exact-record follow-through for saved patent search links.

    At most three distinct publication numbers from the existing public
    catalog, never a guessed identifier or an applicant assertion. The fixed
    shortlist is persisted before querying so finish checks cannot fan out.
    """
    from urllib.parse import urlsplit

    from project_watch.studio import collection_dsh
    from project_watch.studio.collection_host import SCOPE
    from project_watch.studio.google_patents import publication_number

    scope = role.execution.read_checkpoint(SCOPE) or {}
    if (scope.get('capabilities', {}).get('google_patent') != 'available'
            or scope.get('source_decisions', {}).get('google_patent', {}).get('selection', 'enabled') != 'enabled'):
        return []
    selected = role.execution.read_checkpoint('collection_patent_shortlist_v1')
    refs = collection_dsh._refs(role.parent)
    if selected is None:
        candidates = {}
        for sid, links in refs.items():
            if not any(r.get('direction') == 'patent' for r in links):
                continue
            source = collection_dsh.source(role.parent, sid)
            url = urlsplit(source['url'])
            parts = url.path.strip('/').split('/')
            if url.scheme != 'https' or url.hostname != 'patents.google.com' or len(parts) < 2 or parts[0] != 'patent':
                continue
            try:
                number = publication_number(parts[1])
            except ValueError:
                continue
            if number not in source['excerpt'] or not role.available(source):
                continue
            candidates.setdefault(number, dict(adapter='google_patent', query=number, quote=number,
                source_id=sid, reason='读取已保存检索结果的确切专利记录，核对原申请人与公开状态；不推定公司归属'))
            if len(candidates) == 3:
                break
        selected = list(candidates.values())
        # Empty catalog is not a frozen shortlist: later queries may supply it.
        if selected:
            role.execution.checkpoint_chunked('collection_patent_shortlist_v1', selected)
    attempted = _attempted(role)
    return [q for q in selected if ('google_patent', q['query'].casefold()) not in attempted
            and q['source_id'] in refs and role.available(collection_dsh.source(role.parent, q['source_id']))]


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
    """One product dimension, bound to a verified same-subject name receipt.

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
    # The base plan's owner-confirmed subject names count too, when the same
    # public excerpt names that confirmed name, the subject and the product.
    from project_watch.studio.collection_host import SCOPE

    confirmed = set((parent.execution.read_checkpoint(SCOPE) or {}).get('subject_names') or [])
    receipts, matched = [], False
    pairs = [(u, r) for state in collection_dsh.rounds(parent) for r in state['receipts']
             for u in state['plan']['units'] if u['id'] == r['unit_id']]
    pairs.extend((u, r) for _entry, u, r in collection_dsh.historical_queries(parent))
    for unit, receipt in pairs:
        target = eligible.get(unit['target_id'])
        if target is None and unit['target_id'] == parent.project_id and unit['query'] in confirmed and unit['query'] in quote:
            target = {'query': unit['query']}
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
