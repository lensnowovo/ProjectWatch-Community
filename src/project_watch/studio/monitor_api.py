"""HTTP routes for monitoring depth: website pages, feedback rules, watch points, storylines."""

from __future__ import annotations

import json
from contextlib import closing
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Response
from pydantic import BaseModel, Field

from project_watch.studio import monitor as mon
from project_watch.studio import signals as sig
from project_watch.studio.questions import STATUSES, Assessment


class PageIn(BaseModel):
    url: str = Field(min_length=4, max_length=500)
    label: str = Field(default="", max_length=40)


class FeedbackIn(BaseModel):
    kind: Literal["irrelevant", "wrong_entity", "bad_source", "importance"]
    value: str = Field(default="", max_length=80)


class WatchIn(BaseModel):
    text: str = Field(min_length=2, max_length=200)
    alert: bool = True


class WatchPatch(BaseModel):
    alert: bool


class PlanItemIn(BaseModel):
    text: str = Field(min_length=2, max_length=200)
    label: str = Field(default="", max_length=24)
    queries: list[str] = Field(default_factory=list, max_length=4)
    criteria: str = Field(default="", max_length=300)
    axis: str = "asset"
    objects: list[str] = Field(default_factory=list, max_length=8)
    reason: str = Field(default="", max_length=600)
    current_state: str = Field(default="", max_length=600)
    sources: str = Field(default="", max_length=600)
    cadence: str = "medium"
    trigger_rule: str = Field(default="", max_length=600)
    exit_rule: str = Field(default="", max_length=600)
    tier: str = "core"
    alert: bool = True
    view: str = "evidence"
    view_params: dict = Field(default_factory=dict)


class MetricPointIn(BaseModel):
    period: str = Field(min_length=1, max_length=20)
    value: float = Field(strict=True, allow_inf_nan=False)
    unit: str = Field(default="", max_length=20)
    note: str = Field(default="", max_length=200)
    url: str = Field(default="", max_length=500)


class TemplateApply(BaseModel):
    templates: list[str] = Field(min_length=1, max_length=4)


class PlanItemPatch(BaseModel):
    text: str | None = Field(default=None, min_length=2, max_length=200)
    queries: list[str] | None = Field(default=None, max_length=4)
    axis: str | None = None
    objects: list[str] | None = Field(default=None, max_length=8)
    reason: str | None = Field(default=None, max_length=600)
    current_state: str | None = Field(default=None, max_length=600)
    sources: str | None = Field(default=None, max_length=600)
    cadence: str | None = None
    trigger_rule: str | None = Field(default=None, max_length=600)
    exit_rule: str | None = Field(default=None, max_length=600)
    tier: str | None = None
    status: str | None = None
    view: str | None = None
    view_params: dict | None = None
    note: str = Field(default="", max_length=300)


class PlanSection(BaseModel):
    baseline: str = Field(default="", max_length=800)
    known: str = Field(default="", max_length=1200)
    coverage: str = "none"


class PlanMeta(BaseModel):
    understanding: str | None = Field(default=None, max_length=1200)
    method: str | None = Field(default=None, max_length=1200)
    in_scope: list[str] | None = Field(default=None, max_length=12)
    out_of_scope: list[str] | None = Field(default=None, max_length=12)
    sections: dict[str, PlanSection] | None = None
    review: str | None = None
    note: str = Field(default="", max_length=300)


class PlanImport(BaseModel):
    items: list[PlanItemIn] = Field(default_factory=list, max_length=16)
    meta: PlanMeta | None = None
    reason: str = Field(min_length=2, max_length=300)


class CheckPatch(BaseModel):
    status: Literal["open", "resolved", "dismissed"]


