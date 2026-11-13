"""Entry for the rewritten pipeline's job kinds. One path per kind; no legacy admission."""
from contextlib import closing, nullcontext

from project_watch.studio.store import now

ROUND_KINDS = ('collect', 'analyze', 'report')


def request_round(store, project_id, reason, *, automatic=False, db=None):
    """The one way a new round starts: a collect job, unless a round is already queued or running.

    reason is 'manual' (the owner's 更新), 'scheduled' or 'created'. A queued or
    running stage of the same project is returned as is: one round at a time.
    Earlier requests, settled or not, never stop a new round; the dispatch
    check accounts for them per request.
    """
    if reason not in ('manual', 'scheduled', 'created'):
        raise ValueError('invalid round reason')
    own = db is None
    with (closing(store._connect()) if own else nullcontext(db)) as conn, (conn if own else nullcontext()):
        if own:
            conn.execute('BEGIN IMMEDIATE')
        if reason == 'manual':
            # A started job waiting out a connection backoff holds its project (claim);
            # 更新 resumes it now. Nothing was sent; a remaining rate limit re-defers it
            # without a request.
            conn.execute("UPDATE studio_jobs SET retry_after=NULL WHERE project_id=? AND status='queued' "
                         "AND started_at IS NOT NULL AND retry_after>?", (project_id, now()))
            conn.execute('DELETE FROM studio_provider_backoff WHERE status=0')
        active = conn.execute("SELECT * FROM studio_jobs WHERE project_id=? AND kind IN ('collect','analyze','report') "
                              "AND status IN ('queued','running') ORDER BY created_at,id LIMIT 1", (project_id,)).fetchone()
        if active:
            if (reason == 'manual' and active['kind'] == 'collect' and active['status'] == 'queued'
                    and (active['retry_after'] or '') > now()):
                # An owner's 更新 resumes a round waiting for the network now. Only
                # connection backoffs are cleared: nothing was sent, so nothing can repeat.
                conn.execute('UPDATE studio_jobs SET retry_after=NULL WHERE id=?', (active['id'],))
                conn.execute('DELETE FROM studio_provider_backoff WHERE status=0')
                active = conn.execute('SELECT * FROM studio_jobs WHERE id=?', (active['id'],)).fetchone()
            return dict(active)
        return store.enqueue(project_id, 'collect', automatic=automatic, input_version=f'{reason}:{now()}', db=conn)


def run(pipeline, execution):
    kind = execution.job['kind']
    if kind == 'collect':
        from project_watch.studio import collect_job

        project = pipeline.store.get_project(execution.job['project_id']) or {}
        mode = ('first' if not project.get('last_external_at')
                else 'refresh' if execution.job.get('input_version', '').startswith('manual:') else 'incremental')
        return collect_job.run(pipeline, execution, mode=mode)
    if kind == 'analyze':
        from project_watch.studio import analyze_job

        return analyze_job.run(pipeline, execution)
    if kind == 'report':
        from project_watch.studio import report_job

        return report_job.run(pipeline, execution)
    raise ValueError('not a rewritten pipeline job kind')


def schedule_document_analysis(pipeline, project_id):
    """After internal documents are analysed, one analysis on a fresh snapshot; never a collection.

    Event-driven (a finished ``internal`` job newer than the latest analysis), so
    nothing loops: each internal job triggers at most one analysis, whatever its
    outcome. A failed one is redone by the owner, not automatically.
    """
    store = pipeline.store
    project = store.get_project(project_id)
    if not project or not project.get('activated') or project.get('status') != 'active':
        return None
    if not pipeline.settings_store.load().model.ready:
        return None
    with closing(store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        busy = db.execute("SELECT 1 FROM studio_jobs WHERE project_id=? AND status IN ('queued','running') "
                          "AND kind IN ('collect','analyze','report','internal') LIMIT 1", (project_id,)).fetchone()
        if busy:
            return None
        internal = db.execute("SELECT id,finished_at FROM studio_jobs WHERE project_id=? AND kind='internal' AND status='done' "
                              "ORDER BY finished_at DESC,rowid DESC LIMIT 1", (project_id,)).fetchone()
        if not internal:
            return None
        version = 'documents:' + internal['id']
        later = db.execute("SELECT 1 FROM studio_jobs WHERE project_id=? AND kind='analyze' "
                           "AND (input_version=? OR created_at>=?) LIMIT 1",
                           (project_id, version, internal['finished_at'] or '')).fetchone()
        if later:
            return None  # this document batch was already analysed, alone or within a round
        return store.enqueue(project_id, 'analyze', automatic=True, input_version=version, db=db)
