"""Host-owned collection admission before the DSH research turn."""
import re
from datetime import date

from project_watch.studio import collection_continuation, collection_history
from project_watch.studio.collection_ingest import CollectionIngest
from project_watch.studio.collection_ledger import STAGE, SUPPLEMENTS, CollectionLedger
from project_watch.studio.collection_plan import reconcile
from project_watch.studio.collection_runner import run_collection
from project_watch.studio.collection_scope import build_collection_plan
from project_watch.studio.collection_sites import CollectionSites
from project_watch.studio.collection_sources import CollectionSources
from project_watch.studio.collection_targets import source_decisions, targets
from project_watch.studio.dsh_bootstrap import digest
from project_watch.studio.http import Http, HttpError
from project_watch.studio.store import now

ADMISSION = 'collection_admission_v1'
SCOPE = 'collection_scope_v1'
WATCH_FIELDS = ('id', 'visibility', 'status', 'scope_kind', 'relation_type', 'relation_target',
                'axis', 'queries', 'lifecycle_version')


def admit(execution, *, mode):
    """Never retroactively expand a previously started job's authorized calls."""
    saved = execution.read_checkpoint(ADMISSION)
    if saved is not None:
        return saved
    legacy = (execution.read_checkpoint(STAGE) is None and (
        bool(execution.usage()) or execution.read_checkpoint('dsh_binding') is not None
        or execution.read_checkpoint('dsh_state') is not None))
    receipt = dict(enabled=not legacy, mode=mode,
                   reason='legacy_round_without_collection_manifest' if legacy else '')
    execution.checkpoint(ADMISSION, receipt)
    return receipt


def limit_budgets(execution, mode):
    """Compatibility entry: collection scope no longer allocates request quotas."""
    return None


NAME_QUERIES = 3


def latin_first(names):
    """Stable: Latin-script names first, then names containing CJK characters."""
    def cjk(name):
        return any('㐀' <= ch <= '鿿' or '豈' <= ch <= '﫿' for ch in name)
    return sorted(names, key=cjk)


def index_names(names):
    """Search variants only: retain the confirmed original and transformation.

    Strip a terminal full-width CJK annotation only after an unambiguous Latin
    legal suffix. Never change an identity/alias or strip meaningful legal-name
    parentheses such as Acme (UK) Limited.
    """
    variants = []
    for original in names:
        match = re.fullmatch(r'([\x20-\x7e]+(?:Inc\.?|Ltd\.?|Limited|Corporation|Corp\.?|LLC))\s*（([^（）]+)）',
                             original.strip(), flags=re.I)
        query = original
        if match and re.search(r'[㐀-鿿]', match[2]):
            query = match[1].strip()
        variants.append(dict(query=query, confirmed_name=original,
                             transform='terminal_annotation' if query != original else 'identity'))
    ordered = latin_first([v['query'] for v in variants])
    return sorted(variants, key=lambda v: ordered.index(v['query']))


def subject_names(research):
    """Confirmed subject names only; internal aliases and resolver guesses are excluded.

    Current names first, then public aliases whose validity has ended (former
    names): institution and applicant indexes are historical, so older papers
    and filings carry them. Not-yet-valid aliases are never used.
    """
    objects = research.pipeline.objects
    names = list(objects.query_inputs(research.project_id)['company_names'])
    today = date.today().isoformat()
    for row in objects.page(research.project_id, include_internal=False, current_aliases_only=True):
        if row.get('purpose') != 'subject' or row['kind'] != 'company':
            continue
        names.extend(a['name'] for a in row['aliases'] if a['visibility'] == 'public' and a['valid_to']
                     and a['valid_to'] < today and (not a['valid_from'] or a['valid_from'] <= a['valid_to']))
    return list(dict.fromkeys(names))


def _watches(research):
    return [{key: watch.get(key) for key in WATCH_FIELDS}
            for watch in research.pipeline.monitoring_plan.items(research.project_id)
            if watch.get('visibility') == 'public' and watch.get('status') != 'retired']


def guard_scope(research):
    research.guard()
    frozen = research.execution.read_checkpoint(SCOPE)
    if frozen is not None:
        current = research.store.get_project(research.project_id)
        current_watches = {w['id']: w for w in _watches(research)}
        if (research.binding != frozen['settings_binding'] or current.get('website') != frozen['project']['website']
                or source_decisions(research.store, research.project_id) != frozen.get('source_decisions', {})
                or any(current_watches.get(w['id']) != w for w in frozen['watches'])
                # A withdrawn or edited confirmed name stops queries already frozen from it.
                or ('subject_names' in frozen and subject_names(research) != frozen['subject_names'])):
            raise RuntimeError('collection scope changed during round')
    current_targets = {r['id']: r for r in targets(research)}
    if any(current_targets.get(r['id']) != r for r in (frozen or {}).get('source_queries', [])):
        raise RuntimeError('professional query scope changed during round')
    for entry in research.execution.read_checkpoint(SUPPLEMENTS) or []:
        for unit in entry['plan']['units']:
            if not unit['target_id'].startswith('scope:'):
                continue
            target = current_targets.get(unit['target_id'][6:])
            if (not target or target['capability'] != 'available' or target['adapter'] != unit['source']
                    or target['query'] != unit['query']
                    or unit['applicability_evidence_ids'] != [target['ref']['evidence_version_id']]):
                raise RuntimeError('professional query scope changed during round')


