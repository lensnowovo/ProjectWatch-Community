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
from datetime import date

from project_watch.studio import collection_targets, query_names
from project_watch.studio.collection_ingest import CollectionIngest
from project_watch.studio.collection_ledger import SUPPLEMENT_PREFIX, CollectionLedger
from project_watch.studio.collection_plan import make_plan
from project_watch.studio.collection_runner import run_collection
from project_watch.studio.collection_scope import build_collection_plan
from project_watch.studio.collection_sources import CollectionSources
from project_watch.studio.store import digest, now

INPUT = 'collect_input_v1'
RESULT = 'collect_result_v1'
PATENTS = SUPPLEMENT_PREFIX + 'patent_documents'
MAX_PATENT_DOCUMENTS = 6
BODIES = 'collect_bodies_v1'
WATCH_FIELDS = ('id', 'visibility', 'status', 'scope_kind', 'relation_type', 'relation_target',
                'axis', 'queries', 'lifecycle_version')


def subject_names(pipeline, project_id):
    """Confirmed subject names only; internal aliases and resolver guesses are excluded.

    Current names first, then public aliases whose validity has ended (former
    names): institution and applicant indexes are historical, so older papers
    and filings carry them. Not-yet-valid aliases are never used.
    """
    objects = pipeline.objects
    names = list(objects.query_inputs(project_id)['company_names'])
    today = date.today().isoformat()
    for row in objects.page(project_id, include_internal=False, current_aliases_only=True):
        if row.get('purpose') != 'subject' or row['kind'] != 'company':
            continue
        names.extend(a['name'] for a in row['aliases'] if a['visibility'] == 'public' and a['valid_to']
                     and a['valid_to'] < today and (not a['valid_from'] or a['valid_from'] <= a['valid_to']))
    return list(dict.fromkeys(names))


def _names(pipeline, project_id, project):
    raw = subject_names(pipeline, project_id) or [project.get('legal_name') or project.get('name') or '']
    if project.get('short_name') and project['short_name'] not in raw:
        raw = [*raw, project['short_name']]
    return query_names.query_names(raw)


PLAN_FIELDS = ('industry_queries', 'policy_queries', 'paper_queries', 'paper_authors', 'trial_terms', 'trial_sponsors')


def _stored_plan(pipeline, pid, project):
    """The project's public plan; the model drafts it once when none is stored yet.

    AI decides what to watch, once, as data: competitors, industry and policy
    queries, paper topics. Later changes come from the scope review after each
    analysis. A model failure leaves the round on confirmed names and watches.
    """
    from project_watch.studio.http import HttpError
    from project_watch.studio.llm import ModelError

    stored = pipeline.store.get_plan(pid) or {}
    if 'competitors' in stored:
        return _current_paper_topics(pipeline, pid, project, stored)
    _, _, llm, _ = pipeline.tools()
    if not llm.ready:
        return stored
    try:
        drafted = pipeline.build_plan(llm, {**project, 'id': pid})
    except (ModelError, HttpError, ConnectionError, TimeoutError) as exc:
        # An unclear plan request stays recorded (never resent in this job); the round goes on.
        if getattr(exc, 'code', '') in ('project_paused', 'project_not_consented'):
            raise
        return stored
    plan = {**stored, **drafted, 'drafted_at': now(), 'paper_topics_version': PAPER_TOPICS_VERSION}
    pipeline.store.put_plan(pid, plan)
    return plan


PAPER_TOPICS_VERSION = 3  # research topics an English index can match (openalex_query.topic_terms)


