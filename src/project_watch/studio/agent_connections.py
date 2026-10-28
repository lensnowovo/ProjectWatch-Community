"""Generate portable client configurations; never edit a user's Agent config."""
import json
import os
import sys
import threading
import uuid
from pathlib import Path
from typing import Literal

from fastapi import HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, StrictBool

from project_watch.studio.agent_contracts import ScopeWrite
from project_watch.studio.agent_scope import read_scope, write_scope
from project_watch.studio.application import AccessDenied, VersionConflict

_CONNECTION_LOCK = threading.Lock()


class InstanceConnectionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    client: Literal['codex', 'workbuddy']


class ConnectionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    client: Literal['codex', 'workbuddy']
    allow_internal: StrictBool = False
    connection_id: str | None = None


def connection_path(home, connection_id):
    if len(connection_id) != 32 or any(c not in '0123456789abcdef' for c in connection_id):
        raise HTTPException(422, '连接标识无效')
    directory = home / 'agent-connections'
    path = directory / f'{connection_id}.json'
    if path.is_symlink() or path.resolve().parent != directory.resolve():
        raise HTTPException(422, '连接路径无效')
    return path


def client_configuration(path, client, name):
    executable = Path(sys.executable)
    if executable.name.lower() == 'pythonw.exe':
        executable = executable.with_name('python.exe')
    args = ['-I', '-B', '-m', 'project_watch.studio.agent_bridge']
    environment = {'PROJECT_WATCH_AGENT_CONFIG': str(path.resolve())}
    if client == 'codex':
        text = (f'[mcp_servers.{name}]\ncommand = {json.dumps(str(executable))}\n'
                f'args = {json.dumps(args)}\n[mcp_servers.{name}.env]\n'
                f'PROJECT_WATCH_AGENT_CONFIG = {json.dumps(str(path.resolve()))}\n')
        return text, '~/.codex/config.toml'
    return json.dumps({'mcpServers': {name: {'command': str(executable), 'args': args, 'env': environment}}},
                      ensure_ascii=False, indent=2), 'WorkBuddy → 连接器 / MCP → 自定义配置'


def generate_instance(service, client, home, base_url):
    from project_watch.studio.agent_bridge import AgentClient
    from project_watch.studio.settings import _atomic_write

    AgentClient(base_url, service.instance_id, 'validation', 'pwa_validation')
    directory = home / 'agent-connections'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    connection_id = uuid.uuid4().hex
    path = connection_path(home, connection_id)
    config = {'mode': 'instance', 'url': base_url, 'instance_id': service.instance_id,
              'client': client, 'projects': {}}
    marker = home / 'desktop-session/endpoint.json'
    if marker.is_file():
        config['desktop_marker'] = str(marker.resolve())
    try:
        _atomic_write(path, json.dumps(config, ensure_ascii=False))
    except OSError:
        raise HTTPException(503, '连接文件无法保存，未授予项目权限') from None
    name = 'projectwatch_' + connection_id[:12]
    text, destination = client_configuration(path, client, name)
    return {'connection_id': connection_id, 'server_name': name, 'configuration': text,
            'destination': destination, 'status': 'configuration_generated', 'provider_calls': 0,
            'prompt': '请连接此 ProjectWatch，先调用 studio_runtime。尚未授权项目时，不读取任何项目资料。'
                      '授权后调用 authorized_projects，再 select_project 选择需要操作的项目。'}


def saved_connections(service, home):
    result = []
    for path in sorted((home / 'agent-connections').glob('*.json')):
        try:
            connection_path(home, path.stem)
            config = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(config, dict) or config.get('mode') != 'instance' or config.get('instance_id') != service.instance_id:
                continue
            if config.get('client') not in ('codex', 'workbuddy'):
                continue
            name = 'projectwatch_' + path.stem[:12]
            text, destination = client_configuration(path, config['client'], name)
            result.append({'connection_id': path.stem, 'client': config['client'], 'configuration': text,
                           'destination': destination, 'prompt': '请先调用 studio_runtime，再调用 authorized_projects 查看已授权项目。'})
        except (OSError, ValueError, HTTPException):
            continue
    return result


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


def generate(service, project_id, client, allow_internal, home, base_url, connection_id=None):
    from project_watch.studio.agent_bridge import AgentClient

    # Validate origin before issuing credentials. Instance and project are not user-supplied URLs.
    AgentClient(base_url, service.instance_id, project_id, 'pwa_validation')
    project = service.project(project_id)
    if connection_id:
        from project_watch.studio.settings import _atomic_write

        with _CONNECTION_LOCK:
            path = connection_path(home, connection_id)
            try:
                config = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                raise HTTPException(404, '连接不存在，请重新连接') from None
            if not isinstance(config, dict) or config.get('mode') != 'instance' or config.get('instance_id') != service.instance_id or config.get('client') != client:
                raise HTTPException(409, '连接与当前实例或客户端不匹配')
            if not isinstance(config.get('projects'), dict):
                raise HTTPException(409, '连接文件损坏，请重新连接')
            if project_id not in config['projects'] and len(config['projects']) >= 100:
                raise HTTPException(422, '单个连接最多授权100个项目')
            grant = service.issue_grant(project_id, 'write', f'{client} project research', hours=720, allow_internal=allow_internal)
            old = config['projects'].get(project_id)
            config['projects'][project_id] = {'project_id': project_id, 'name': project['name'],
                                            'token': grant['token'], 'grant_id': grant['id']}
            try:
                _atomic_write(path, json.dumps(config, ensure_ascii=False))
            except OSError:
                service.revoke_grant(project_id, grant['id'])
                raise HTTPException(503, '连接更新失败，本次授权已撤销') from None
            if isinstance(old, dict) and old.get('grant_id'):
                service.revoke_grant(project_id, old['grant_id'])
        name = 'projectwatch_' + connection_id[:12]
        text, destination = client_configuration(path, client, name)
        return {'connection_id': connection_id, 'configuration': text, 'destination': destination,
                'prompt': f'先调用 select_project，project_id={project_id}。\n' + research_prompt(project['name'], name),
                'grant_id': grant['id'], 'expires_at': grant['expires_at'], 'status': 'project_authorized', 'provider_calls': 0}
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
    @app.get('/api/studio/agent-connections')
    def list_connections(response: Response):
        response.headers['Cache-Control'] = 'no-store'
        return saved_connections(service, home)

    @app.post('/api/studio/agent-connection')
    def connect_instance(body: InstanceConnectionRequest, request: Request, response: Response):
        response.headers['Cache-Control'] = 'no-store'
        return generate_instance(service, body.client, home, str(request.base_url).rstrip('/'))

    @app.post('/api/studio/projects/{project_id}/agent-connection')
    def connect(project_id: str, body: ConnectionRequest, request: Request, response: Response):
        response.headers['Cache-Control'] = 'no-store'
        return generate(service, project_id, body.client, body.allow_internal, home, str(request.base_url).rstrip('/'), body.connection_id)

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