def register_monitor_routes(app: FastAPI, store, pipeline) -> None:
    def monitor():
        return pipeline.monitor

    def project_or_404(project_id: str) -> dict:
        project = store.get_project(project_id)
        if not project:
            raise HTTPException(404, "项目不存在")
        return project

    def event_or_404(event_id: str) -> dict:
        found = store.list_events(event_ids=[event_id], include_dismissed=True, limit=1)
        if not found:
            raise HTTPException(404, "事件不存在")
        return found[0]

    @app.get("/api/studio/projects/{project_id}/monitor")
    def monitor_state(project_id: str):
        project_or_404(project_id)
        m = monitor()
        return {"pages": m.pages(project_id), "changes": m.changes(project_id, 20), "rules": m.rules(project_id),
                "watchpoints": m.watchpoints(project_id), "threads": m.threads(project_id),
                "watch_labels": m.event_watch_labels(project_id), "stages": mon.THREAD_STAGES,
                "questions": pipeline.questions.view(project_id),
                "site_discovery": mon.site_discovery(store, project_id)}

    # ---------------------------------------------------------------- website pages
    @app.post("/api/studio/projects/{project_id}/pages", status_code=201)
    def add_page(project_id: str, body: PageIn):
        project_or_404(project_id)
        try:
            return monitor().add_page(project_id, body.url, body.label, "custom", "user")
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.delete("/api/studio/site-pages/{page_id}", status_code=204)
    def remove_page(page_id: str):
        with monitor()._db() as db:
            page = db.execute("SELECT project_id, url, added_by FROM studio_site_pages WHERE id=?", (page_id,)).fetchone()
        try:
            monitor().remove_page(page_id)
        except KeyError as exc:
            raise HTTPException(404, "页面不存在") from exc
        # Removing an automatically found homepage means "this is not the company's site": do not search again.
        found = mon.site_discovery(store, page["project_id"]) if page else {}
        if found.get("status") == "found" and found.get("url") == page["url"]:
            store.set_state(f"site_discovery:{page['project_id']}", json.dumps({**found, "status": "dismissed"}, ensure_ascii=False))

    # ---------------------------------------------------------------- feedback & rules
    @app.post("/api/studio/events/{event_id}/feedback")
    def feedback(event_id: str, body: FeedbackIn):
        event = event_or_404(event_id)
        m = monitor()
        project_id = event["project_id"]
        rule = None
        if body.kind == "importance":
            if body.value not in ("major", "notable", "routine"):
                raise HTTPException(422, "重要性无效")
            with closing(store._connect()) as db, db:
                db.execute("UPDATE studio_events SET importance=? WHERE id=?", (body.value, event_id))
            m.add_feedback(project_id, event_id, "importance", event["title"], event["importance"], body.value)
            return {"ok": True, "message": "已记住你的判断，后续同类信息会参考", "rule": None}
        if body.kind == "wrong_entity":
            if len(body.value.strip()) < 2:
                raise HTTPException(422, "请填写被误认的公司或名称")
            rule = m.add_rule(project_id, "exclude_entity", body.value, f"来自反馈：{event['title'][:40]}")
        elif body.kind == "bad_source":
            value = body.value.strip() or mon.host_of(event.get("url") or "") or event.get("source") or ""
            if len(value) < 2:
                raise HTTPException(422, "无法确定来源")
            rule = m.add_rule(project_id, "block_source", value, f"来自反馈：{event['title'][:40]}")
        m.add_feedback(project_id, event_id, body.kind, event["title"])
        store.set_event_status(event_id, "dismissed")
        message = {"exclude_entity": f"已记住：以后排除“{rule['value']}”" if rule else "",
                   "block_source": f"已记住：以后屏蔽来源 {rule['value']}" if rule else ""}.get(rule["kind"] if rule else "", "已隐藏，后续同类信息会参考")
        return {"ok": True, "message": message, "rule": rule}

    @app.post("/api/studio/projects/{project_id}/rules", status_code=201)
    def add_rule(project_id: str, body: dict):
        project_or_404(project_id)
        try:
            return monitor().add_rule(project_id, str(body.get("kind", "")), str(body.get("value", "")), "手动添加")
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.delete("/api/studio/rules/{rule_id}", status_code=204)
    def remove_rule(rule_id: str):
        try:
            monitor().remove_rule(rule_id)
        except KeyError as exc:
            raise HTTPException(404, "规则不存在") from exc

    # ---------------------------------------------------------------- monitoring plan
    def plan():
        return pipeline.monitoring_plan

    @app.get("/api/studio/projects/{project_id}/monitoring-plan")
    def plan_view(project_id: str):
        project_or_404(project_id)
        return plan().view(project_id)

    @app.post("/api/studio/projects/{project_id}/monitoring-plan/items", status_code=201)
    def plan_add(project_id: str, body: PlanItemIn):
        project_or_404(project_id)
        try:
            return plan().add(project_id, body.model_dump(), actor="human", origin="human", reason="手动添加")
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/studio/projects/{project_id}/monitoring-plan/import", status_code=201)
    def plan_import(project_id: str, body: PlanImport):
        """Load a reviewed plan (the owner's action); same question text updates instead of duplicating."""
        project_or_404(project_id)
        try:
            if body.meta:
                meta = body.meta.model_dump(exclude_none=True)
                meta.pop("note", None)
                plan().set_meta(project_id, meta, actor="human", reason=body.reason)
            return plan().import_items(project_id, [i.model_dump() for i in body.items], reason=body.reason)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.put("/api/studio/projects/{project_id}/monitoring-plan/meta")
    def plan_meta(project_id: str, body: PlanMeta):
        project_or_404(project_id)
        meta = body.model_dump(exclude_none=True)
        note = meta.pop("note", "")
        try:
            return plan().set_meta(project_id, meta, actor="human", reason=note or "调整方案概要")
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.patch("/api/studio/monitoring-plan/items/{watch_id}")
    def plan_patch(watch_id: str, body: PlanItemPatch):
        fields = body.model_dump(exclude_none=True)
        note = fields.pop("note", "")
        try:
            return plan().update(watch_id, fields, actor="human", reason=note or "手动调整")
        except KeyError as exc:
            raise HTTPException(404, "方案条目不存在") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/studio/projects/{project_id}/monitoring-plan/templates")
    def plan_templates(project_id: str, body: TemplateApply):
        project_or_404(project_id)
        try:
            return plan().apply_templates(project_id, body.templates)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/studio/monitoring-plan/items/{watch_id}/points", status_code=201)
    def plan_point(watch_id: str, body: MetricPointIn):
        try:
            return plan().add_point(watch_id, **body.model_dump())
        except KeyError as exc:
            raise HTTPException(404, "方案条目不存在") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.delete("/api/studio/monitoring-plan/points/{point_id}", status_code=204)
    def plan_point_remove(point_id: str):
        try:
            plan().remove_point(point_id)
        except KeyError as exc:
            raise HTTPException(404, "数据点不存在") from exc

    @app.post("/api/studio/projects/{project_id}/monitoring-plan/review")
    def plan_review(project_id: str):
        project_or_404(project_id)
        return plan().confirm_review(project_id)

    @app.post("/api/studio/monitoring-plan/changes/{change_id}/accept")
    def plan_accept(change_id: str):
        try:
            return plan().decide(change_id, True)
        except KeyError as exc:
            raise HTTPException(404, "建议不存在或已处理") from exc

    @app.post("/api/studio/monitoring-plan/changes/{change_id}/reject")
    def plan_reject(change_id: str):
        try:
            return plan().decide(change_id, False)
        except KeyError as exc:
            raise HTTPException(404, "建议不存在或已处理") from exc

    @app.post("/api/studio/monitoring-plan/changes/{change_id}/revert")
    def plan_revert(change_id: str):
        try:
            return plan().revert(change_id)
        except KeyError as exc:
            raise HTTPException(404, "变更不存在") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    # ---------------------------------------------------------------- watch points
    @app.post("/api/studio/projects/{project_id}/watchpoints", status_code=201)
    def add_watchpoint(project_id: str, body: WatchIn, response: Response):
        project = project_or_404(project_id)
        if project.get('execution_mode') == 'agent':
            raise HTTPException(409, '此项目由 Agent 更新，请在连接的 Agent 中调整关注点')
        if project.get('status') == 'removed':
            raise HTTPException(404, '项目不存在')
        from project_watch.studio.monitoring_plan import MAX_ITEMS

        text = body.text.strip()
        if len(text) < 2:
            raise HTTPException(422, '请输入关注内容')
        config = pipeline.settings_store.load()

        def check_capacity(db=None):
            if sum(w.get('status') != 'retired' for w in monitor().watchpoints(project_id, db=db)) >= MAX_ITEMS:
                raise HTTPException(422, f'每个项目最多 {MAX_ITEMS} 个运行中的关注点')

        def prepare():
            check_capacity()
            _settings, _http, llm, _collectors = pipeline.tools(config)
            return mon.build_watchpoint(llm, project, text)

        def commit(spec, db):
            current = store.get_project(project_id, db=db)
            if not current or current.get('status') == 'removed':
                raise HTTPException(404, '项目不存在')
            if any(current.get(k) != project.get(k) for k in ('legal_name', 'short_name', 'name')):
                raise HTTPException(409, '公司信息已变化，请重新添加关注点')
            check_capacity(db)
            watch = monitor().add_watchpoint(project_id, text, spec['label'], spec['queries'], spec['criteria'], body.alert, db=db)
            if current.get('activated') and current.get('status') == 'active':
                store.enqueue(project_id, 'external', db=db)
            return {'id': watch['id']}

        result, operation_id, replayed = pipeline.direct_operations.run('add_watchpoint',
            {'project_id': project_id, 'company': {k: project.get(k) for k in ('legal_name', 'short_name', 'name')},
             'text': text, 'alert': body.alert},
            config, prepare, commit=commit)
        response.headers['X-ProjectWatch-Operation'] = operation_id
        response.headers['X-ProjectWatch-Replayed'] = str(replayed).lower()
        watch = next((w for w in monitor().watchpoints(project_id) if w['id'] == result['id']), None)
        if not watch:
            raise HTTPException(404, '关注点已删除')
        # Idempotent local routing can finish after a process stops following commit.
        pipeline.questions.route_existing_materials(project_id, {watch['id']})
        return watch

    @app.patch("/api/studio/watchpoints/{watch_id}")
    def patch_watchpoint(watch_id: str, body: WatchPatch):
        try:
            monitor().update_watchpoint(watch_id, alert=body.alert)
        except KeyError as exc:
            raise HTTPException(404, "关注点不存在") from exc
        return {"ok": True}

    @app.delete("/api/studio/watchpoints/{watch_id}", status_code=204)
    def remove_watchpoint(watch_id: str):
        try:
            monitor().remove_watchpoint(watch_id)
        except KeyError as exc:
            raise HTTPException(404, "关注点不存在") from exc

    # ---------------------------------------------------------------- question judgements (M2)
    @app.get("/api/studio/projects/{project_id}/questions")
    def questions(project_id: str):
        project_or_404(project_id)
        return {"questions": pipeline.questions.view(project_id), "statuses": STATUSES}

    @app.get("/api/studio/projects/{project_id}/questions/{watch_id}/history")
    def question_history(project_id: str, watch_id: str, limit: int = Query(default=50, ge=1, le=200),
                         offset: int = Query(default=0, ge=0)):
        project_or_404(project_id)
        if not any(w["id"] == watch_id for w in monitor().watchpoints(project_id)):
            raise HTTPException(404, "问题不存在")
        return pipeline.questions.history(watch_id, limit, offset=offset)

    @app.post("/api/studio/projects/{project_id}/questions/{watch_id}/assessments", status_code=201)
    def add_human_assessment(project_id: str, watch_id: str, body: Assessment):
        project_or_404(project_id)
        try:
            return pipeline.questions.record(project_id, watch_id, "human", body)
        except KeyError as exc:
            raise HTTPException(404, "问题不存在") from exc
        except PermissionError as exc:
            raise HTTPException(403, "内部材料判断不能公开") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/studio/watchpoints/{watch_id}/events")
    def watch_events(watch_id: str):
        ids = monitor().watch_event_ids(watch_id)
        return store.list_events(event_ids=ids, limit=200) if ids else []

    # ---------------------------------------------------------------- signals
    @app.get("/api/studio/projects/{project_id}/signals")
    def project_signals(project_id: str):
        project_or_404(project_id)
        documents = [{"created_at": s.created_at.isoformat()} for s in pipeline.repository.list_sources(project_id)
                     if s.kind == "document"]
        base = pipeline.settings_store.load().refresh_hours
        return {"crosschecks": pipeline.signals.crosschecks(project_id),
                "silence": sig.silence(store, monitor(), project_id, documents, base_hours=base),
                "cadence": sig.cadence(store, monitor(), project_id, base),
                "latency": sig.latency(store, project_id)}

    @app.patch("/api/studio/crosschecks/{check_id}")
    def patch_crosscheck(check_id: str, body: CheckPatch):
        try:
            return pipeline.signals.set_status(check_id, body.status)
        except KeyError as exc:
            raise HTTPException(404, "核对项不存在") from exc
