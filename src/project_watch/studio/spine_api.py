"""HTTP routes for the rewritten pipeline: round progress, re-analysis, assumptions, early warning."""
from contextlib import closing
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field

from project_watch.studio import assumptions, dispatch
from project_watch.studio.execution import JobExecution


class AssumptionInput(BaseModel):
    text: str = Field(min_length=4, max_length=300)


class AssumptionPatch(BaseModel):
    text: str | None = Field(default=None, min_length=4, max_length=300)
    status: Literal['active', 'retired'] | None = None


class Reanalyze(BaseModel):
    # The owner acknowledges that a request whose earlier result was unclear may be sent again.
    accept_duplicate_cost: bool


def _latest(db, pid, kind):
    row = db.execute('SELECT * FROM studio_jobs WHERE project_id=? AND kind=? ORDER BY created_at DESC,rowid DESC LIMIT 1',
                     (pid, kind)).fetchone()
    return dict(row) if row else None


def round_view(store, pid):
    """The latest round as the owner sees it: collected, analysing, partly done, done."""
    from project_watch.studio import analyze_job, collect_job, report_job, round_quality
    from project_watch.studio.research_sources import research_source_counts

    with closing(store._connect()) as db:
        jobs = {kind: _latest(db, pid, kind) for kind in ('collect', 'analyze', 'report')}
    view = {}
    for kind, stage in (('collect', collect_job.RESULT), ('analyze', analyze_job.RESULT), ('report', report_job.RESULT)):
        job = jobs[kind]
        if job is None:
            view[kind] = None
            continue
        result = JobExecution(store, {'id': job['id']}).read_checkpoint(stage)
        view[kind] = dict(job_id=job['id'], status=job['status'], stage=job['stage'],
                          started_at=job['started_at'], finished_at=job['finished_at'],
                          error=job['error'] if job['status'] == 'failed' else '', result=result)
    collect = jobs['collect']
    analyze = view['analyze']
    can_redo = bool(collect and collect['status'] == 'done' and (
        analyze is None or analyze['status'] in ('failed', 'archived')
        or ((analyze.get('result') or {}).get('status') in ('partial', 'failed'))))
    busy = any(j and j['status'] in ('queued', 'running') for j in jobs.values())
    return dict(collect=view['collect'], analyze=analyze, report=view['report'],
                materials=research_source_counts(store, pid),
                materials_unreviewed=research_source_counts(store, pid, review='unreviewed'),
                materials_collected=research_source_counts(store, pid, review=None),
                running=busy, can_reanalyze=can_redo and not busy, quality=round_quality.report(store, pid))


def attach(app, store, project_or_404):
    @app.get('/api/studio/projects/{project_id}/round')
    def project_round(project_id: str):
        project_or_404(project_id)
        return round_view(store, project_id)

    @app.post('/api/studio/projects/{project_id}/reanalyze', status_code=202)
    def reanalyze(project_id: str, body: Reanalyze):
        project = project_or_404(project_id)
        if project.get('status') != 'active':
            raise HTTPException(409, '请先恢复监控')
        if not body.accept_duplicate_cost:
            raise HTTPException(409, '重新分析可能重新发送结果不明的模型请求，需要你确认可能重复计费')
        view = round_view(store, project_id)
        if not view['can_reanalyze']:
            raise HTTPException(409, '当前没有可以用已保存材料重新分析的轮次')
        batch = view['collect']['job_id']
        return store.enqueue(project_id, 'analyze', input_version=dispatch.REDO_PREFIX + batch)

    @app.get('/api/studio/projects/{project_id}/assumptions')
    def list_assumptions(project_id: str, include_retired: bool = False):
        project_or_404(project_id)
        return {'items': assumptions.current(store, project_id, include_retired=include_retired)}

    @app.post('/api/studio/projects/{project_id}/assumptions', status_code=201)
    def add_assumption(project_id: str, body: AssumptionInput):
        project_or_404(project_id)
        return assumptions.edit(store, project_id, text=body.text)

    @app.patch('/api/studio/projects/{project_id}/assumptions/{assumption_id}')
    def patch_assumption(project_id: str, assumption_id: str, body: AssumptionPatch):
        project_or_404(project_id)
        try:
            return assumptions.edit(store, project_id, assumption_id=assumption_id, text=body.text, status=body.status)
        except KeyError as exc:
            raise HTTPException(404, '没有这条假设') from exc

    @app.get('/api/studio/projects/{project_id}/warning')
    def warning(project_id: str):
        project_or_404(project_id)
        record = assumptions.latest_warning(store, project_id)
        if not record:
            return None
        texts = {a['id']: a for a in assumptions.current(store, project_id, include_retired=True)}
        ids = {s['item'] for a in record.get('assumptions', []) for s in a['signals']}
        ids |= {i for u in record.get('unforeseen', []) for i in u['items']}
        with closing(store._connect()) as db:
            marks = ','.join('?' * len(ids))
            items = {r['id']: dict(r) for r in db.execute(
                f'SELECT id,title,url,source,published_at,category,importance FROM studio_items WHERE project_id=? AND id IN ({marks})',
                (project_id, *ids))} if ids else {}
        for row in record.get('assumptions', []):
            row['text'] = (texts.get(row['id']) or {}).get('text', '')
            row['signals'] = [{**s, 'source': items.get(s['item'])} for s in row['signals']]
        for row in record.get('unforeseen', []):
            row['sources'] = [items[i] for i in row['items'] if i in items]
        return record
