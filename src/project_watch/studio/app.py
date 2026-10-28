"""Studio HTTP API (/api/studio/*), web UI at /, and the local app factory."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import FastAPI, File, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from project_watch.local_ocr import ocr_capabilities
from project_watch.static_files import ProductStaticFiles
from project_watch.studio import digest as dg
from project_watch.studio import finance, review
from project_watch.studio.agent_access import attach_agent_routes
from project_watch.studio.application import StudioApplication
from project_watch.studio.direct_operations import OperationBlocked
from project_watch.studio.http import HttpError
from project_watch.studio.llm import ModelError
from project_watch.studio.pipeline import Pipeline, StudioWorker
from project_watch.studio.public_profile import company_trials
from project_watch.studio.resolve import resolve_company
from project_watch.studio.settings import SearchConfig, SettingsStore, SettingsUnavailable
from project_watch.studio.sources import Collectors
from project_watch.studio.store import FollowupSuperseded, StudioStore

WEB_ROOT = Path(__file__).resolve().parent.parent / "web"
DRAFT_TTL_SECONDS = 6 * 3600


class Entity(BaseModel):
    legal_name: str = Field(min_length=1, max_length=200)
    short_name: str = Field(default="", max_length=100)
    credit_code: str = Field(default="", max_length=40)
    website: str = Field(default="", max_length=300)
    founded: str = Field(default="", max_length=20)
    location: str = Field(default="", max_length=100)
    industry: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=500)
    stage: str = Field(default="", max_length=60)
    aliases: list[str] = Field(default_factory=list, max_length=12)
    entity_source: Literal["registry", "ai_search", "manual"] = "manual"

    @field_validator('legal_name')
    @classmethod
    def company_name(cls, value):
        from project_watch.studio.business_identity import generic_name

        value = value.strip()
        if not value or generic_name(value):
            raise ValueError('请填写公司名称')
        return value


class CompanyNameIntent(BaseModel):
    confirmed_company_names: list[Annotated[str, Field(min_length=1, max_length=200)]] | None = Field(default=None, max_length=12)

    @field_validator('confirmed_company_names')
    @classmethod
    def explicit_names(cls, values):
        from project_watch.studio.business_identity import generic_name

        if values is None:
            return None
        if any(not n.strip() or generic_name(n) for n in values):
            raise ValueError('公司简称不能是泛称')
        return list(dict.fromkeys(n.strip() for n in values))


class ProjectCreate(CompanyNameIntent):
    execution_mode: Literal['api', 'agent'] = 'api'
    entity: Entity
    consent: bool
    draft_ids: list[str] = Field(default_factory=list, max_length=10)
    watchpoints: list[Annotated[str, Field(min_length=2, max_length=200)]] = Field(default_factory=list, max_length=3)


class ResolveRequest(BaseModel):
    name: str = Field(default="", max_length=200)
    draft_id: str | None = Field(default=None, max_length=64)


class ProjectPatch(CompanyNameIntent):
    note: str | None = Field(default=None, max_length=5000)
    status: Literal["active", "paused"] | None = None
    entity: Entity | None = None

    @model_validator(mode='after')
    def names_require_entity(self):
        if self.confirmed_company_names is not None and self.entity is None:
            raise ValueError('公司名称修改须同时提供公司主体')
        return self


class ItemPatch(BaseModel):
    status: Literal["new", "read", "starred", "dismissed"]


class RefreshRequest(BaseModel):
    kind: Literal["internal", "external", "external_followup", "brief", "public_profile", "landscape", "business", "competition", "details", "commercial", "insight"] = "external"


class TopicRequest(BaseModel):
    watch_id: str = Field(min_length=1, max_length=64)
    expected_version: str = Field(min_length=1, max_length=64)


class RecoveryRequest(BaseModel):
    expected_version: str = Field(min_length=1, max_length=64)
    idempotency_key: str = Field(min_length=8, max_length=100)
    accept_duplicate_cost: bool = Field(strict=True)


class DirectRetryRequest(BaseModel):
    accept_duplicate_cost: bool = Field(strict=True)


class CommercialPreference(BaseModel):
    investment_status: Literal['watching', 'invested']


class TechRoutes(BaseModel):
    terms: list[str] | None = Field(default=None, max_length=4)
    own: str | None = Field(default=None, max_length=20)


def attach_studio(app: FastAPI, *, data_dir: Path, start_worker: bool) -> None:
    @app.exception_handler(SettingsUnavailable)
    async def settings_unavailable(_request, exc):
        return JSONResponse(status_code=503, content={
            "code": "settings_unavailable", "detail": str(exc)})

    @app.exception_handler(OperationBlocked)
    async def operation_blocked(_request, exc):
        return JSONResponse(status_code=409, content={"code": "operation_held",
            "detail": str(exc), "operation_id": exc.operation_id, "operation_status": exc.status})

    worker_state = app.state.intake_worker
    repository = worker_state.repository
    blob_store = worker_state.blob_store
    store = StudioStore(repository.db_path)
    settings_store = SettingsStore(data_dir / "settings.json")
    from project_watch.studio import plan_templates

    plan_templates.load_local(data_dir / "plan_templates")  # instance-local industry methods (e.g. converted NEI)
    pipeline = Pipeline(repository, store, settings_store)
    pipeline.monitoring_plan.migrate_framework()  # once: earlier eight sections → four rings, nine dimensions
    # Only the composition root may import optional private implementations.
    validator = None
    try:
        import project_watch.studio.brief_analysis as brief_analysis_plugin
        import project_watch.studio.crosscheck_analysis as crosscheck_analysis_plugin
        import project_watch.studio.insight_generation as insight_generation_plugin
        import project_watch.studio.landscape as landscape_plugin
        import project_watch.studio.plan_replan as plan_replan_plugin
        import project_watch.studio.race as race_plugin
        import project_watch.studio.red_flags as red_flags_plugin
        import project_watch.studio.techroutes as techroutes_plugin
        import project_watch.studio.track as track_plugin
        from project_watch.studio.competition import assess_relation
        from project_watch.studio.competition_research import CompetitionResearch
        from project_watch.studio.question_advisor import ModelQuestionAdvisor
    except ModuleNotFoundError as exc:
        optional = {'competition', 'competition_evaluation', 'competition_research', 'landscape',
                    'product_matching', 'question_advisor', 'plan_replan', 'race', 'red_flags', 'registry_peers',
                    'techroutes', 'track', 'insight_generation', 'brief_analysis', 'crosscheck_analysis'}
        if exc.name not in {'project_watch.studio.' + name for name in optional}:
            raise  # Broken Core/third-party dependencies are not an absent plugin.
    else:
        pipeline.competition_research = CompetitionResearch(pipeline)
        pipeline.landscape_builder = landscape_plugin.build
        pipeline.landscape_projection = landscape_plugin
        pipeline.question_advisor = ModelQuestionAdvisor()
        pipeline.plan_reviser = plan_replan_plugin
        pipeline.insight_generator = insight_generation_plugin
        pipeline.brief_analysis = brief_analysis_plugin
        pipeline.crosscheck_analysis = crosscheck_analysis_plugin
        pipeline.insight_available = all((race_plugin, red_flags_plugin, track_plugin, techroutes_plugin))
        validator = assess_relation
        pipeline.relationships.assessment_validator = validator
    studio_worker = StudioWorker(pipeline)
    app.state.studio_store = store
    app.state.studio_pipeline = pipeline
    app.state.studio_worker = studio_worker
    from project_watch.studio.commercial import CommercialStore
    from project_watch.studio.commercial import targets as commercial_targets

    commercial_store = CommercialStore(store)
    application = StudioApplication(store)
    application.relationships.assessment_validator = validator
    from project_watch.studio.materials import MaterialService

    application.materials = MaterialService(application, repository, blob_store, settings_store)
    app.state.studio_application = application
    attach_agent_routes(app, application)
    drafts: dict[str, dict] = {}
    drafts_lock = threading.Lock()
    draining = threading.Event()
    active_writes = 0

    @app.middleware('http')
    async def drain_admission(request, call_next):
        nonlocal active_writes
        is_write = request.url.path.startswith('/api/') and request.method not in ('GET', 'HEAD', 'OPTIONS')
        counted = is_write and request.url.path != '/api/studio/runtime/drain'
        if (draining.is_set() and request.url.path.startswith('/api/')
                and request.method not in ('GET', 'HEAD', 'OPTIONS')
                and request.url.path != '/api/studio/runtime/drain'):
            return JSONResponse(status_code=503, content={'detail': '正在停止后台，请稍后重新启动再操作。',
                                                          'code': 'runtime_draining'})
        if counted:
            active_writes += 1
        try:
            return await call_next(request)
        finally:
            if counted:
                active_writes -= 1

    original = app.router.lifespan_context

    from contextlib import asynccontextmanager

    from project_watch.studio.host import instance_lock

    @asynccontextmanager
    async def lifespan(inner_app):
        from project_watch.studio.updates import source_runtime_lock

        with source_runtime_lock(), instance_lock(data_dir):
            pipeline.direct_operations.recover_interrupted()
            async with original(inner_app):
                if start_worker:
                    studio_worker.start()
                try:
                    yield
                finally:
                    drain_runtime()
                    # Manual stepping can also open the input-version observer.
                    # Always release it, even when no background thread started.
                    studio_worker.stop()
            # Do not release the home lock while a slow writer can still commit.
            # A supervisor may terminate its owned process tree if it cannot drain.
            import asyncio
            while not runtime_health()['drained']:
                await asyncio.sleep(.2)

    app.router.lifespan_context = lifespan

    def project_or_404(project_id: str) -> dict:
        project = store.get_project(project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        return project

    def require_insight(project_id: str) -> None:
        project_or_404(project_id)
        if not pipeline.insight_available:
            raise HTTPException(503, {'code': 'insight_unavailable',
                                     'message': '未安装投资分析扩展；基础监控仍可使用。'})

    # ------------------------------------------------------------- settings
    @app.get("/api/studio/settings")
    def get_settings():
        return SettingsStore.public_view(settings_store.load())

    @app.put("/api/studio/settings")
    def put_settings(body: dict):
        try:
            return SettingsStore.public_view(settings_store.update(body))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/studio/settings/executor")
    def get_executor():
        from project_watch.studio import public_executor

        return public_executor.current(data_dir)

    @app.put("/api/studio/settings/executor")
    def put_executor(body: dict):
        from project_watch.studio import public_executor

        if type(body.get('enabled')) is not bool:
            raise HTTPException(422, 'enabled 必须是 true 或 false')
        if not public_executor.current(data_dir)['configurable']:
            raise HTTPException(409, {'code': 'executor_not_configurable',
                                      'message': '当前以开发方式运行，执行方式由环境变量决定。'})
        # Saved only; the running worker keeps its executor until the app restarts.
        public_executor.write_choice(data_dir, body['enabled'])
        return public_executor.current(data_dir)

    @app.get('/api/studio/projects/{project_id}/commercial')
    def commercial_view(project_id: str):
        project = project_or_404(project_id)
        return commercial_store.view(project_id, settings_store.load().commercial,
                                     targets=commercial_targets(pipeline, project))

    @app.get('/api/studio/projects/{project_id}/race')
    def race_view(project_id: str, template: str | None = None):
        """Stored public company/peer records with scope, not company-wide rankings."""
        require_insight(project_id)
        from project_watch.studio import race
        from project_watch.studio.comparisons import with_originals
        from project_watch.studio.event_state import timeline
        from project_watch.studio.observations import read as observations

        project = project_or_404(project_id)
        plan = store.get_plan(project_id)
        commercial = commercial_store.view(project_id, settings_store.load().commercial,
                                           targets=commercial_targets(pipeline, project))
        company_events = store.list_events(project_id=project_id, category="company", since_days=730, limit=200)
        competitor_events = store.list_events(project_id=project_id, category="competitor", since_days=730, limit=300)
        return race.build(
            project=project, plan=plan,
            peers=race.peers_for(project, plan, application.relationships.view(project_id, include_internal=False),
                                 public_objects=application.objects.page(project_id, include_internal=False, current_aliases_only=True)),
            company_events=company_events,
            competitor_events=competitor_events,
            quantity_events=with_originals(store, project_id, [*company_events, *competitor_events]),
            threads=pipeline.monitor.threads(project_id),
            event_states=timeline(application, project_id, include_internal=False)["items"],
            commercial_records=commercial["records"],
            rd_items=[*store.list_items(project_id=project_id, category="paper", limit=200),
                      *store.list_items(project_id=project_id, category="patent", limit=200)],
            template=template, public_facts=(store.get_public_profile(project_id) or {}).get("facts") or [],
            relations=observations(application, project_id, limit=100, kind='business_relation')['items'])

    @app.get('/api/studio/projects/{project_id}/commitments')
    def commitments_view(project_id: str):
        from project_watch.studio.commitments import read

        project_or_404(project_id)
        return read(application, project_id)

    @app.get('/api/studio/projects/{project_id}/red-flags')
    def red_flags_view(project_id: str):
        """Bad signals for the company and its competitors (rules only, stored records)."""
        require_insight(project_id)
        from project_watch.studio import race, red_flags
        from project_watch.studio import signals as sig
        from project_watch.studio.attention import documents_lookup
        from project_watch.studio.event_state import timeline

        project = project_or_404(project_id)
        monitor = pipeline.monitor
        from project_watch.studio.commitments import read as promises

        return red_flags.build(
            project=project,
            peers=race.peers_for(project, store.get_plan(project_id), application.relationships.view(project_id, include_internal=False),
                                 public_objects=application.objects.page(project_id, include_internal=False, current_aliases_only=True)),
            company_events=store.list_events(project_id=project_id, category="company", since_days=red_flags.WINDOW_DAYS, limit=300),
            competitor_events=store.list_events(project_id=project_id, category="competitor", since_days=red_flags.WINDOW_DAYS, limit=300),
            event_states=timeline(application, project_id, include_internal=False)["items"],
            site_changes=monitor.changes(project_id, 40), site_pages=monitor.pages(project_id),
            silence=sig.silence(store, monitor, project_id, documents_lookup(pipeline)(project_id),
                                base_hours=settings_store.load().refresh_hours),
            commitments=promises(application, project_id)['items'])

    @app.get('/api/studio/projects/{project_id}/track')
    def track_view(project_id: str):
        """赛道四格: demand, capital, price, policy (rules over stored, cited records)."""
        require_insight(project_id)
        from project_watch.studio import track

        project = project_or_404(project_id)
        landscape = store.get_landscape(project_id) or {}
        commercial = commercial_store.view(project_id, settings_store.load().commercial,
                                           targets=commercial_targets(pipeline, project))
        return track.build(
            project=project, industry=landscape.get("industry") if isinstance(landscape.get("industry"), dict) else None,
            industry_events=store.list_events(project_id=project_id, category="industry", since_days=560, limit=400),
            competitor_events=store.list_events(project_id=project_id, category="competitor", since_days=560, limit=400),
            policy_events=store.list_events(project_id=project_id, category="policy", since_days=560, limit=200),
            commercial_records=commercial["records"])

    def _tech_view(project_id: str) -> dict:
        require_insight(project_id)
        from project_watch.studio import race, techroutes

        project = project_or_404(project_id)
        plan = store.get_plan(project_id)
        landscape = store.get_landscape(project_id) or {}
        docs = [e for cat in ("company", "industry", "competitor", "paper", "patent")
                for e in store.list_events(project_id=project_id, category=cat, since_days=560, limit=300)]
        profile = store.get_public_profile(project_id) or {}
        self_texts = [race._event_title(e)[0] for e in docs if e.get("category") == "company"]
        self_texts += [str(profile.get(k) or "") for k in ("one_liner", "summary")] + [project.get("description") or ""]
        peers = [p["name"] for p in race.peers_for(project, plan, application.relationships.view(project_id, include_internal=False),
                                                 public_objects=application.objects.page(project_id, include_internal=False, current_aliases_only=True))]
        return techroutes.view(project=project, saved=techroutes.load(store, project_id),
                               industry=landscape.get("industry") if isinstance(landscape.get("industry"), dict) else None,
                               docs=docs, self_texts=self_texts, peers=peers,
                               papers=store.list_items(project_id=project_id, category="paper", limit=300),
                               patents=store.list_items(project_id=project_id, category="patent", limit=300))

    @app.get('/api/studio/projects/{project_id}/tech')
    def tech_view(project_id: str):
        """技术路线与新面孔 (rules over stored reports, papers and patents)."""
        return _tech_view(project_id)

    @app.put('/api/studio/projects/{project_id}/tech')
    def tech_save(project_id: str, body: TechRoutes):
        require_insight(project_id)
        from project_watch.studio import techroutes

        project_or_404(project_id)
        techroutes.save(store, project_id, body.terms if body.terms is not None else [], body.own)
        if not body.terms:
            store.set_state(techroutes.STATE_KEY.format(project_id), "{}")  # back to system suggestions
        return _tech_view(project_id)

    @app.put('/api/studio/projects/{project_id}/commercial/preference')
    def commercial_preference(project_id: str, body: CommercialPreference):
        project_or_404(project_id)
        commercial_store.set_focus(project_id, body.investment_status)
        return {'investment_status': body.investment_status}

    def run_direct(kind, inputs, config, callback, response, *, commit=None):
        result, operation_id, replayed = pipeline.direct_operations.run(kind, inputs, config, callback, commit=commit)
        response.headers['X-ProjectWatch-Operation'] = operation_id
        response.headers['X-ProjectWatch-Replayed'] = str(replayed).lower()
        return result

    @app.get('/api/studio/operations')
    def direct_operations():
        return pipeline.direct_operations.list()

    @app.post('/api/studio/operations/{operation_id}/retry')
    def retry_direct_operation(operation_id: str, body: DirectRetryRequest):
        try:
            return pipeline.direct_operations.authorize_retry(operation_id,
                accept_duplicate_cost=body.accept_duplicate_cost)
        except KeyError as exc:
            raise HTTPException(404, '没有这项操作') from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/studio/settings/test")
    def test_settings(response: Response, scope: Literal['all', 'setup'] = 'all'):
        config = settings_store.load()
        if scope == 'setup' and len(config.search.enabled()) != 1:
            raise HTTPException(422, '首次配置请选择一个搜索服务。')
        # Both screens share one receipt: switching screens must not bypass an
        # uncertain paid request. A fresh cached receipt contains only tested services.
        return run_direct('settings_test', {}, config, lambda: _test_settings(config, scope), response)

    def _test_settings(config, scope='all'):
        _settings, _http, llm, collectors = pipeline.tools(config)
        result: dict[str, dict] = {}

        def failure_message(exc):
            # Fixed local copy: exception detail can contain URLs or credentials.
            messages = {
                'search_key_invalid': '搜索 Key 无效，请检查并重新保存 Key。',
                'search_key_missing': '尚未填写搜索 Key，请在设置中填写并保存。',
                'search_query_invalid': '搜索词长度或返回条数不符合服务要求，请缩短检索词。',
                'search_access_denied': '搜索服务没有访问权限，请检查账号和套餐权限。',
                'search_quota_exhausted': '搜索额度已用完，请到服务商充值或切换搜索服务。',
                'model_access_denied': '模型 Key 无效或没有权限，请检查 Key 和模型权限。',
                'model_content_rejected': '模型拒绝本次内容，请检查内容或切换模型；未自动重发。',
                'connection_failed': '网络连接失败，请检查网络和代理设置。',
                'request_outcome_unknown': '请求结果不明，请先查看服务商记录，不要重复点击测试。',
                'model_timeout': '模型响应超时，请先查看服务商记录，不要重复点击测试。',
                'provider_backoff': '服务暂时暂停，请检查额度和服务状态后再测试。',
            }
            return messages.get(getattr(exc, 'code', ''), '连接测试失败，请检查服务地址、网络和权限。')

        if llm.ready:
            try:
                started = time.monotonic()
                llm.complete("你是连接测试。", "请只回复：OK", max_tokens=1024)
                result["model"] = {"ok": True,
                                   "message": f"连接正常（{time.monotonic() - started:.1f} 秒）"}
            except (ModelError, HttpError) as exc:
                result["model"] = {"ok": False, "message": failure_message(exc)}
        else:
            result["model"] = {"ok": False, "message": "尚未填写模型服务"}
        if scope == 'all':
            try:
                news = collectors.bing_news("人工智能")
                result["news"] = {"ok": True, "message": f"Bing 新闻可用（{len(news)} 条）"}
            except (HttpError, ValueError):
                result["news"] = {"ok": False, "message": "必应新闻不可达，请检查网络；也可配置搜索服务。"}
        if scope == 'all' and _settings.search.google_news:
            try:
                news = collectors.google_news("人工智能")
                result["google_news"] = {"ok": True, "message": f"Google News 可用（{len(news)} 条，仅标题）"}
            except (HttpError, ValueError):
                result["google_news"] = {"ok": False, "message": "Google News 不可达，请检查网络或关闭此来源。"}
        if _settings.search.enabled():
            keys = _settings.search.credentials()
            for provider in _settings.search.enabled():
                label = f"search_{provider}"
                if not keys.get(provider):
                    result[label] = {"ok": False, "message": "尚未填写 API Key"}
                    continue
                try:
                    service = Collectors(_http, SearchConfig(provider, keys[provider]))
                    hits = service.web_search("人工智能 投资", count=3)
                    result[label] = {"ok": True, "message": f"连接正常（{len(hits)} 条）"}
                except (HttpError, ValueError) as exc:
                    result[label] = {"ok": False, "message": failure_message(exc)}
        else:
            result["search"] = {"ok": None, "message": "未配置搜索服务，将使用必应新闻；可在设置中添加。"}
        return result

    # ------------------------------------------------------------- digest & push
    def _digest_days(days: int) -> int:
        if days not in (7, 14, 30):
            raise HTTPException(400, "只支持 7/14/30 天")
        return days

    def _digest(days: int) -> dict:
        from project_watch.studio import attention

        days = _digest_days(days)
        return {**dg.build_digest(store, days), "attention": attention.for_pipeline(pipeline, min(days, 30))}

    @app.get("/api/studio/digest")
    def digest(days: int = 7):
        data = _digest(days)
        last = store.get_state("notify_last")
        return {**data, "markdown": dg.digest_markdown(data),
                "notify": {"ready": settings_store.load().notify.ready,
                           "last": json.loads(last) if last else None}}

    @app.get("/api/studio/digest.md")
    def digest_file(days: int = 7):
        data = _digest(days)
        name = f"ProjectWatch-组合动态-{time.strftime('%Y%m%d')}.md"
        return PlainTextResponse(dg.digest_markdown(data), media_type="text/markdown; charset=utf-8", headers={
            "Content-Disposition": f"attachment; filename=\"ProjectWatch-digest.md\"; filename*=UTF-8''{quote(name)}"})

    def _push(title: str, markdown: str) -> dict:
        settings = settings_store.load()
        if not settings.notify.ready:
            raise HTTPException(400, "尚未配置推送渠道")
        try:
            dg.send(pipeline.http_factory(settings), settings.notify, title, markdown)
        except HttpError as exc:
            return {"ok": False, "message": f"推送失败：{exc.code} {exc}".strip()}
        return {"ok": True, "message": "已推送到群机器人"}

    @app.post("/api/studio/notify/test")
    def notify_test():
        return _push("ProjectWatch 测试", "# ProjectWatch 推送测试\n推送渠道已连通。之后的周报与重大事件会发到这个群。\n")

    @app.post("/api/studio/digest/push")
    def digest_push(days: int = 7):
        data = _digest(days)
        return _push("ProjectWatch 组合动态", dg.digest_markdown(data, compact=True))

    @app.get("/api/studio/usage")
    def usage():
        rows = store.usage(30)
        return {"days": rows,
                "total": {k: sum(r[k] for r in rows) for k in ("calls", "input_tokens", "output_tokens",
                    "known_calls", "unknown_calls", "legacy_calls", "input_known_calls", "output_known_calls",
                    "cache_read_tokens", "cache_creation_tokens")}}

    @app.get('/api/studio/projects/{project_id}/usage')
    def project_usage(project_id: str, limit: int = 30):
        from project_watch.studio.usage import jobs

        project_or_404(project_id)
        return {'jobs': jobs(store, project_id, limit=limit), 'token_budget': None}

    # ------------------------------------------------------------- onboarding
    @app.post("/api/studio/drafts")
    def upload_draft(file: UploadFile = File(...)):
        from project_watch.extraction_process import extract_document_subprocess
        from project_watch.extractors import ExtractionError

        stored = blob_store.put_stream(file.filename or "", file.file)
        try:
            result = extract_document_subprocess(stored.path, suffix=stored.suffix, cancel=draining)
            text = "\n".join(f.text for f in result.fragments)
        except (ExtractionError, OSError, ValueError):
            text = ""
        with drafts_lock:
            cutoff = time.time() - DRAFT_TTL_SECONDS
            for key in [k for k, v in drafts.items() if v["at"] < cutoff]:
                drafts.pop(key)
            drafts[stored.sha256] = {"blob": stored, "text": text, "at": time.time()}
        return {"draft_id": stored.sha256, "filename": stored.display_name,
                "size": stored.size, "readable": bool(text.strip()), "excerpt": text[:400]}

    @app.post("/api/studio/resolve")
    def resolve(body: ResolveRequest, response: Response):
        config = settings_store.load()
        bp_text = ""
        if body.draft_id:
            draft = drafts.get(body.draft_id)
            if not draft:
                raise HTTPException(404, "上传的文件已过期，请重新上传")
            bp_text = draft["text"]
        name = ' '.join(body.name.split())
        if not name and not bp_text.strip():
            raise HTTPException(422, '请输入公司名称，或上传 BP')

        def execute():
            _settings, http, llm, collectors = pipeline.tools(config)
            try:
                return resolve_company(name, bp_text=bp_text, llm=llm, collectors=collectors,
                                       http=http, registry=_settings.registry)
            except ModelError as exc:
                raise HTTPException(502, str(exc)) from exc
        return run_direct('resolve', {'name': name, 'bp': bp_text}, config, execute, response)

    @app.post("/api/studio/projects", status_code=201)
    def create_project(body: ProjectCreate, response: Response):
        if not body.consent:
            raise HTTPException(422, "需要确认授权后才能启动监控")
        entity = body.entity.model_dump()
        display = entity["short_name"] or entity["legal_name"]
        config = settings_store.load()
        points = list(dict.fromkeys(t.strip() for t in body.watchpoints if len(t.strip()) >= 2))
        with drafts_lock:
            attachments = [drafts[key]['blob'] for key in body.draft_ids if key in drafts]

        def prepare():
            from project_watch.studio.monitor import build_watchpoints

            if len(attachments) != len(body.draft_ids):
                raise HTTPException(404, '上传的文件已过期，请重新上传')
            _settings, _http, llm, _collectors = pipeline.tools(config)
            return build_watchpoints(llm, entity, points)

        def commit(specs, db):
            project = repository.create_project(display, db=db)
            existing = db.execute('SELECT execution_mode FROM studio_projects WHERE project_id=?', (project.id,)).fetchone()
            if existing and (body.execution_mode == 'agent' or existing[0] == 'agent'):
                raise HTTPException(409, '同名项目已存在，请选择已有项目；本次不会改变其配置或暂停状态')
            store.upsert_project(project.id, entity, consent=True, db=db)
            if body.execution_mode == 'agent':
                db.execute("UPDATE studio_projects SET execution_mode='agent',status='paused' WHERE project_id=?", (project.id,))
            pipeline.objects.confirm_subject_identity(project.id, entity['legal_name'],
                body.confirmed_company_names, db=db)
            for text, spec in zip(points, specs, strict=True):
                pipeline.monitor.add_watchpoint(project.id, text, spec['label'], spec['queries'], spec['criteria'], True, db=db)
            for stored in attachments:
                source = repository.add_source(project.id, kind="document",
                                               reference=stored.display_name,
                                               content_sha256=stored.sha256, db=db)
                repository.enqueue_intake(project.id, source.id, db=db)
            store.enqueue(project.id, "external", db=db)
            return {'id': project.id}

        try:
            if body.execution_mode == 'agent':
                if len(attachments) != len(body.draft_ids):
                    raise HTTPException(404, '上传的文件已过期，请重新上传')
                if points:
                    raise HTTPException(422, '请连接 Agent 后设置监控范围')
                with closing(store._connect()) as db, db:
                    db.execute('BEGIN IMMEDIATE')
                    result = commit([], db)
                return project_or_404(result['id'])
            result = run_direct('create_project', body.model_dump(), config, prepare, response, commit=commit)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return project_or_404(result['id'])

    # ------------------------------------------------------------- projects
    @app.get("/api/studio/projects")
    def list_projects():
        from project_watch.studio.runtime_health import active_projections

        projects = store.list_projects()
        active = active_projections(store, worker=studio_worker.runtime_state())
        for p in projects:
            p['worker_state'] = studio_worker.runtime_state()
            p['worker_health'] = studio_worker.health()['state']
            p["nav_alerts"] = store.nav_alerts(p["id"])
            if p['id'] in active:
                p['job_status'] = active[p['id']]['status']
                p['job_runtime'] = active[p['id']]['runtime']
            brief = store.get_brief(p["id"])
            p["headline"] = brief.get("headline") if brief else ""
            p["tone"] = brief.get("tone") if brief else ""
            p["finance"] = finance.key_numbers(store.list_metrics(p["id"]))
        return projects

    @app.get("/api/studio/projects/{project_id}")
    def project_detail(project_id: str):
        from project_watch.studio import insight
        from project_watch.studio.agent_results import insight_projection
        from project_watch.studio.dossier_activity import saved_revisions
        from project_watch.studio.event_state import timeline
        from project_watch.studio.milestones import read as read_milestones
        from project_watch.studio.observations import read as read_observations
        from project_watch.studio.progress import read as read_progress
        from project_watch.studio.runtime_health import project_jobs

        project = project_or_404(project_id)
        intake = {j.source_id: j for j in repository.list_intake(project_id)}
        execution = {'analysis': studio_worker.runtime_state(), 'parsing': worker_state.runtime_state()}
        execution['health'] = studio_worker.health()
        project['worker_state'] = execution['analysis']
        project['worker_health'] = execution['health']['state']
        documents = [{
            "id": s.id, "name": s.reference, "created_at": s.created_at.isoformat(),
            "status": intake[s.id].status if s.id in intake else "unknown",
            "error": intake[s.id].error_code if s.id in intake else None,
            "fragments": len(repository.list_fragments(s.id)),
        } for s in repository.list_sources(project_id) if s.kind == "document"]
        plan = store.get_plan(project_id)
        metrics = store.list_metrics(project_id)
        detail_result = pipeline.structured_facts.get(project_id)
        from project_watch.studio.agent_results import project_projection
        from project_watch.studio.collection_dsh import latest_report

        return project_projection(store, project_id, {
            "project": project,
            "dossier_saved": saved_revisions(pipeline.monitor.watchpoints(project_id),
                                             insight_projection(store, project_id, insight.load(store, project_id))),
            "execution": execution,
            "capabilities": {"insight_generation": pipeline.insight_generator is not None,
                             "insight_projections": pipeline.insight_available},
            "monitoring_progress": read_progress(store, project_id),
            "profile": store.get_profile(project_id),
            "public_profile": store.get_public_profile(project_id),
            "trials": company_trials(project, store.list_items(project_id=project_id, category="trial", limit=100)),
            "brief": store.get_brief(project_id),
            "plan": plan,
            "collection_report": latest_report(store, project_id),
            "metrics": metrics,
            "finance": finance.view(metrics),
            "landscape": _landscape_view(project_id),
            "relationships": application.relationships.view(project_id),
            "detail_summary": ({'status': detail_result['status'], 'fact_count': len(detail_result['fact_ids']),
                                'rejected': detail_result.get('rejected', 0), 'unquantified': detail_result.get('unquantified', 0)}
                               if detail_result else None),
            "competition_research": pipeline.competition_research.saved(project_id),
            "observations": read_observations(application, project_id, include_internal=True),
            "event_states": timeline(application, project_id, include_internal=True),
            "milestones": read_milestones(application, project_id, include_internal=True),
            "concerns": application.concerns.read(project_id),
            "activity": store.activity(project_id),
            "issues": review.metric_issues(metrics),
            "doc_changes": store.doc_changes(project_id, 3),
            "body_coverage": store.body_coverage(project_id),
            "counts": store.category_counts(project_id),
            "jobs": project_jobs(store, project_id, worker=execution['analysis']),
            "documents": sorted(documents, key=lambda d: d["created_at"], reverse=True),
            "model_ready": settings_store.load().model.ready,
            # Current configuration, not the flags stored with the last round (those go stale after setup).
            "search_ready": bool(settings_store.load().search.ready_providers()),
            "registry_ready": settings_store.load().registry.provider == "tianyancha" and bool(settings_store.load().registry.api_key),
            "pending_documents": store.pending_documents(project_id),
            "ocr": ocr_capabilities(),
        })

    def _landscape_view(project_id: str) -> dict | None:
        """Stored competition/industry view, with the heat counts recomputed on read so they stay current."""
        ls = pipeline.landscape_projection

        data = store.get_landscape(project_id)
        if data:
            # Competitor moves are rule-classified from stored news: recompute so they include the latest reports.
            planned = [c["name"] for c in (store.get_plan(project_id) or {}).get("competitors", []) if c.get("name")]
            data["moves"] = ls.competitor_moves(store.list_events(project_id=project_id, category="competitor",
                                                                  since_days=180, limit=80), planned)
            if isinstance(data.get("competition"), dict):
                data["competition"] = ls.complete_competitors(data["competition"], planned, data["moves"])
        if data and isinstance(data.get("industry"), dict) and hasattr(ls, 'heat_signals'):
            data["industry"]["heat_index"] = ls.heat_signals(store, project_id, data["industry"].get("funding") or [],
                                                             data.get("moves") or [])
        elif data and isinstance(data.get('industry'), dict):
            data['industry'].pop('heat_index', None)
        return data

    @app.patch("/api/studio/projects/{project_id}")
    def patch_project(project_id: str, body: ProjectPatch):
        project = project_or_404(project_id)
        if body.entity is not None:
            entity = body.entity.model_dump()
            try:
                with closing(store._connect()) as db, db:
                    db.execute('BEGIN IMMEDIATE')
                    store.upsert_project(project_id, {**entity, 'entity_source':
                        entity.get('entity_source') or project.get('entity_source')}, consent=False, db=db)
                    pipeline.objects.confirm_subject_identity(project_id, entity['legal_name'],
                        body.confirmed_company_names, db=db)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
        if body.note is not None:
            store.set_note(project_id, body.note)
        if body.status is not None:
            application.change_status(project_id, body.status, expected_version=None,
                                      idempotency_key=uuid.uuid4().hex, actor="local_web")
        return store.get_project(project_id)

    @app.post("/api/studio/projects/{project_id}/activate")
    def activate(project_id: str):
        project = project_or_404(project_id)
        store.upsert_project(project_id, {**project, "legal_name": project.get("legal_name") or project["name"]},
                             consent=True)
        store.enqueue(project_id, "external")
        return store.get_project(project_id)

    @app.post("/api/studio/projects/{project_id}/files", status_code=202)
    def upload(project_id: str, file: UploadFile = File(...)):
        try:
            return application.materials.submit(project_id, file.filename or "", file.file)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/studio/projects/{project_id}/links", status_code=202)
    def add_link(project_id: str, body: dict):
        """A web page or online PDF, read once and kept as a document (see link_intake)."""
        import io

        from project_watch.studio import link_intake

        project_or_404(project_id)
        try:
            name, data = link_intake.fetch(pipeline.reader(), str(body.get("url") or ""))
        except link_intake.LinkError as exc:
            raise HTTPException(422, str(exc)) from exc
        return application.materials.submit(project_id, name, io.BytesIO(data))

    @app.post("/api/studio/projects/{project_id}/files/{source_id}/retry", status_code=202)
    def retry_file(project_id: str, source_id: str):
        project_or_404(project_id)
        job = next((j for j in repository.list_intake(project_id) if j.source_id == source_id), None)
        if job is None:
            raise HTTPException(404, "资料不存在")
        if job.status not in ("failed", "needs_ocr"):
            raise HTTPException(409, "这份资料已识别或正在处理中")
        retried = repository.retry_failed_intake(job.id)
        store.reset_analyzed(project_id)
        return {"status": retried.status}

    @app.get('/api/studio/projects/{project_id}/research-topic/{watch_id}')
    def research_topic_result(project_id: str, watch_id: str):
        from project_watch.studio.dsh_topic import latest_topic

        project_or_404(project_id)
        try:
            return latest_topic(store, project_id, watch_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, '专题结果暂时无法核验，请保留原任务并检查本地回执') from exc

    @app.post('/api/studio/projects/{project_id}/research-topic', status_code=202)
    def research_topic(project_id: str, body: TopicRequest):
        from project_watch.studio.dsh_runtime import runtime_entry
        from project_watch.studio.dsh_topic import enqueue_topic
        from project_watch.studio.public_executor import require_available

        project_or_404(project_id)
        try:
            require_available()
            root = os.environ.get('PROJECT_WATCH_DSH_RUNTIME')
            if not root or os.environ.get('PROJECT_WATCH_DSH_MONITOR') != '1':
                raise ValueError('专题研究需要启用 DSH 监控')
            if not runtime_entry(Path(root)).is_file():
                raise ValueError('DSH 运行时不可用')
            settings = settings_store.load()
            if not settings.model.ready or not settings.search.ready_providers():
                raise ValueError('请先配置模型与搜索服务')
            return enqueue_topic(pipeline, project_id, body.watch_id, body.expected_version)
        except (ValueError, OSError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/studio/projects/{project_id}/refresh", status_code=202)
    def refresh(project_id: str, body: RefreshRequest):
        project = project_or_404(project_id)
        if project.get('execution_mode') == 'agent':
            raise HTTPException(409, '此项目由已有 Agent 更新。请到“连接 Agent”复制研究指令，不会调用内置 API。')
        if not project.get("activated"):
            raise HTTPException(409, "项目尚未启动监控")
        if body.kind == 'external_followup':
            if project['status'] != 'active':
                raise HTTPException(409, '请先恢复监控')
            try:
                return store.retry_external_followup(project_id)
            except FollowupSuperseded:
                # Stale receipts are not resumed; the owner's rerun starts a round on current inputs.
                return store.enqueue(project_id, 'external')
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
        if body.kind == 'commercial':
            if project['status'] != 'active':
                raise HTTPException(409, '请先恢复监控')
            if not settings_store.load().commercial.ready_providers():
                raise HTTPException(409, '请在设置中启用专业数据来源')
            if not commercial_targets(pipeline, project):
                raise HTTPException(409, '请先确认公司的公开全称')
            return store.enqueue(project_id, 'commercial')
        if body.kind == 'competition':
            settings = settings_store.load()
            if project['status'] != 'active' or not settings.model.ready or not settings.search.ready_providers():
                raise HTTPException(409, '请先启用监控并连接模型与搜索服务')
            if not pipeline.competition_research.inputs(project_id)['queries']:
                raise HTTPException(409, '缺少有来源的产品用途与购买方信息')
            return store.enqueue(project_id, 'competition', input_version='manual:' + uuid.uuid4().hex)
        if body.kind == 'insight':
            if pipeline.insight_generator is None:
                raise HTTPException(503, {'code': 'insight_unavailable',
                                          'message': '当前安装未包含深度分析生成器；已有记录仍可查看'})
            if not settings_store.load().model.ready:
                raise HTTPException(409, '请先在设置中连接 AI 模型')
            from project_watch.studio import insight

            if insight.held(store, project_id):
                raise HTTPException(409, '该项目有结果不明的模型请求等待确认恢复；请先处理原任务')
            return store.enqueue(project_id, 'insight', input_version='manual:' + uuid.uuid4().hex)
        if body.kind == "internal":
            store.reset_analyzed(project_id)
        if body.kind == 'details':
            if project['status'] != 'active' or not settings_store.load().model.ready:
                raise HTTPException(409, '请先启用监控并连接模型')
            if not pipeline.structured_facts.inputs(project_id, pipeline.model_binding()):
                raise HTTPException(409, '暂无获准分析的来源')
            return store.enqueue(project_id, 'details', input_version='manual:' + uuid.uuid4().hex)
        return store.enqueue(project_id, body.kind)

    @app.get("/api/studio/tasks")
    def task_board(project_id: str | None = None):
        """任务看板: running, queued, finished and failed work, plus each uploaded file's progress."""
        from project_watch.studio import taskboard

        result = taskboard.board(store, worker_state=studio_worker.runtime_state(), model_ready=settings_store.load().model.ready,
                               model_binding=pipeline.model_binding(), project_id=project_id)
        result['runtime'] = runtime_health()
        return result

    @app.get('/api/studio/updates')
    def update_status():
        from project_watch.studio.updates import configured_check

        return configured_check()

    @app.post('/api/studio/updates/check')
    def check_updates():
        # Existing same-origin/CSRF boundary; no web apply/exec endpoint.
        from project_watch.studio.updates import configured_check

        return configured_check(refresh=True)

    @app.get('/api/studio/health')
    def runtime_health():
        # No names, prompts, keys, data-directory paths or network side effects.
        from project_watch.studio.maintenance import disk_health

        analysis, intake = studio_worker.health(), worker_state.health()
        storage = disk_health(data_dir)
        legacy_alive = any(w is not None and w._thread is not None and w._thread.is_alive()
                           for w in (app.state.monitoring_scheduler, app.state.reading_worker))
        result = {**analysis, 'parsing': intake, 'storage': storage, 'draining': draining.is_set(),
                  'active_writes': active_writes,
                  'drained': draining.is_set() and analysis['state'] == 'stopped' and intake['state'] == 'stopped'
                             and not intake['active'] and not legacy_alive and not active_writes}
        result['restart_required'] = analysis['restart_required'] or intake['restart_required']
        if draining.is_set():
            result.update(state='stopped' if result['drained'] else 'stopping', label='已停止' if result['drained'] else '正在停止',
                          restart_required=False)
        elif intake['restart_required']:
            result.update(state='stalled', label='资料处理受阻，等待恢复')
        elif storage['state'] != 'ok' and result['state'] == 'running':
            result.update(state='degraded', label=storage['label'])
        return result

    @app.post('/api/studio/runtime/drain')
    def drain_runtime():
        draining.set()
        studio_worker.request_stop()
        worker_state.request_stop()
        if app.state.monitoring_scheduler is not None:
            app.state.monitoring_scheduler.request_stop()
        if app.state.reading_worker is not None:
            app.state.reading_worker.request_stop()
        return runtime_health()

    @app.post("/api/studio/tasks/{job_id}/dismiss", status_code=204)
    def dismiss_task(job_id: str):
        """Quiet an incomplete outcome's reminder; never mark it successful or retry it."""
        from project_watch.studio.runtime_health import projection

        with closing(store._connect()) as db:
            row = db.execute("SELECT * FROM studio_jobs WHERE id=?", (job_id,)).fetchone()
            if not row or not (row['status'] == 'failed' or
                               (row['status'] == 'done' and projection(db, row)['state'] == 'partial')):
                raise HTTPException(404, "没有这个需要处理的任务")
        from project_watch.studio.store import now

        store.set_state(f"task_dismissed:{job_id}", now())

    @app.get("/api/studio/removed-projects")
    def removed_projects():
        """Projects taken off the watchlist, so the portfolio page can offer to restore them."""
        return [{k: p.get(k) for k in ("id", "name", "legal_name", "industry", "stage", "updated_at")}
                for p in store.list_projects(include_removed=True) if p.get("status") == "removed"]

    @app.delete("/api/studio/projects/{project_id}", status_code=204)
    def remove_project(project_id: str):
        """移出关注: hide the project and stop its automatic work; its data stays on this machine and can be restored."""
        project_or_404(project_id)
        store.remove_project(project_id)

    @app.post("/api/studio/projects/{project_id}/restore")
    def restore_project(project_id: str):
        project_or_404(project_id)
        store.restore_project(project_id)
        return store.get_project(project_id)

    @app.get('/api/studio/projects/{project_id}/insight')
    def insight_view(project_id: str):
        """Stored deep analysis (AI judgments with numbered sources), its history and the latest job state."""
        from project_watch.studio import insight

        project_or_404(project_id)
        with closing(store._connect()) as db:
            job = db.execute("SELECT id, status, error, created_at, started_at, finished_at FROM studio_jobs "
                             "WHERE project_id=? AND kind='insight' ORDER BY created_at DESC, rowid DESC LIMIT 1", (project_id,)).fetchone()
        from project_watch.studio.agent_results import insight_history, insight_projection

        return {"record": insight_projection(store, project_id, insight.load(store, project_id)), "history": insight_history(store, project_id, insight.history(store, project_id)),
                "job": dict(job) if job else None, "model_ready": settings_store.load().model.ready,
                "generation_available": pipeline.insight_generator is not None,
                "labels": insight.VERDICTS, "sources": {**insight.SOURCE_LABEL, 'E':'公开来源（Agent）'}}

    @app.get('/api/studio/projects/{project_id}/events/{event_id}/context')
    def event_context_view(project_id: str, event_id: str):
        """Why one event matters: source excerpts → plan points it hit → assessments and AI judgment points citing it."""
        from project_watch.studio import event_context, insight
        from project_watch.studio.agent_results import insight_projection

        project_or_404(project_id)
        record = insight_projection(store, project_id, insight.load(store, project_id))
        found = event_context.build(store, pipeline.monitor, project_id, event_id, record, insight.VERDICTS)
        if found is None:
            raise HTTPException(404, "事件不存在")
        return found

    @app.get('/api/studio/projects/{project_id}/recovery')
    def project_recovery_preview(project_id: str):
        from project_watch.studio.recovery import project_preview

        project_or_404(project_id)
        return project_preview(pipeline, project_id)

    @app.get('/api/studio/projects/{project_id}/jobs/{job_id}/recovery')
    def recovery_preview(project_id: str, job_id: str):
        from project_watch.studio.recovery import preview

        project_or_404(project_id)
        try:
            return preview(pipeline, project_id, job_id)
        except KeyError as exc:
            raise HTTPException(404, '任务不存在') from exc

    @app.post('/api/studio/projects/{project_id}/jobs/{job_id}/recovery', status_code=202)
    def recovery_authorize(project_id: str, job_id: str, body: RecoveryRequest):
        from project_watch.studio.recovery import authorize

        project_or_404(project_id)
        try:
            return authorize(pipeline, project_id, job_id, **body.model_dump())
        except KeyError as exc:
            raise HTTPException(404, '任务不存在') from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    # ------------------------------------------------------------- feed
    @app.get("/api/studio/feed")
    def feed(project_id: str | None = None, category: str | None = None,
             importance: str | None = None, days: int | None = None,
             limit: int = 50, offset: int = 0):
        limit = max(1, min(limit, 200))
        return store.list_items(project_id=project_id, category=category, importance=importance,
                                since_days=days, limit=limit, offset=max(0, offset))

    @app.get("/api/studio/events")
    def events(project_id: str | None = None, category: str | None = None,
               importance: str | None = None, days: int | None = None,
               limit: int = 50, offset: int = 0):
        limit = max(1, min(limit, 200))
        return store.list_events(project_id=project_id, category=category, importance=importance,
                                 since_days=days, limit=limit, offset=max(0, offset))

    @app.patch("/api/studio/events/{event_id}")
    def patch_event(event_id: str, body: ItemPatch):
        try:
            store.set_event_status(event_id, body.status)
        except KeyError as exc:
            raise HTTPException(404, "事件不存在") from exc
        return {"ok": True}

    @app.patch("/api/studio/items/{item_id}")
    def patch_item(item_id: str, body: ItemPatch):
        try:
            store.set_item_status(item_id, body.status)
        except KeyError as exc:
            raise HTTPException(404, "信息不存在") from exc
        return {"ok": True}

    # ------------------------------------------------------------- web UI
    index = WEB_ROOT / "index.html"
    if (WEB_ROOT / "assets").is_dir():
        app.mount("/assets", ProductStaticFiles(directory=WEB_ROOT / "assets"), name="studio-assets")

    def spa():
        if index.is_file():
            return FileResponse(index, media_type="text/html", headers={"Cache-Control": "no-store"})
        return HTMLResponse("<h1>ProjectWatch</h1><p>前端尚未构建：请在 frontend 目录运行 npm run build。</p>")

    # Replace the classic page at "/" and keep it reachable at /classic.
    app.router.routes[:] = [r for r in app.router.routes
                            if not (getattr(r, "path", None) == "/" and "GET" in getattr(r, "methods", set()))]
    classic = Path(__file__).resolve().parent.parent / "static" / "index.html"
    def classic_page():
        if not getattr(app.state, 'classic_available', True):
            raise HTTPException(503, '此安装包仅提供 Studio，未包含旧版界面。')
        return FileResponse(classic, media_type="text/html")

    app.add_api_route("/classic", classic_page, include_in_schema=False,
                      response_model=None)
    app.add_api_route("/", spa, include_in_schema=False, response_model=None)
    from project_watch.studio.monitor_api import register_monitor_routes

    register_monitor_routes(app, store, pipeline)
    from project_watch.studio.attention import register_attention_routes

    register_attention_routes(app, pipeline)

    for path in ("/feed", "/projects", "/projects/{rest:path}", "/new", "/settings", "/setup", "/agents", "/digest", "/tasks"):
        app.add_api_route(path, spa, include_in_schema=False, response_model=None)


