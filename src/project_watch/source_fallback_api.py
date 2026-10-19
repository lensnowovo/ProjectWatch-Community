"""Local explicit grant for a second public receiver; never sent private material."""
import hmac
import os

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from project_watch.external_focus import ExternalFocusStore


class FallbackGrant(BaseModel):
    model_config = ConfigDict(extra='forbid')
    consent: bool = Field(strict=True)
    public_term: str = Field(min_length=2,max_length=120)


def attach_source_fallback(app, repo, store):
    token = os.environ.get('PROJECT_WATCH_SETTINGS_TOKEN','')

    @app.post('/api/workspace/projects/{project_id}/monitoring/plans/{plan_id}/fallback')
    def approve(request: Request, project_id: str, plan_id: str, body: FallbackGrant):
        if store is None:
            raise HTTPException(404,'仅供本机使用。')
        if (request.url.hostname not in {'localhost','127.0.0.1','::1'} or not token
                or not hmac.compare_digest(request.headers.get('x-settings-token',''),token)
                or request.headers.get('origin') != str(request.base_url).rstrip('/')):
            raise HTTPException(403,'更改公开检索来源需要本机授权。')
        plan = repo.get_monitoring_plan(plan_id)
        scope = ExternalFocusStore(repo).get(plan_id)
        if not plan or plan.project_id != project_id:
            raise HTTPException(404,'未找到这个跟踪范围。')
        if not scope or scope['term'] != body.public_term:
            raise HTTPException(409,'公开名称已变化，本次没有更改。')
        try:
            store.approve(plan_id, consent=body.consent, actor='local-operator')
        except ValueError:
            raise HTTPException(409,'仅支持已授权的 GDELT 公开名称。') from None
        return {'plan_id':plan_id,'alternate':'bing','public_term':scope['term'],
                'disclosure':'原来源失败时，将同一个公开名称发送至 Bing 新闻订阅；不发送项目材料。',
                'enabled':plan.enabled}
