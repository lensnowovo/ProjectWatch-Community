"""Generate portable client configurations; never edit a user's Agent config."""
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Literal

from fastapi import HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, StrictBool

from project_watch.studio.agent_contracts import ScopeWrite
from project_watch.studio.agent_scope import read_scope, write_scope
from project_watch.studio.application import AccessDenied, VersionConflict


class ConnectionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    client: Literal['codex', 'workbuddy']
    allow_internal: StrictBool = False


def research_prompt(project_name, server_name):
    return (f'请使用 {server_name} 连接 ProjectWatch，为“{project_name}”做一轮研究并写回。\n'
            '1. 调用 studio_runtime、project_overview、monitoring_scope 和 current_monitoring_result 读取当前状态。\n'
            '2. 先核对公司主体。使用你自己的联网搜索与阅读工具查找公开新闻、竞品和行业信息。'
            'search_project 只查本机项目，不是网络搜索；如果没有联网工具，明确报告缺口，不编造结果。\n'
            '3. 按 submit_monitoring_results 的结构写入 sources、news、company、brief 等；'
            '来源附 URL、日期及实际 read_scope，区分事实和判断。保留人的笔记，不将商业数据冒充公开来源。\n'
            '4. 根据我的要求，通过 update_monitoring_scope 增加/调整关注点，写明 queries、reason、exit_rule；'
            '退出条件满足可标 retired。范围保存不代表已执行搜索，不要擅自恢复后台 API 任务。\n'
            '5. 每次写入先读取最新 config_version，使用稳定的 idempotency_key，写后读取回执与当前结果确认。'
            '若响应不明，先核对历史和回执，不用新 key 重复写入；有版本冲突重新读取并判断。\n'
            '内部材料仅在我明确授权后读取；不访问其他项目，不读取密钥，不直接编辑数据库。'
            '报告搜索/阅读次数及可取得的模型用量，缺失写未知。这是一轮按需研究，不承诺无人值守运行。')


def generate(service, project_id, client, allow_internal, home, base_url):
    from project_watch.studio.agent_bridge import AgentClient

    # Validate origin before issuing credentials. Instance and project are not user-supplied URLs.
    AgentClient(base_url, service.instance_id, project_id, 'pwa_validation')
    project = service.project(project_id)
    directory = home / 'agent-connections'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    grant = service.issue_grant(project_id, 'write', f'{client} project research', hours=720, allow_internal=allow_internal)
    path = directory / f'{uuid.uuid4().hex}.json'
    try:
        config = {'url': base_url, 'instance_id': service.instance_id, 'project_id': project_id,
                  'token': grant['token'], 'grant_id': grant['id']}
        marker = home / 'desktop-session/endpoint.json'
        if marker.is_file():
            config['desktop_marker'] = str(marker.resolve())
        with path.open('x', encoding='utf-8') as stream:
            os.chmod(path, 0o600)
            json.dump(config, stream, ensure_ascii=False)
    except OSError:
        service.revoke_grant(project_id, grant['id'])
        raise HTTPException(503, '连接文件无法保存；本次授权已撤销，请检查目录权限') from None
    executable = Path(sys.executable)
    if executable.name.lower() == 'pythonw.exe':
        executable = executable.with_name('python.exe')
    name = 'projectwatch_' + project_id.replace('-', '')[:12]
    args = ['-I', '-B', '-m', 'project_watch.studio.agent_bridge']
    environment = {'PROJECT_WATCH_AGENT_CONFIG': str(path.resolve())}
    if client == 'codex':
        text = (f'[mcp_servers.{name}]\ncommand = {json.dumps(str(executable))}\n'
                f'args = {json.dumps(args)}\n[mcp_servers.{name}.env]\n'
                f'PROJECT_WATCH_AGENT_CONFIG = {json.dumps(str(path.resolve()))}\n')
        destination = '~/.codex/config.toml'
    else:
        text = json.dumps({'mcpServers': {name: {'command': str(executable), 'args': args, 'env': environment}}},
                          ensure_ascii=False, indent=2)
        destination = 'WorkBuddy → 连接器 / MCP → 自定义配置'
    return {'client': client, 'server_name': name, 'configuration': text, 'destination': destination,
            'grant_id': grant['id'], 'expires_at': grant['expires_at'], 'allow_internal': allow_internal,
            'prompt': research_prompt(project['name'], name), 'status': 'configuration_generated',
            'provider_calls': 0}


def attach(app, service, home, plan, scoped):
    @app.post('/api/studio/projects/{project_id}/agent-connection')
    def connect(project_id: str, body: ConnectionRequest, request: Request, response: Response):
        response.headers['Cache-Control'] = 'no-store'
        return generate(service, project_id, body.client, body.allow_internal, home, str(request.base_url).rstrip('/'))

    @app.get('/api/studio/agent/projects/{project_id}/scope')
    def scope(project_id: str, request: Request):
        return read_scope(service, plan, project_id, scoped(request, project_id))

    @app.post('/api/studio/agent/projects/{project_id}/scope')
    def update_scope(project_id: str, body: ScopeWrite, request: Request):
        token = scoped(request, project_id, 'write')
        try:
            return write_scope(service, plan, project_id, body, token)
        except (AccessDenied, VersionConflict):
            raise
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
