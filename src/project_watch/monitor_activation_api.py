"""Local operator bridge; approved public terms only, no model or search on reads."""
import hmac
import os
from contextlib import closing
from typing import Literal

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from project_watch.monitor_activation import (
    MonitorActivationStore,
    checked_strategy,
    current_packet,
)
from project_watch.monitor_strategy import Packet, digest
from project_watch.repository import ReviewConflictError


class ActivationPreview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    packet: Packet
    strategy: dict
    provider: Literal['gdelt', 'brave', 'bing'] = 'gdelt'


class ActivationStart(ActivationPreview):
    consent: bool = Field(strict=True)
    approved_terms: list[str] = Field(min_length=1, max_length=16)
    disclosure_digest: str = Field(pattern=r'^[0-9a-f]{64}$')


def attach_monitor_activation(app, repository, *, team_mode=False, brave_available=lambda: False):
    token = os.environ.get('PROJECT_WATCH_SETTINGS_TOKEN', '')
    store = None if team_mode else MonitorActivationStore(repository)

    def allowed(request, project_id):
        if team_mode:
            raise HTTPException(404, '仅供本机使用。')
        if (request.url.hostname not in {'localhost', '127.0.0.1', '::1'} or not token
                or not hmac.compare_digest(request.headers.get('x-settings-token', ''), token)
                or request.headers.get('origin') != str(request.base_url).rstrip('/')):
            raise HTTPException(403, '开始监控需要本机授权。')
        if repository.get_project(project_id) is None:
            raise HTTPException(404, '项目不存在。')

    def preview(project_id, body):
        if body.packet.project_id != project_id:
            raise ValueError('project mismatch')
        p, strategy = checked_strategy(body.packet.model_dump(), body.strategy)
        with closing(repository._connect()) as db:
            current_packet(db, p)
        value = dict(project_id=project_id, input_digest=digest(p), strategy=strategy, provider=body.provider)
        return {'disclosure_digest': digest(value), 'terms': [s['anchor'] for s in strategy['scopes']],
                'provider': body.provider, 'eligible': body.provider in {'gdelt', 'bing'} or brave_available(),
                'purpose': '仅用这些公开名称检索；不发送 BP 正文。新范围将每天检查，已暂停的范围不会重新开启。'}

    @app.post('/api/workspace/projects/{project_id}/monitor-activation/preview')
    def view(request: Request, project_id: str, body: ActivationPreview):
        allowed(request, project_id)
        try:
            return preview(project_id, body)
        except ValueError:
            raise HTTPException(409, '资料或监控范围已变化，请重新生成。') from None

    @app.post('/api/workspace/projects/{project_id}/monitor-activation', status_code=202)
    def start(request: Request, project_id: str, body: ActivationStart):
        allowed(request, project_id)
        try:
            viewed = preview(project_id, body)
            if not viewed['eligible']:
                raise HTTPException(409, '所选检索服务暂不可用。')
            if not hmac.compare_digest(viewed['disclosure_digest'], body.disclosure_digest):
                raise ValueError('disclosure changed')
            return store.activate(body.packet.model_dump(), body.strategy, approved_terms=body.approved_terms,
                                  provider=body.provider, actor='local-operator', consent=body.consent)
        except (ValueError, ReviewConflictError):
            raise HTTPException(409, '资料、范围或授权不一致，本次未启动新的检查。') from None
