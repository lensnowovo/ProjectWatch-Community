"""Local-only consent-bound model invocation, independent from automatic intake."""

import hashlib
import secrets
import threading
import time

from fastapi import HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from project_watch.executors import ExecutorUnavailable, ModelCandidateExecutor


class ManualModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    consent: bool
    ticket: str = Field(min_length=16, max_length=100)


def attach_manual_model_routes(app, repository, model, *, team_mode=False):
    tickets = {}
    lock = threading.Lock()

    def enabled(request):
        selected_model = model.get() if hasattr(model, 'get') else model
        if team_mode or selected_model is None:
            raise HTTPException(503, "AI 执行尚未配置；材料仍可在本机阅读。")
        if request.url.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise HTTPException(403, "本轮 AI 执行仅供本机使用。")
        return selected_model

    def evidence(project_id, source_id, locator):
        source = repository.get_source(source_id)
        if source is None or source.project_id != project_id or source.kind != "document":
            raise HTTPException(404, "材料不存在于当前项目。")
        fragment = next(
            (f for f in repository.list_fragments(source_id) if f.locator == locator), None
        )
        if fragment is None or not fragment.text.strip():
            raise HTTPException(404, "没有可读取的原文片段。")
        text = fragment.text[:1500]
        digest = hashlib.sha256(text.encode()).hexdigest()
        return source, text, digest

    @app.get("/api/projects/{project_id}/ai-extraction")
    def disclosure(
        request: Request,
        project_id: str,
        source_id: str = Query(max_length=100),
        source_locator: str = Query(max_length=200),
    ):
        selected_model = enabled(request)
        source, text, digest = evidence(project_id, source_id, source_locator)
        settings = selected_model.disclosure()
        settings['config_id'] += getattr(model,'revision','')
        ticket = secrets.token_urlsafe(32)
        with lock:
            now = time.monotonic()
            for key in list(tickets):
                if tickets[key]["expires"] < now:
                    del tickets[key]
            if len(tickets) >= 256:
                raise HTTPException(429, "待确认请求过多，请稍后重试。")
            tickets[ticket] = dict(
                project=project_id,
                source=source_id,
                locator=source_locator,
                sha=source.content_sha256,
                digest=digest,
                config=settings["config_id"],
                expires=now + 600,
            )
        return {
            **settings,
            "ticket": ticket,
            "excerpt": text,
            "source_reference": source.reference,
            "source_locator": source_locator,
            "truncated": len(text) == 1500,
        }

    @app.post("/api/projects/{project_id}/ai-extraction")
    def execute(request: Request, project_id: str, body: ManualModelRequest):
        selected_model = enabled(request)
        origin = request.headers.get("origin")
        if origin != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "需要从本机页面确认发送。")
        if body.consent is not True:
            raise HTTPException(422, "请先确认发送范围和模型接收方。")
        with lock:
            selected = tickets.get(body.ticket)
            if (
                not selected
                or selected["project"] != project_id
                or selected["expires"] < time.monotonic()
            ):
                raise HTTPException(409, "请求已执行或预览已过期，请重新打开预览。")
            del tickets[body.ticket]
        source, _text, digest = evidence(project_id, selected["source"], selected["locator"])
        if (
            source.content_sha256 != selected["sha"]
            or digest != selected["digest"]
            or selected_model.disclosure()["config_id"] + getattr(model,'revision','') != selected["config"]
        ):
            raise HTTPException(409, "材料或模型配置已变化，请重新核对后发送。")
        try:
            candidate = ModelCandidateExecutor(repository, selected_model).propose(
                project_id,
                source_id=source.id,
                source_locator=selected["locator"],
            )
        except (ExecutorUnavailable, ValueError):
            raise HTTPException(
                502,
                "本次 AI 提取未完成或未通过证据校验。未新增候选；请检查执行配置、额度或片段内容。",
            ) from None
        return {
            "candidate_id": candidate.id,
            "status": candidate.status,
            "receipt": getattr(selected_model, "receipt", {}),
            "model": selected_model.disclosure()["model"],
        }
