"""Purpose-separated local disclosure and consent for saved external analysis."""
import hmac
import os
from dataclasses import asdict

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from project_watch.external_analysis import OUTPUT_TOKENS, RULE_VERSION, Selection, digest
from project_watch.external_article_packet import ARTICLE_RULE_VERSION
from project_watch.reading_authorization_api import ReadingAuthorizationStatus
from project_watch.reading_jobs import ReadingDenied, ReadingLimits


class ExternalScopeInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    plan_ids: list[str] = Field(min_length=1, max_length=20)
    selections: list[Selection] = Field(default_factory=list, max_length=3)
    include_future_news: bool = Field(default=False, strict=True)
    read_articles: bool = Field(default=False, strict=True)
    max_requests: int = Field(default=20, ge=1, le=1000, strict=True)
    max_cost_cny: str | None = Field(default=None, max_length=40)
    input_cny_per_million: str | None = Field(default=None, max_length=40)
    output_cny_per_million: str | None = Field(default=None, max_length=40)

    def limits(self):
        return ReadingLimits(max_requests=self.max_requests, max_output_tokens=OUTPUT_TOKENS,
                             max_cost_cny=self.max_cost_cny, input_cny_per_million=self.input_cny_per_million,
                             output_cny_per_million=self.output_cny_per_million)


class ExternalConsentInput(ExternalScopeInput):
    consent: bool = Field(strict=True)
    request_key: str = Field(min_length=16, max_length=100)
    disclosure_digest: str = Field(pattern=r'^[0-9a-f]{64}$')


class ExternalRetryInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    consent: bool = Field(strict=True)
    disclosure_digest: str = Field(pattern=r'^[0-9a-f]{64}$')