def _current_paper_topics(pipeline, pid, project, stored):
    """Once per plan: research topics when the plan has none that pass the topic rules.

    Product names are searched too (codes as exact phrases), but they are not
    research topics. The old list stays for audit.
    """
    from project_watch.studio.http import HttpError
    from project_watch.studio.llm import ModelError
    from project_watch.studio.openalex_query import topic_terms

    if stored.get('paper_topics_version') == PAPER_TOPICS_VERSION:
        return stored
    old = [q for q in stored.get('paper_queries') or [] if isinstance(q, str)]
    marked = {**stored, 'paper_topics_version': PAPER_TOPICS_VERSION}
    if old and all(topic_terms(q) for q in old):
        pipeline.store.put_plan(pid, marked)  # nothing to rewrite, no model request
        return marked
    products = pipeline.objects.public_names(pid, kind='product')[:6]
    _, _, llm, _ = pipeline.tools()
    if not llm.ready:
        return stored
    try:
        topics = pipeline.redraft_paper_topics(llm, {**project, 'id': pid}, [*old, *products])
    except (ModelError, HttpError, ConnectionError, TimeoutError) as exc:
        # Recorded like any request; a later round may ask again, this job never resends.
        if getattr(exc, 'code', '') in ('project_paused', 'project_not_consented'):
            raise
        return stored
    plan = {**marked, 'paper_queries': topics, 'paper_queries_previous': old, 'paper_topics_redrafted_at': now()}
    pipeline.store.put_plan(pid, plan)
    return plan


def _merge_stored_plan(public_plan, stored):
    """Watch-derived entries first, then the stored plan; confirmed names stay the company queries."""
    names = {c['name'] for c in public_plan['competitors']}
    public_plan['competitors'] = [*public_plan['competitors'], *[
        {k: c[k] for k in ('name', 'query') if c.get(k)} for c in stored.get('competitors') or []
        if isinstance(c, dict) and c.get('name') and c['name'] not in names]][:8]
    for field in PLAN_FIELDS:
        merged = [*public_plan.get(field, []), *[q for q in stored.get(field) or [] if isinstance(q, str)]]
        public_plan[field] = list(dict.fromkeys(merged))[:8]


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
    _merge_stored_plan(public_plan, _stored_plan(pipeline, pid, project))
    # A paper topic that brought only irrelevant papers over the last rounds sits this round out.
    # It leaves the measured window that way and is tried again later.
    from project_watch.studio.openalex_query import topic_terms
    from project_watch.studio.round_quality import stale_queries

    stale = {q.strip('"') for s in stale_queries(pipeline.store, pid)
             if s['source'] in ('openalex_topic', 'europepmc_topic') for q in [s['query']]}
    resting = [q for q in public_plan['paper_queries'] if (topic_terms(q) or '').strip('"') in stale]
    if resting:
        public_plan['paper_queries'] = [q for q in public_plan['paper_queries'] if q not in resting]
        public_plan['resting_paper_queries'] = resting
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
    # What is read is the relevance pre-filter's choice; there is no count cap.
    read = pipeline.fetch_bodies(pid, stage, eligible_ids=eligible, collectors=collectors)
    return {'read': read, 'candidates': len(ids)}


def _record_health(pipeline, pid, rows):
    """Per-channel source health from this round's receipts (counts only, no content)."""
    from project_watch.studio.collection_plan import channel_key
    from project_watch.studio.health import SOURCE_KINDS

    report = {}
    for row in rows:
        if row['capability'] != 'available' or row['outcome'] == 'not_due':
            continue
        key = channel_key(row)
        if key not in SOURCE_KINDS:
            continue
        entry = report.setdefault(key, {'checked': 0, 'added': 0, 'errors': []})
        if row['outcome'] in ('success', 'partial'):
            entry['checked'] += 1
            entry['added'] += row.get('saved_count') or 0
        if row['outcome'] != 'success':
            entry['errors'].append(row['reason'] or row['outcome'])
    if report:
        pipeline.health.record(pid, report)


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
                  bodies_read=bodies.get('read', 0),
                  not_collected=[dict(source=r['source'], outcome=r['outcome'], reason=(r['reason'] or '')[:80])
                                 for r in rows if r['capability'] == 'available'
                                 and r['outcome'] not in ('success', 'not_due')][:30])
    execution.checkpoint(RESULT, result)
    _record_health(pipeline, pid, rows)
    # The scheduler's cadence counts attempts, so a failing source never loops;
    # only a round that collected something counts as observed.
    pipeline.store.set_state('external_attempt:' + pid, now())
    if status != 'failed':
        pipeline.store.touch(pid, 'last_external_at')
    if status != 'failed':
        pipeline.store.enqueue(pid, 'analyze', input_version='collect:' + execution.job['id'])
    return result


def summary(store, job_id):
    from project_watch.studio.execution import JobExecution

    return JobExecution(store, {'id': job_id}).read_checkpoint(RESULT)

