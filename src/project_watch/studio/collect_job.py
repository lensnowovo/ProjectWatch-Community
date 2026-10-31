"""The rewritten pipeline's collection stage. Deterministic: code decides what to query.

Inputs are frozen once per job (confirmed names, products, public monitoring
watches, owner source decisions), so a restarted job repeats the same plan and
the request journal replays what already succeeded. Every source result is
stored as it arrives and is visible as a candidate at once. One failed source
records its own receipt and never stops the others. When the round ends, an
``analyze`` job is queued for the stored material; collection never waits on
analysis, and analysis never re-collects.
"""
import re
from contextlib import closing

from project_watch.studio import collection_targets, query_names
from project_watch.studio.collection_ingest import CollectionIngest
from project_watch.studio.collection_ledger import SUPPLEMENT_PREFIX, CollectionLedger
from project_watch.studio.collection_plan import make_plan
from project_watch.studio.collection_runner import run_collection
from project_watch.studio.collection_scope import build_collection_plan
from project_watch.studio.collection_sources import CollectionSources
from project_watch.studio.dsh_bootstrap import digest
from project_watch.studio.store import now

INPUT = 'collect_input_v1'
RESULT = 'collect_result_v1'
PATENTS = SUPPLEMENT_PREFIX + 'patent_documents'
MAX_PATENT_DOCUMENTS = 6
MAX_BODY_PAGES = 8
BODIES = 'collect_bodies_v1'
WATCH_FIELDS = ('id', 'visibility', 'status', 'scope_kind', 'relation_type', 'relation_target',
                'axis', 'queries', 'lifecycle_version')


def _names(pipeline, project_id, project):
    from project_watch.studio.collection_host import subject_names

    class Research:  # subject_names only needs these two attributes
        pass
    research = Research()
    research.pipeline, research.project_id = pipeline, project_id
    raw = subject_names(research) or [project.get('legal_name') or project.get('name') or '']
    if project.get('short_name') and project['short_name'] not in raw:
        raw = [*raw, project['short_name']]
    return query_names.query_names(raw)


def freeze(pipeline, execution, *, mode):
    saved = execution.read_checkpoint(INPUT)
    if saved:
        return saved
    pid = execution.job['project_id']
    project = pipeline.store.get_project(pid) or {}
    names = _names(pipeline, pid, project)
    if not names:
        raise ValueError('confirmed company name required')
    products = pipeline.objects.public_names(pid, kind='product')[:4]
    watches = [{k: w.get(k) for k in WATCH_FIELDS} for w in pipeline.monitoring_plan.items(pid)
               if w.get('visibility') == 'public' and w.get('status') != 'retired']
    relation = [w for w in watches if w['scope_kind'] == 'relation']
    public_plan = {
        'company_queries': names[:3],
        'affiliation_names': query_names.affiliation_names(names),
        'patent_index_assignees': query_names.affiliation_names(names),
        'patent_assignees': names[:3],
        'paper_queries': products,
        'trial_terms': products,
        'trial_sponsors': [n for n in query_names.latin_first(names) if not query_names.cjk(n)][:2],
        'competitors': [{'name': w['relation_target']} for w in relation
                        if w['relation_type'] == 'competitor' and w['relation_target']],
    }
    for kind, field in (('technology', 'technology_queries'), ('industry', 'industry_queries'),
                        ('policy', 'policy_queries')):
        public_plan[field] = [q for w in relation if w['relation_type'] == kind for q in w['queries'] or []]
    # Exploration quota: queries the last early-warning check proposed, used once.
    from project_watch.studio.assumptions import take_exploration
    exploration = take_exploration(pipeline.store, pid, execution.job['id'])
    if exploration:
        public_plan['industry_queries'] = list(dict.fromkeys([*public_plan['industry_queries'], *exploration]))
    due = [w['id'] for w in pipeline.monitoring_plan.due(pid, cycle=execution.job['id'])
           if w.get('visibility') == 'public']
    frozen = dict(protocol=1, mode=mode, at=now(), names=names, exploration=exploration,
                  project={'id': pid, 'legal_name': names[0], 'name': names[0], 'website': project.get('website')},
                  public_plan=public_plan, watches=watches, due_ids=due,
                  source_decisions=collection_targets.source_decisions(pipeline.store, pid))
    execution.checkpoint_chunked(INPUT, frozen)
    return frozen


def _guard(pipeline, execution):
    def guard():
        with closing(pipeline.store._connect()) as db:
            execution._guard(db)  # lease still ours
    return guard


