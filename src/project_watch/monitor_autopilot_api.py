"""Single disclosure for selected public-safe BP excerpts; never invokes a model on GET."""
import hmac
import json
import os
from contextlib import closing
from dataclasses import asdict

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from project_watch.monitor_activation import current_packet
from project_watch.monitor_strategy import OUTPUT_TOKENS, RULE_VERSION, Packet, digest
from project_watch.reading_jobs import ReadingDenied


class Preview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    packet: Packet
    automatic_analysis: bool = Field(default=False, strict=True)


class Start(Preview):
    model_consent: bool = Field(strict=True)
    public_consent: bool = Field(strict=True)
    disclosure_digest: str = Field(pattern=r'^[0-9a-f]{64}$')


def attach_monitor_autopilot(app, repo, settings, pilot):
    token = os.environ.get('PROJECT_WATCH_SETTINGS_TOKEN','')

    def allowed(request, project_id, write=False):
        if pilot is None:
            raise HTTPException(404,'自动监控暂仅支持本机。')
        if request.url.hostname not in {'localhost','127.0.0.1','::1'}:
            raise HTTPException(403,'请从本机页面操作。')
        if write and (not token or not hmac.compare_digest(request.headers.get('x-settings-token',''),token)
                or request.headers.get('origin') != str(request.base_url).rstrip('/')):
            raise HTTPException(403,'开始监控需要本机授权。')
        if repo.get_project(project_id) is None:
            raise HTTPException(404,'项目不存在。')

    def disclosure(project_id, body):
        p=body.packet.model_dump()
        if p['project_id'] != project_id:
            raise ValueError('foreign packet')
        with closing(repo._connect()) as db:
            current_packet(db,p)
        model=settings.get()
        if model is None or not hasattr(model,'plan_monitoring') or not hasattr(model,'authorization_id'):
            raise HTTPException(503,'请先配置模型；上传的资料仍保存在本机。')
        value={'packet':p,'config_id':model.authorization_id(),'recipient':asdict(model.config),
            'rule_version':RULE_VERSION,'max_requests':1,'max_output_tokens':OUTPUT_TOKENS,'provider':'bing'}
        workflow_key = digest([p, value['config_id'], RULE_VERSION, 'monitor_strategy', 'bing'])
        with closing(repo._connect()) as db:
            bp_reused = db.execute('SELECT 1 FROM monitor_autopilots WHERE id=?', (workflow_key,)).fetchone() is not None
        value['automatic_analysis'] = body.automatic_analysis
        if body.automatic_analysis:
            if not hasattr(model, 'analyze_external'):
                raise HTTPException(503, '当前模型暂不支持消息解读。')
            value['analysis'] = pilot.analysis.preview(p)
        # Runtime progress is informational, not a changing consent scope. A retry
        # using the original disclosure must keep its idempotency after creation.
        return {**value,'bp_reused':bp_reused,'disclosure_digest':digest(value),
            'notice':'仅选中片段发送给当前模型。确认片段不含保密内容后，AI 从中提取公司及产品名称，新增方向使用 Bing 新闻订阅并每日检查。已有同词同类监控沿用此前授权来源和暂停设置。不上传原文件或其他材料；换版需重新确认。'}

    @app.get('/api/workspace/projects/{project_id}/monitor-autopilot')
    def read(request: Request, project_id: str):
        allowed(request,project_id)
        return {'workflows':pilot.read(project_id),
            'worker_enabled':bool(getattr(app.state,'reading_worker_enabled',False))}

    @app.post('/api/workspace/projects/{project_id}/monitor-autopilot/preview')
    def preview(request: Request, project_id: str, body: Preview):
        allowed(request,project_id,True)
        try:
            return disclosure(project_id,body)
        except ValueError:
            raise HTTPException(409,'资料版本或片段已变化，请重新选择。') from None

    @app.post('/api/workspace/projects/{project_id}/monitor-autopilot',status_code=202)
    def start(request: Request, project_id: str, body: Start):
        allowed(request,project_id,True)
        try:
            view=disclosure(project_id,body)
            if not hmac.compare_digest(body.disclosure_digest,view['disclosure_digest']):
                raise ValueError('disclosure changed')
            return pilot.authorize(body.packet.model_dump(),model_consent=body.model_consent,
                public_consent=body.public_consent,expected_config=view['config_id'],
                automatic_analysis=body.automatic_analysis)
        except (ValueError,ReadingDenied):
            raise HTTPException(409,'资料、模型接收方或授权已变化，本次未启动。') from None

    @app.post('/api/workspace/projects/{project_id}/monitor-autopilot/{pilot_id}/analysis-preview')
    def analysis_preview(request: Request, project_id: str, pilot_id: str):
        allowed(request, project_id, True)
        with closing(repo._connect()) as db:
            row = db.execute('SELECT packet_json FROM monitor_autopilots WHERE id=? AND project_id=?',
                             (pilot_id, project_id)).fetchone()
        if row is None:
            raise HTTPException(404, '未找到这项监控。')
        try:
            return disclosure(project_id, Preview(packet=json.loads(row[0]), automatic_analysis=True))
        except (ValueError, ReadingDenied):
            raise HTTPException(409, '资料已变化，请重新确认 BP。') from None

    @app.post('/api/workspace/projects/{project_id}/monitor-autopilot/{pilot_id}/pause')
    def pause(request: Request, project_id: str, pilot_id: str):
        allowed(request,project_id,True)
        if pilot_id not in {r['id'] for r in pilot.read(project_id)}:
            raise HTTPException(404,'未找到这项监控。')
        pilot.pause(pilot_id)
        return {'state':'paused'}

    @app.post('/api/workspace/projects/{project_id}/monitor-autopilot/{pilot_id}/retry-activation')
    def retry_activation(request: Request, project_id: str, pilot_id: str):
        allowed(request, project_id, True)
        if pilot_id not in {r['id'] for r in pilot.read(project_id)}:
            raise HTTPException(404, '未找到这项监控。')
        try:
            pilot.retry_activation(pilot_id)
        except (ReadingDenied, ValueError):
            raise HTTPException(409, '无法继续，请检查资料版本和模型设置。') from None
        return {'state':'activating'}