def sources_for(research):
    bound = getattr(research, '_collection_binding', None)
    if not bound or bound[0] is not research.execution or bound[1] is not research.collectors:
        if getattr(research.pipeline._execution_state, 'current', None) is not research.execution:
            raise ValueError('collection requires current job execution')
        # Admission verifies construction on the job thread. The same research
        # object is then used by the serialized gateway tool thread, which has
        # no pipeline thread-local current execution. Bind exact objects, not
        # a persistent flag that another job or direct client could inherit.
        research._collection_binding = (research.execution, research.collectors)
    sources = CollectionSources(research.execution, research.collectors)
    original = sources.collectors.http

    def guarded_send(*args):
        guard_scope(research)
        return original.transport(*args)

    sources.collectors.http = Http(guarded_send)
    return sources


def restore_for_continuation(research):
    """Read saved host work before the single authorized model continuation.

    No collection or tool dispatch is admitted by this preparation. The gateway
    still consumes the existing one-shot slot, and only a valid reply clears hold.
    """
    from project_watch.studio.recovery import dsh_model_continuation

    execution = research.execution
    if not dsh_model_continuation(research.pipeline, execution):
        return None
    if (execution.read_checkpoint('dsh_hold') or {}).get('code') != 'request_outcome_unknown':
        return None
    previous = research.recovery_admission
    research.recovery_admission = True
    try:
        guard_scope(research)
    finally:
        research.recovery_admission = previous
    frozen, manifest = execution.read_checkpoint(SCOPE), execution.read_checkpoint(STAGE)
    if not frozen or not manifest:
        raise HttpError('collection_checkpoint_incomplete')
    plan = manifest['plan']
    if (plan['round_id'] != execution.job['id'] or plan['project_id'] != research.project_id
            or plan['binding'] != digest(frozen)):
        raise HttpError('collection_checkpoint_mismatch')
    result = reconcile(plan, manifest['receipts'])
    if any(row['outcome'] in ('pending', 'running', 'unknown') for row in result['rows']):
        raise HttpError('collection_checkpoint_incomplete')
    # Bind the normal guarded clients for later tools, without invoking them.
    sources_for(research)
    return result


