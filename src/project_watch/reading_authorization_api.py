"""Local operator-authorized material egress settings. No automatic calls on GET."""

import hmac
import os
from dataclasses import asdict
from typing import Literal

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from project_watch.company_state import READING_OUTPUT_TOKENS, READING_RULE_VERSION
from project_watch.reading_jobs import ReadingDenied, ReadingJobs, ReadingLimits


class ReadingAuthorizationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    consent: bool = Field(strict=True)
    config_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    # Bind the submission to the disclosure the operator actually saw. Missing
    # fields from an old cached client are rejected, never upgraded silently.
    rule_version: str | None = Field(default=None, min_length=1, max_length=100, strict=True)
    max_output_tokens: int | None = Field(default=None, ge=1, le=8192, strict=True)
    request_key: str = Field(min_length=16, max_length=100)
    source_hashes: list[str] = Field(default_factory=list, max_length=1000)
    include_future: bool = Field(default=False, strict=True)
    max_requests: int = Field(default=20, ge=1, le=1000, strict=True)
    max_cost_cny: str | None = Field(default=None, max_length=40)
    input_cny_per_million: str | None = Field(default=None, max_length=40)
    output_cny_per_million: str | None = Field(default=None, max_length=40)


class ReadingAuthorizationStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["active", "paused", "revoked"]


def attach_reading_authorization(app, repository, settings, *, team_mode=False):
    ledger = None if team_mode else ReadingJobs(repository)
    operator_token = os.environ.get("PROJECT_WATCH_SETTINGS_TOKEN", "")
    app.state.reading_jobs = ledger

    def allowed(request, project_id, *, write=False):
        if team_mode:
            raise HTTPException(404, "本机材料授权不适用于团队部署。")
        if request.url.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise HTTPException(403, "请从本机页面操作。")
        if write and (
            not operator_token
            or not hmac.compare_digest(request.headers.get("x-settings-token", ""), operator_token)
            or request.headers.get("origin") != str(request.base_url).rstrip("/")
        ):
            raise HTTPException(403, "调整材料发送范围需要本机管理口令。")
        if repository.get_project(project_id) is None:
            raise HTTPException(404, "项目不存在。")

    @app.get("/api/workspace/projects/{project_id}/reading-authorization")
    def status(request: Request, project_id: str):
        allowed(request, project_id)
        model = settings.get()
        return {
            "automatic_reading_available": bool(getattr(app.state, "reading_worker_enabled", False)),
            "rule_version": READING_RULE_VERSION,
            "model": None if model is None or not hasattr(model, "authorization_id") else {
                "config_id": model.authorization_id(),
                "recipient": asdict(model.config),
                "max_output_tokens": READING_OUTPUT_TOKENS,
            },
            "authorizations": [g for g in ledger.list_authorizations(project_id) if g['purpose'] == 'company_reading'],
            "purpose": "仅整理本项目材料，形成带出处的阅读内容，不确认正式事实。",
            "budget_note": "调用上限会跨重启保留；金额为本地估算，以服务商账单为准。",
            "pause_note": "暂停后不再发送新请求，已发出的请求无法撤回。",
        }

    @app.post("/api/workspace/projects/{project_id}/reading-authorization")
    def authorize(request: Request, project_id: str, body: ReadingAuthorizationInput):
        allowed(request, project_id, write=True)
        model = settings.get()
        if model is None or not hasattr(model, "authorization_id"):
            raise HTTPException(503, "请先在设置中配置模型；原件仍可阅读。")
        if body.consent is not True:
            raise HTTPException(422, "请先确认接收服务、材料范围和用量上限。")
        if model.authorization_id() != body.config_id:
            raise HTTPException(409, "模型配置已变化，请重新核对接收方后确认。")
        if body.rule_version != READING_RULE_VERSION or body.max_output_tokens != READING_OUTPUT_TOKENS:
            raise HTTPException(409, "整理方式或用量上限已变化，请重新打开自动整理窗口后确认。")
        try:
            return ledger.authorize(
                project_id, config_id=body.config_id, model=model.config.model,
                rule_version=READING_RULE_VERSION, source_hashes=body.source_hashes,
                include_future=body.include_future, actor="local-operator",
                recipient=asdict(model.config),
                request_key=body.request_key,
                limits=ReadingLimits(
                    max_requests=body.max_requests,
                    max_output_tokens=READING_OUTPUT_TOKENS,
                    max_cost_cny=body.max_cost_cny,
                    input_cny_per_million=body.input_cny_per_million,
                    output_cny_per_million=body.output_cny_per_million,
                ),
            )
        except (ReadingDenied, ValueError):
            raise HTTPException(422, "材料范围或用量限制无效，请重新核对。") from None

    @app.patch("/api/workspace/projects/{project_id}/reading-authorization/{grant_id}")
    def change(request: Request, project_id: str, grant_id: str, body: ReadingAuthorizationStatus):
        allowed(request, project_id, write=True)
        try:
            grant = ledger.status(grant_id)
        except ReadingDenied:
            raise HTTPException(404, "材料授权不存在。") from None
        if grant["project_id"] != project_id or grant['purpose'] != 'company_reading':
            raise HTTPException(404, "材料授权不存在。")
        if body.status == "active":
            model = settings.get()
            if (model is None or not hasattr(model, "authorization_id")
                    or model.authorization_id() != grant["config_id"] or grant["rule_version"] != READING_RULE_VERSION):
                raise HTTPException(409, "接收方已变化，请新建授权；旧额度不会重置。")
        try:
            ledger.set_status(grant_id, body.status, actor="local-operator")
        except ReadingDenied:
            raise HTTPException(409, "已撤销的授权不能恢复，请重新确认范围。") from None
        return ledger.status(grant_id)