def _patent_followups(execution, receipts_items):
    """Europe PMC patents whose applicant field names a confirmed name → one document fetch each.

    Europe PMC ids carry no kind code; the document number is id + typeCode.
    Only explicit applicant matches qualify; nothing is guessed from titles.
    """
    frozen = execution.read_checkpoint(INPUT)
    cores = [query_names.affiliation_core(n).casefold() for n in frozen['names'] if not query_names.cjk(n)]
    numbers = []
    for item in receipts_items:
        extra = item.get('extra') or {}
        applicant = (extra.get('provider_affiliation') or '').casefold()
        number = (extra.get('patent_number') or '') + (extra.get('patent_kind') or '')
        if applicant and any(c and c in applicant for c in cores) and re.fullmatch(r'[A-Z]{2}[0-9]{4,14}[A-Z][0-9]{0,2}', number):
            numbers.append(number)
    return list(dict.fromkeys(numbers))[:MAX_PATENT_DOCUMENTS]


def read_bodies(pipeline, execution, collectors):
    """Bounded article reading for this round's news: the model only picks what to read.

    Reading is collection, so it happens here and never in analysis. Without a
    model every candidate stays readable by its summary; nothing is dropped.
    """
    pid = execution.job['project_id']
    _, _, llm, _ = pipeline.tools()
    with closing(pipeline.store._connect()) as db:
        ids = {r[0] for r in db.execute('SELECT DISTINCT o.item_id FROM studio_collection_observations o '
                                        "JOIN studio_items i ON i.id=o.item_id WHERE o.job_id=? AND i.category NOT IN ('paper','patent','trial')",
                                        (execution.job['id'],))}
    if not ids or not llm.ready:
        return {'read': 0, 'candidates': len(ids)}
    project = pipeline.store.get_project(pid) or {}

    def stage(text):
        pipeline.store.set_stage(execution.job['id'], text)
    plan = execution.read_checkpoint(INPUT)['public_plan']
    eligible = pipeline.prefilter_bodies(pid, llm, project, plan, stage, item_ids=ids)
    read = pipeline.fetch_bodies(pid, stage, eligible_ids=eligible, collectors=collectors, page_limit=MAX_BODY_PAGES)
    return {'read': read, 'candidates': len(ids)}


def run(pipeline, execution, *, mode='refresh'):
    frozen = freeze(pipeline, execution, mode=mode)
    pid = execution.job['project_id']
    _, _, _, collectors = pipeline.tools()
    sources = CollectionSources(execution, collectors)
    capabilities = sources.capabilities() | {'site': 'not_integrated'}
    plan = build_collection_plan(round_id=execution.job['id'], project=frozen['project'], binding=digest(frozen),
                                 public_plan=frozen['public_plan'], watches=frozen['watches'],
                                 capabilities=capabilities, at=frozen['at'], mode=frozen['mode'],
                                 due_watch_ids=frozen['due_ids'], source_decisions=frozen['source_decisions'])
    ingest = CollectionIngest(execution, admit_materials=False)
    collected = []

    def keep(unit, items):
        saved = ingest(unit, items)
        if unit['source'] == 'europepmc_patent':
            collected.extend(items)
        return saved
    guard = _guard(pipeline, execution)
    ledger = CollectionLedger(execution, plan)
    coverage = run_collection(ledger, collect=sources.collect, ingest=keep, guard=guard)
    numbers = execution.read_checkpoint(PATENTS + ':numbers')
    if numbers is None:
        numbers = _patent_followups(execution, collected)
        execution.checkpoint(PATENTS + ':numbers', numbers)
    extra = None
    if numbers:
        candidates = [dict(source='google_patent', category='patent', target_id=pid, query_version='1', query=n,
                           capability='available', window_start=None, window_end=frozen['at'])
                      for n in numbers]
        extra_plan = make_plan(round_id=execution.job['id'], project_id=pid, binding=digest(frozen),
                               mode=frozen['mode'], candidates=candidates, at=frozen['at'])
        extra = run_collection(CollectionLedger(execution, extra_plan, stage=PATENTS), collect=sources.collect,
                               ingest=ingest, guard=guard)
    bodies = execution.read_checkpoint(BODIES)
    if bodies is None:
        bodies = read_bodies(pipeline, execution, collectors)
        execution.checkpoint(BODIES, bodies)
    rows = coverage['rows'] + (extra['rows'] if extra else [])
    outcomes = [r['outcome'] for r in rows if r['capability'] == 'available' and r['outcome'] != 'not_due']
    status = ('failed' if outcomes and all(o in ('failed', 'unknown') for o in outcomes)
              else 'complete' if all(o == 'success' for o in outcomes) else 'partial')
    result = dict(status=status, finished_at=now(), sources=len(rows),
                  outcomes={o: outcomes.count(o) for o in set(outcomes)}, patent_documents=numbers,
                  bodies_read=bodies.get('read', 0))
    execution.checkpoint(RESULT, result)
    pipeline.store.touch(pid, 'last_external_at')
    if status != 'failed':
        pipeline.store.enqueue(pid, 'analyze', input_version='collect:' + execution.job['id'])
    return result


def summary(store, job_id):
    from project_watch.studio.execution import JobExecution

    return JobExecution(store, {'id': job_id}).read_checkpoint(RESULT)