def attach_external_analysis(app, repository, settings, *, store, worker, team_mode=False):
    operator_token = os.environ.get('PROJECT_WATCH_SETTINGS_TOKEN', '')

    def allowed(request, project_id, *, write=False):
        if team_mode:
            raise HTTPException(404, '外部解读暂仅支持本机使用。')
        if request.url.hostname not in {'localhost', '127.0.0.1', '::1'}:
            raise HTTPException(403, '请从本机页面操作。')
        if write and (not operator_token
                      or not hmac.compare_digest(request.headers.get('x-settings-token', ''), operator_token)
                      or request.headers.get('origin') != str(request.base_url).rstrip('/')):
            raise HTTPException(403, '调整发送范围需要本机管理口令。')
        if repository.get_project(project_id) is None:
            raise HTTPException(404, '项目不存在。')

    def current_model():
        model = settings.get()
        if model is None or not hasattr(model, 'authorization_id') or not hasattr(model, 'analyze_external'):
            raise HTTPException(503, '请先配置支持外部解读的模型；公开消息仍可阅读。')
        return model

    def disclosure(project_id, body):
        model = current_model()
        scope = store.preview_scope(project_id, plan_ids=body.plan_ids,
                                    selections=[s.model_dump() for s in body.selections],
                                    include_future_news=body.include_future_news, read_articles=body.read_articles)
        value = dict(project_id=project_id, scope=scope, config_id=model.authorization_id(),
                     recipient=asdict(model.config), limits=asdict(body.limits()),
                     rule_version=ARTICLE_RULE_VERSION if body.read_articles else RULE_VERSION)
        return {**value, 'disclosure_digest': digest(value)}

    @app.get('/api/workspace/projects/{project_id}/external-analysis')
    def read(request: Request, project_id: str):
        allowed(request, project_id)
        return {**store.read(project_id), 'authorizations': store.list_authorizations(project_id),
                'failures': store.failures(project_id),
                'automatic_analysis_available': bool(getattr(app.state, 'reading_worker_enabled', False)),
                'processing': worker.status(project_id)}

    @app.get('/api/workspace/projects/{project_id}/external-intelligence')
    def intelligence(request: Request, project_id: str):
        from project_watch.external_focus import external_reading
        from project_watch.external_intelligence import build_intelligence

        allowed(request, project_id)
        # Deliberately outside external_reading: saved-result freshness itself
        # uses external_reading, so merging there would recurse.
        saved = store.read(project_id)
        briefing = external_reading(repository, project_id)['briefing']
        return {**build_intelligence(project_id, briefing, saved),
                'processing': worker.status(project_id),
                'automatic_analysis_available': bool(getattr(app.state, 'reading_worker_enabled', False))}

    @app.post('/api/workspace/projects/{project_id}/external-analysis/jobs/{job_id}/retry-preview')
    def retry_preview(request: Request, project_id: str, job_id: str):
        allowed(request, project_id, write=True)
        model = current_model()
        try:
            return store.retry_preview(project_id, job_id, model.authorization_id())
        except (ReadingDenied, ValueError):
            raise HTTPException(409, '暂不能重试：请检查消息与背景版本、是否暂停，以及剩余调用额度。') from None

    @app.post('/api/workspace/projects/{project_id}/external-analysis/jobs/{job_id}/retry')
    def retry(request: Request, project_id: str, job_id: str, body: ExternalRetryInput):
        allowed(request, project_id, write=True)
        model = current_model()
        if body.consent is not True:
            raise HTTPException(422, '重试会新增一次模型请求，请先确认。')
        if model.disclosure().get('remaining_runs', 0) <= 0:
            raise HTTPException(409, '当前服务的调用额度已用完，请在模型设置中检查。')
        try:
            job = store.retry(project_id, job_id, model.authorization_id(), body.disclosure_digest)
            return {'id': job['id'], 'state': job['state']}
        except (ReadingDenied, ValueError):
            raise HTTPException(409, '消息、授权或额度已变化，请重新检查后重试。') from None

    @app.post('/api/workspace/projects/{project_id}/external-analysis/preview')
    def preview(request: Request, project_id: str, body: ExternalScopeInput):
        allowed(request, project_id, write=True)
        try:
            return {**disclosure(project_id, body),
                    'purpose': '解读选定的公开动态；仅附带下方明确选中的材料片段，不发送项目全库或人的判断。',
                    'source_note': ('读取这些新闻链接的公开正文，保存引用片段并发送给所示模型；不能读取时保留标题与摘要。不登录、不绕过付费限制。'
                                    if body.read_articles else '公开输入限已保存的标题与摘要，不代表已读取文章全文。'),
                    'pause_note': '暂停阻止新请求，已发出的请求无法撤回。',
                    'budget_note': '调用额度跨重启保留；金额为本地估算，以服务商账单为准。'}
        except (ReadingDenied, ValueError):
            raise HTTPException(422, '所选消息、资料或用量限制不可用，请重新核对。') from None

    @app.post('/api/workspace/projects/{project_id}/external-analysis/authorization')
    def authorize(request: Request, project_id: str, body: ExternalConsentInput):
        allowed(request, project_id, write=True)
        if body.consent is not True:
            raise HTTPException(422, '请先确认接收服务、发送范围和用量上限。')
        try:
            viewed = disclosure(project_id, body)
            if not hmac.compare_digest(viewed['disclosure_digest'], body.disclosure_digest):
                raise HTTPException(409, '内容、接收方或用量已变化，请重新预览后确认。')
            return store.authorize(project_id, scope=viewed['scope'], config_id=viewed['config_id'],
                                   recipient=viewed['recipient'], limits=body.limits(),
                                   actor='local-operator', request_key=body.request_key)
        except (ReadingDenied, ValueError):
            raise HTTPException(409, '发送范围已变化或确认记录不一致，请重新预览。') from None

    @app.patch('/api/workspace/projects/{project_id}/external-analysis/authorization/{grant_id}')
    def change(request: Request, project_id: str, grant_id: str, body: ReadingAuthorizationStatus):
        allowed(request, project_id, write=True)
        try:
            grant = store.status(grant_id)
        except ReadingDenied:
            raise HTTPException(404, '外部解读授权不存在。') from None
        if grant['project_id'] != project_id:
            raise HTTPException(404, '外部解读授权不存在。')
        if body.status == 'active':
            model = current_model()
            if model.authorization_id() != grant['config_id'] or grant['rule_version'] not in {RULE_VERSION, ARTICLE_RULE_VERSION}:
                raise HTTPException(409, '接收方或解读方式已变化，请重新预览和授权。')
        try:
            store.set_status(grant_id, body.status, actor='local-operator')
        except ReadingDenied:
            raise HTTPException(409, '已撤销的授权不能恢复，请重新确认范围。') from None
        return store.status(grant_id)
