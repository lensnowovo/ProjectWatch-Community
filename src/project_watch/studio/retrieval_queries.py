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