def prepare(research, *, admit_materials=True):
    execution = research.execution
    admission = execution.read_checkpoint(ADMISSION)
    if not admission or not admission['enabled']:
        return None
    restored = restore_for_continuation(research)
    if restored is not None:
        return restored
    # pipeline.tools must have created the job's journal/cache, not the direct
    # operation client. Reject incorrect composition before any provider call.
    research.guard()
    sources = sources_for(research)
    sites = CollectionSites(research, sources)
    frozen = execution.read_checkpoint(SCOPE)
    if frozen is None:
        settings = research.pipeline.settings_store.load()
        watches = _watches(research)
        topic = execution.read_checkpoint('collection_request_v1') if admission['mode'] == 'topic' else None
        if topic is not None:
            selected = (topic.get('question') or {}).get('id')
            watches = [w for w in watches if w['id'] == selected]
            if len(watches) != 1:
                raise ValueError('selected topic scope required')
        # Only already-authorized public watch queries are reused. get_plan may
        # include internal-derived strings and is deliberately never read here.
        public_plan = {'competitors': [{'name': w['relation_target']}
                                      for w in watches if w['scope_kind'] == 'relation'
                                      and w['relation_type'] == 'competitor' and w['relation_target']]}
        for relation, field in (('technology', 'technology_queries'), ('industry', 'industry_queries'),
                                ('policy', 'policy_queries')):
            public_plan[field] = [q for w in watches if w['scope_kind'] == 'relation'
                                  and w['relation_type'] == relation for q in w['queries'] or []]
        names = None if topic is not None else subject_names(research)
        if names:
            public_plan['name_order_policy'] = 'confirmed-names-v1'
            # Owner-confirmed public company names (the subject object and its
            # current public aliases), as the legacy collector already used them.
            # Without this, institution/applicant indexes only received the legal
            # name, which English affiliation and assignee fields rarely contain.
            # OpenAlex/Europe PMC index English affiliations and applicants, so a
            # Chinese legal name there spends a request on a near-certain zero.
            # The web patent search keeps the owner's order (CN filings).
            variants = index_names(names)
            public_plan['index_name_evidence'] = variants
            queries = list(dict.fromkeys(v['query'] for v in variants))[:NAME_QUERIES]
            public_plan['affiliation_names'] = queries
            public_plan['patent_index_assignees'] = queries
            public_plan['patent_assignees'] = names[:NAME_QUERIES]
        frozen = dict(project={k: research.project.get(k) for k in ('id', 'legal_name', 'name', 'website')},
                      watches=watches, public_plan=public_plan, source_queries=[] if topic else targets(research),
                      **({'subject_names': names} if names else {}),
                      source_decisions=source_decisions(research.store, research.project_id),
                      settings_binding=research.binding,
                      history_policy=dict(overlap_days=settings.collection_overlap_days,
                                          baseline_days=settings.collection_baseline_days),
                      due_ids=([w['id'] for w in watches] if topic else [w['id'] for w in research.pipeline.monitoring_plan.due(
                          research.project_id, cycle=execution.job['id']) if w.get('visibility') == 'public']),
                      capabilities=sources.capabilities() | {'site': sites.capability}, at=now())
        execution.checkpoint_chunked(SCOPE, frozen)
    # Persist this cycle's limits before dispatch. Backlog admission happens
    # after collection, so exhausted old work cannot suppress fresh checks.
    from project_watch.studio.material_handoff import prepare as prepare_materials

    prepare_materials(research, frozen)
    manifest = execution.read_checkpoint(STAGE)
    plan = manifest['plan'] if manifest is not None else build_collection_plan(
        round_id=execution.job['id'], project=frozen['project'], binding=digest(frozen),
        public_plan=frozen['public_plan'], watches=frozen['watches'],
        capabilities=frozen['capabilities'], at=frozen['at'], mode=admission['mode'],
        due_watch_ids=frozen['due_ids'], source_queries=frozen.get('source_queries', []),
        source_decisions={adapter: dict(capability=decision['selection'], reason=decision['reason'],
                                       evidence_ids=[decision['id']])
                          for adapter, decision in frozen.get('source_decisions', {}).items()
                          if decision['selection'] != 'enabled'})
    if manifest is None:
        plan = collection_history.schedule(
            plan, research.binding, collection_history.read(research.store, research.project_id),
            continuations=collection_continuation.read(research.store, research.project_id),
            **frozen.get('history_policy', dict(overlap_days=7, baseline_days=30)))
    research.pipeline.monitoring_plan.explorations.admit_collection(execution, plan)
    ledger = CollectionLedger(execution, plan)
    from project_watch.studio.research_work_budget import plan_work

    counts = dict(search=0, open_data=0, site=0)
    for unit in plan['units']:
        if unit['capability'] != 'available' or unit['disposition'] == 'not_due':
            continue
        kind = ('search' if unit['source'] in ('search', 'patent_search', 'cn_regulatory')
                else 'site' if unit['source'] == 'site' else 'open_data')
        counts[kind] += 1 if kind == 'search' else sources.max_pages
    plan_work(execution, phase='collect', counts=counts,
              basis={'collection_plan': digest(plan), 'pagination_limit': sources.max_pages})

    def guard():
        guard_scope(research)

    guard()
    execution.checkpoint('dsh_runtime_phase', {'phase': 'collect'})
    research.store.set_stage(execution.job['id'], '收集公开来源')
    def collect(unit):
        return sites.collect(unit, guard) if unit['source'] == 'site' else sources.collect(unit)

    result = run_collection(ledger, collect=collect,
                            ingest=CollectionIngest(execution, admit_materials=admit_materials), guard=guard)
    if execution.read_checkpoint('acceptance_read_continued_v1'):
        # A corrected search string is a new, evidenced query, never a rewrite
        # of a completed zero-result query or its frozen original manifest.
        from project_watch.studio.collection_ledger import SUPPLEMENT_PREFIX
        from project_watch.studio.collection_plan import make_plan

        mapping = {v['confirmed_name']: v['query'] for v in index_names(frozen.get('subject_names', []))
                   if v['transform'] == 'terminal_annotation'}
        for unit in plan['units']:
            if (unit['source'] not in ('openalex_affiliation', 'europepmc_affiliation', 'europepmc_patent')
                    or unit['query'] not in mapping or unit['capability'] != 'available'):
                continue
            extra = make_plan(round_id=plan['round_id'], project_id=plan['project_id'],
                binding=plan['binding'], mode=plan['mode'], at=plan['created_at'], candidates=[{
                    **unit, 'query': mapping[unit['query']], 'query_version': 'index-annotation-v1'}])
            stage = SUPPLEMENT_PREFIX + extra['units'][0]['id'][:40]
            entries = execution.read_checkpoint(SUPPLEMENTS) or []
            if not any(entry['stage'] == stage for entry in entries):
                execution.checkpoint_chunked(SUPPLEMENTS, [*entries, dict(stage=stage, plan=extra,
                    name_evidence=dict(confirmed_name=unit['query'], query=mapping[unit['query']]))])
            run_collection(CollectionLedger(execution, extra, stage=stage), collect=collect,
                ingest=CollectionIngest(execution, admit_materials=admit_materials), guard=guard)
    execution.checkpoint('dsh_runtime_phase', {'phase': 'read'})
    research.task += ('\n宿主已执行本轮基础采集。先 pw_status 或 pw_context 查看 collection 与 collected_sources；'
                      '按 next_offset 分页，使用 pw_read 读取已保存证据，再决定是否补查。'
                      '需要专业来源查询时，用 pw_collect 提交来自公开证据的机构、作者、产品或技术词及引用；'
                      '它会记录查询范围并执行来源适配器，结果归属仍需阅读核对。'
                      '来源字段和结构化记录不等于网页全文；采集回执完整性由宿主对账。')
    return result
