"""Local workspace projections. Client state is separate from business facts."""

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Literal

from fastapi import HTTPException, Query, Request
from pydantic import BaseModel, Field

from project_watch.monitor_briefing import project_briefing
from project_watch.monitor_profile import project_profile
from project_watch.reading_projection import project_reading

EVENTS = """
SELECT 'source:'||s.id id,s.project_id,'材料' kind,s.reference title,s.created_at time,
       'materials' tab, s.id source_id FROM sources s
UNION ALL
SELECT 'observation:'||o.id,o.project_id,'待核对',o.statement,o.created_at,'review',o.source_id
FROM observations o WHERE o.status='candidate'
UNION ALL
SELECT 'signal:'||s.id,s.project_id,'公开线索',s.title,s.first_seen_at,'overview',NULL FROM signals s
UNION ALL
SELECT 'failure:'||r.id,r.project_id,'来源异常',COALESCE(r.error_code,'检查未完成'),r.finished_at,'overview',NULL
FROM source_runs r WHERE r.status != 'completed'
"""


class InboxAction(BaseModel):
    id: str = Field(max_length=150)
    status: Literal["open", "done", "ignored"]


class ProfileEdit(BaseModel):
    expected_version: int = Field(ge=0, strict=True)
    fields: dict[str, str] = Field(default_factory=dict, max_length=5)
    focus: list[str] | None = Field(default=None, max_length=20)
    restore_fields: list[str] = Field(default_factory=list, max_length=5)
    restore_focus: bool = False


