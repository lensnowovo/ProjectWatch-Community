"""Build host collection scope from already-authorized public inputs.

No internal-plan sanitization or applicability inference happens here. The caller
must supply its public projection and evidence-backed source decisions. Missing
clinical scope is not inferred to be inapplicable from an industry label.
"""
from datetime import datetime, timedelta

from project_watch.studio.collection_plan import CAPABILITIES, MODES, make_plan

BASE_DIRECTIONS = ('company', 'competitor', 'chain', 'industry', 'policy', 'technology')


def watch_direction(watch):
    return {'competitor': 'competitor', 'supplier': 'chain', 'customer': 'chain',
            'industry': 'industry', 'policy': 'policy', 'technology': 'technology'}.get(
                watch.get('relation_type'),
                {'peers': 'competitor', 'alternatives': 'competitor', 'customer': 'chain',
                 'partners': 'chain', 'market': 'industry', 'science': 'technology',
                 'rules': 'policy'}.get(watch.get('axis'), 'company'))


def build_collection_plan(*, round_id, project, binding, public_plan, watches,
                          capabilities, at, mode, due_watch_ids=(), source_decisions=None, source_queries=()):
    """Preserve every query; executor budgets defer work rather than drop scope.

    Source names identify adapters, including search modes. Scheduled watch
    eligibility comes from the existing lifecycle service; manual refresh only
    overrides cadence for active relations, never wakes sleeping questions.
    """
    if mode not in MODES:
        raise ValueError('invalid collection mode')
    moment = datetime.fromisoformat(at)
    recent = (moment - timedelta(days=90)).isoformat()
    name = str(project.get('legal_name') or project.get('name') or '').strip()
    if not name:
        raise ValueError('confirmed company name required')
    decisions = source_decisions or {}
    due_ids = set(due_watch_ids)
    candidates = []
    scoped_sources = {r['adapter'] for r in source_queries}

    def add(source, category, queries, *, target=None, historical=False, schedule_due=True,
            query_version='1', priority=1, evidence_reason=''):
        decision = decisions.get(source, {})
        if decision:
            if decision.get('capability') not in ('not_applicable', 'disabled'):
                raise ValueError('invalid applicability decision')
            if not decision.get('reason') or not decision.get('evidence_ids'):
                raise ValueError('applicability decision requires recorded basis')
        if source in scoped_sources and target is None:
            # Model-discovered terms extend the confirmed-company baseline;
            # they cannot erase the original entity search. Equal terms share
            # the explicit scope (including a disabled/blocked scope).
            covered_queries = set()
            for row in source_queries:
                if row['adapter'] != source:
                    continue
                covered_queries.add(row['query'])
                capability = capabilities.get(source, 'not_integrated')
                if row['capability'] != 'available':
                    capability = row['capability']
                if decision:
                    capability = decision['capability']
                candidates.append(dict(source=source, category=category, target_id='scope:' + row['id'],
                    query_version='scope-v1', query=row['query'], capability=capability,
                    reason=decision.get('reason') or row['reason'],
                    applicability_evidence_ids=decision.get('evidence_ids') or [row['ref']['evidence_version_id']],
                    priority=priority, schedule_due=schedule_due,
                    window_start=None if row['historical'] else recent, window_end=at))
            queries = [q for q in queries if isinstance(q, str) and q.strip() and q.strip() not in covered_queries]
            if not queries:
                return
        capability = capabilities.get(source, 'not_integrated')
        if capability not in CAPABILITIES:
            raise ValueError('invalid source capability')
        if decision:
            capability = decision['capability']
        scoped = list(dict.fromkeys(q.strip() for q in queries if isinstance(q, str) and q.strip()))
        reason = decision.get('reason') or evidence_reason
        if capability == 'available' and not scoped:
            capability, reason = 'needs_scope', 'No confirmed query scope'
        if capability != 'available' and not reason:
            reason = 'Source capability: ' + capability
        for query in scoped or ['']:
            candidates.append(dict(source=source, category=category, target_id=target or project['id'],
                                   query_version=query_version, query=query, capability=capability,
                                   reason=reason, priority=priority, schedule_due=schedule_due,
                                   applicability_evidence_ids=decision.get('evidence_ids', []),
                                   window_start=None if historical else recent, window_end=at))

    if mode != 'topic':
        peers = public_plan.get('competitors') or []
        peer_queries = [p.get('query') or p.get('name') for p in peers if p.get('name')]
        # Discovery queries concern the confirmed company; they do not declare
        # that a named company is already a competitor, supplier or customer.
        directions = {
            'company': public_plan.get('company_queries') or [name],
            'competitor': peer_queries or [name + ' 竞争对手'],
            'chain': public_plan.get('chain_queries') or [name + ' 客户 供应商 合作'],
            'industry': public_plan.get('industry_queries') or [name + ' 行业'],
            'policy': public_plan.get('policy_queries') or [name + ' 政策'],
            'technology': public_plan.get('technology_queries') or public_plan.get('paper_queries')
                          or [name + ' 技术 研发'],
        }
        for direction in BASE_DIRECTIONS:
            if direction == 'competitor' and peer_queries:
                for peer in peers:
                    if peer.get('name'):
                        add('search', direction, [peer.get('query') or peer['name']],
                            target='peer:' + peer['name'])
            else:
                add('search', direction, directions[direction], priority=0 if direction == 'company' else 1)
        add('site', 'company', [project.get('website')], historical=True)
        add('cninfo', 'company', [name])
        add('openalex_affiliation', 'paper', public_plan.get('affiliation_names') or [name], historical=True)
        add('openalex_topic', 'paper', public_plan.get('paper_queries') or directions['technology'])
        if public_plan.get('paper_authors') or 'openalex_author' in scoped_sources:
            add('openalex_author', 'paper', public_plan.get('paper_authors') or [])
        add('patent_search', 'patent', public_plan.get('patent_assignees') or [name], historical=True)
        # is_biomed is deliberately not an applicability decision. Sponsor and
        # intervention searches remain separate, including hospital-led studies.
        sponsors, terms = public_plan.get('trial_sponsors') or [], public_plan.get('trial_terms') or []
        if sponsors or 'clinical_sponsor' in scoped_sources or (not terms and 'clinical_term' not in scoped_sources):
            add('clinical_sponsor', 'trial', sponsors, historical=True)
        if terms or 'clinical_term' in scoped_sources:
            add('clinical_term', 'trial', terms, historical=True)
        if sponsors or terms or scoped_sources & {'clinical_sponsor', 'clinical_term', 'cn_regulatory'}:
            add('cn_regulatory', 'trial', [name], historical=True)

    for watch in watches:
        if watch.get('visibility') != 'public' or watch.get('status') == 'retired':
            continue
        relation = watch.get('scope_kind') == 'relation'
        if mode == 'topic' and relation:
            continue
        eligible = watch['id'] in due_ids or (relation and mode in ('first', 'refresh'))
        # Sleeping/event-gated questions are never admitted by refresh.
        if not eligible and not relation:
            continue
        # Adapter routing retains direction independently of the stored news kind.
        direction = watch_direction(watch)
        add('search', direction, watch.get('queries') or [], target='watch:' + watch['id'],
            schedule_due=eligible, query_version='watch:' + str(watch['lifecycle_version']))

    return make_plan(round_id=round_id, project_id=project['id'], binding=binding,
                     mode=mode, candidates=candidates, at=at)