def default_data_dir() -> Path:
    configured = os.environ.get("PROJECT_WATCH_HOME")
    if configured:
        return Path(configured)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / "ProjectWatch"


def create_studio_app(data_dir: Path | str | None = None, *, start_worker: bool = True,
                      classic: bool | None = None) -> FastAPI:
    """Full installs retain classic; a physically absent classic API uses Studio only."""
    from project_watch.studio.host import instance_lock
    from project_watch.studio.local_base import create_local_base
    from project_watch.studio.updates import source_runtime_lock

    create_app = None
    if classic is not False:
        try:
            from project_watch.api import create_app
        except ModuleNotFoundError as exc:
            # Never turn an installed-but-broken classic dependency into a quiet fallback.
            if classic is True or exc.name != 'project_watch.api':
                raise

    home = Path(data_dir) if data_dir else default_data_dir()
    home.mkdir(parents=True, exist_ok=True)
    # Serialize initialization/DDL as well as the later runtime lifespan. A live
    # instance or a full-home backup must block construction before any DB write.
    with source_runtime_lock(), instance_lock(home):
        if create_app is None:
            app = create_local_base(home / 'projectwatch.sqlite3', start_worker=start_worker)
        else:
            app = create_app(home / "projectwatch.sqlite3", start_worker=start_worker, isolated_intake=True)
            app.state.classic_available = True
        attach_studio(app, data_dir=home, start_worker=start_worker)
    return app


def create_studio_app_from_env() -> FastAPI:
    return create_studio_app()