def attach_workspace_routes(app, repo, *, team_mode=False):
    # Sidecar is UI workflow metadata, not a migration of the investment database.
    sidecar = Path(str(repo.db_path) + ".client.sqlite3")
    if not team_mode:
        with closing(sqlite3.connect(sidecar)) as c, c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS inbox_state(id TEXT PRIMARY KEY,status TEXT NOT NULL)"
            )

    def allowed(request, write=False):
        if team_mode:
            raise HTTPException(404, "本机工作台接口不适用于团队部署。")
        if write and request.headers.get("origin") != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "请从本机页面执行。")

    def connect():
        c = repo._connect()
        c.execute("ATTACH DATABASE ? AS client", (str(sidecar),))
        return c

    @app.get("/api/workspace/attention")
    def attention(request: Request, include_demo: bool = False, q: str = Query("", max_length=100)):
        allowed(request)
        with closing(connect()) as c:
            rows = c.execute(
                """SELECT g.id,g.project_id,p.name project_name,g.title,g.url,g.freshness,
                COALESCE(g.published_at,g.first_seen_at) time, 'news' kind
                FROM signals g JOIN projects p ON p.id=g.project_id
                LEFT JOIN client.inbox_state i ON i.id='signal:'||g.id
                WHERE g.freshness='current' AND COALESCE(i.status,'open')='open'
                AND p.name LIKE ? AND (? OR (p.name NOT LIKE '%合成%' AND p.name NOT LIKE '%synthetic%'
                AND p.name NOT LIKE '%演示%' AND p.name NOT LIKE '%demo%'))
                ORDER BY time DESC,g.id LIMIT 100""",
                (f"%{q}%", include_demo),
            ).fetchall()
        return {"items": [dict(r) for r in rows]}

    @app.get("/api/workspace/events")
    def events(
        request: Request,
        status: Literal["open", "done", "ignored", "all"] = "open",
        limit: int = Query(30, ge=1, le=100),
        offset: int = Query(0, ge=0),
        q: str = Query("", max_length=100),
    ):
        allowed(request)
        with closing(connect()) as c:
            rows = c.execute(
                f"""WITH events AS ({EVENTS})
                SELECT e.*,p.name project_name,COALESCE(i.status,'open') status FROM events e
                JOIN projects p ON p.id=e.project_id LEFT JOIN client.inbox_state i ON i.id=e.id
                WHERE (?='all' OR COALESCE(i.status,'open')=?) AND (p.name LIKE ? OR e.title LIKE ?)
                ORDER BY e.time DESC,e.id DESC LIMIT ? OFFSET ?""",
                (status, status, f"%{q}%", f"%{q}%", limit + 1, offset),
            ).fetchall()
        return {"items": [dict(r) for r in rows[:limit]], "has_more": len(rows) > limit}

    @app.post("/api/workspace/events/state")
    def set_state(request: Request, body: InboxAction):
        allowed(request, True)
        with closing(connect()) as c, c:
            if not c.execute(
                f"WITH events AS ({EVENTS}) SELECT 1 FROM events WHERE id=?", (body.id,)
            ).fetchone():
                raise HTTPException(404, "记录不存在。")
            c.execute(
                "INSERT INTO client.inbox_state VALUES (?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status",
                (body.id, body.status),
            )
        return {"status": body.status}

    @app.get("/api/workspace/projects/{project_id}/briefing")
    def briefing(request: Request, project_id: str):
        allowed(request)
        if repo.get_project(project_id) is None:
            raise HTTPException(404, "项目不存在。")
        return project_briefing(repo, project_id)

    @app.get("/api/workspace/projects/{project_id}/reading")
    def reading(request: Request, project_id: str):
        allowed(request)
        if repo.get_project(project_id) is None:
            raise HTTPException(404, "项目不存在。")
        result = project_reading(repo, project_id)
        worker = getattr(app.state, "reading_worker", None)
        if worker is not None:
            result["background"] = worker.status(project_id)
            if not getattr(app.state, "reading_worker_enabled", False):
                result["background"].update(reason="unavailable", poll=False)
        return result

    @app.get("/api/workspace/projects/{project_id}/profile")
    def profile(request: Request, project_id: str):
        allowed(request)
        return project_profile(repo, project_id)

    @app.get("/api/workspace/projects/{project_id}/profile/history")
    def profile_history(request: Request, project_id: str):
        allowed(request)
        return {"items": repo.monitor_profile_history(project_id)}

    @app.patch("/api/workspace/projects/{project_id}/profile")
    def edit_profile(request: Request, project_id: str, body: ProfileEdit):
        allowed(request, True)
        repo.edit_monitor_profile(project_id, **body.model_dump())
        return project_profile(repo, project_id)

    @app.get("/api/workspace/projects")
    def projects(
        request: Request,
        q: str = Query("", max_length=100),
        include_demo: bool = True,
        limit: int = Query(30, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ):
        allowed(request)
        with closing(repo._connect()) as c:
            rows = c.execute(
                """SELECT p.*,
                (SELECT count(*) FROM sources s WHERE s.project_id=p.id) source_count,
                (SELECT count(*) FROM observations o WHERE o.project_id=p.id AND o.status='candidate') pending_count,
                (SELECT count(*) FROM monitoring_plans m WHERE m.project_id=p.id AND m.enabled=1) active_monitors,
                (SELECT count(*) FROM signals g WHERE g.project_id=p.id) signal_count,
                (SELECT reference FROM sources s WHERE s.project_id=p.id ORDER BY created_at DESC,id DESC LIMIT 1) latest_material,
                COALESCE((SELECT max(created_at) FROM sources s WHERE s.project_id=p.id),p.created_at) updated_at
                FROM projects p WHERE p.name LIKE ?
                AND (? OR (p.name NOT LIKE '%合成%' AND p.name NOT LIKE '%synthetic%'
                    AND p.name NOT LIKE '%演示%' AND p.name NOT LIKE '%demo%'))
                ORDER BY updated_at DESC,p.id LIMIT ? OFFSET ?""",
                (f"%{q}%", include_demo, limit + 1, offset),
            ).fetchall()
        items = []
        for row in rows[:limit]:
            item = dict(row)
            item["reading_summary"] = project_reading(repo, item["id"])["summary"]
            items.append(item)
        return {"items": items, "has_more": len(rows) > limit}

    @app.get("/api/workspace/tasks")
    def tasks(request: Request, limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0)):
        allowed(request)
        with closing(repo._connect()) as c:
            rows = c.execute(
                """WITH jobs AS (
                SELECT j.id,j.project_id,'材料解析' kind,j.status,j.updated_at time,j.error_code,s.reference title
                FROM intake_jobs j JOIN sources s ON s.id=j.source_id
                UNION ALL SELECT id,project_id,'监控检查',status,updated_at,error_code,'公开来源检查' FROM monitoring_jobs
                UNION ALL SELECT id,project_id,'研究任务',status,created_at,NULL,kind FROM execution_tasks
            ) SELECT j.*,p.name project_name FROM jobs j JOIN projects p ON p.id=j.project_id
            ORDER BY j.time DESC,j.id DESC LIMIT ? OFFSET ?""",
                (limit + 1, offset),
            ).fetchall()
        return {"items": [dict(r) for r in rows[:limit]], "has_more": len(rows) > limit}
