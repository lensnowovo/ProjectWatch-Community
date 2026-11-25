"""ProjectWatch starts the user's local Codex for an authorized Agent project.

Codex runs without the user's own configuration: only this ProjectWatch's MCP bridge
is loaded, scoped by the project grant already saved in the connection file, plus
Codex's own web search. One run at a time. A run that is interrupted or times out is
recorded as unknown and never resent; the next scheduled round is a new run.
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from contextlib import closing, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from project_watch.studio.store import now

TIMEOUT_SECONDS = 45 * 60
# Codex features that could reach this computer or other services. The read-only sandbox only stops writes; with
# these off the run has web search and the bridge, which the project grant bounds. Code mode stays on: this Codex
# routes MCP calls through it, and without a shell its scripts cannot read files (checked with a canary file).
LOCAL_TOOLS = ('shell_tool', 'unified_exec', 'view_image', 'image_generation', 'multi_agent', 'plugins', 'apps', 'hooks')
WRITE_TOOLS = ('submit_monitoring_results', 'update_monitoring_scope')
RENEW_BEFORE_SECONDS = 3 * 24 * 3600
ACTIVE = ('queued', 'running')
SCHEMA = '''CREATE TABLE IF NOT EXISTS studio_agent_runs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    trigger TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    exit_code INTEGER,
    summary TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    input_tokens INTEGER,
    output_tokens INTEGER
);
CREATE INDEX IF NOT EXISTS studio_agent_runs_project ON studio_agent_runs(project_id, created_at);'''


class AutoSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: StrictBool
    connection_id: str | None = None
    cadence_days: int = Field(default=7, ge=1, le=30)
    run_now: StrictBool = False


def _utc(value):
    return datetime.fromisoformat(value).astimezone(UTC) if value else None


def locate_codex():
    """The native Codex executable. The npm shim is a .cmd file that would need a console."""
    override = os.environ.get('PROJECT_WATCH_CODEX_COMMAND')
    if override:
        return json.loads(override)
    found = shutil.which('codex')
    if not found:
        return None
    path = Path(found)
    if os.name != 'nt':
        return [str(path)]
    if path.suffix.lower() == '.exe':
        return [str(path)]
    package = path.parent / 'node_modules' / '@openai' / 'codex'
    for pattern in ('node_modules/@openai/codex-win32-*/vendor/*/bin/codex.exe', 'vendor/*/bin/codex.exe'):
        for candidate in sorted(package.glob(pattern)):
            return [str(candidate)]
    return None


def _child_env(command):
    env = dict(os.environ)
    for name in ('CODEX_MANAGED_BY_NPM', 'CODEX_MANAGED_BY_BUN', 'CODEX_MANAGED_BY_PNPM', 'CODEX_MANAGED_BY_VITE_PLUS'):
        env.pop(name, None)
    marker = os.sep + os.path.join('node_modules', '@openai', 'codex') + os.sep
    if marker in command[0]:
        # As the npm launcher does: lets Codex find its bundled helpers.
        env['CODEX_MANAGED_BY_NPM'] = '1'
        env['CODEX_MANAGED_PACKAGE_ROOT'] = command[0][:command[0].index(marker) + len(marker) - 1]
    # Nothing of this instance's configuration or secrets reaches Codex; the bridge reads its connection file.
    for name in [n for n in env if n.upper().startswith('PROJECT_WATCH_')]:
        env.pop(name)
    return env


def _quiet_run(command, timeout):
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    return subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          timeout=timeout, creationflags=flags, env=_child_env(command))


_STATUS_CACHE: dict = {}


def codex_status():
    """Whether a local, logged-in Codex can be started. Cached briefly; never sends a model request."""
    cached = _STATUS_CACHE.get('value')
    if cached and time.monotonic() - _STATUS_CACHE['at'] < 300:
        return cached
    command = locate_codex()
    result = {'available': False, 'logged_in': False, 'version': ''}
    if command:
        try:
            version = _quiet_run([*command, '--version'], 20)
            if version.returncode == 0:
                result['available'] = True
                result['version'] = version.stdout.decode('utf-8', 'replace').strip().splitlines()[-1][:60]
                result['logged_in'] = _quiet_run([*command, 'login', 'status'], 20).returncode == 0
        except (OSError, subprocess.SubprocessError, IndexError):
            pass
    _STATUS_CACHE.update(value=result, at=time.monotonic())
    return result


def bridge_command():
    executable = Path(sys.executable)
    if executable.name.lower() == 'pythonw.exe':
        executable = executable.with_name('python.exe')
    return str(executable), ['-I', '-B', '-m', 'project_watch.studio.agent_bridge']


def auto_prompt(project_id, project_name, server_name):
    return (f'先调用 select_project，project_id={project_id}。\n'
            f'这是 ProjectWatch 按计划自动发起的一轮研究，使用 {server_name} 为“{project_name}”研究并写回。'
            '无人值守：不要向用户提问或等待确认；无法完成的步骤写明缺口后继续。\n'
            '1. 调用 studio_runtime、project_overview、monitoring_scope 和 current_monitoring_result 读取当前状态与上轮结果。\n'
            '2. 先核对公司主体。用你的联网搜索与阅读工具查找上轮之后的公开新闻、竞品和行业信息。'
            'search_project 只查本机项目，不是网络搜索；没有联网工具时明确报告缺口，不编造结果。\n'
            '3. 按 submit_monitoring_results 的结构写入 sources、news、company、brief 等；'
            '来源附 URL、日期及实际 read_scope，区分事实和判断。保留人的笔记，不将商业数据冒充公开来源。\n'
            '4. 根据本轮发现，用 update_monitoring_scope 增加或调整关注点，写明 queries、reason、exit_rule；'
            '退出条件满足可标 retired。范围保存不代表已执行搜索。\n'
            '5. 每次写入先读取最新 config_version，使用稳定的 idempotency_key，写后读取回执与当前结果确认。'
            '若响应不明，先核对历史和回执，不用新 key 重复写入；有版本冲突重新读取并判断。\n'
            '内部材料仅在授权允许时读取；不访问其他项目，不读取密钥，不直接编辑数据库，不运行本机命令。\n'
            '最后用三到五句话总结：写入了什么、主要变化、缺口，以及搜索/阅读次数。')


class Child:
    """Codex in its own process tree, without a console window; killed with the tree."""

    def __init__(self, command, env, log):
        if os.name == 'nt':
            from project_watch.studio.windows_child import WindowsChild
            self.inner = WindowsChild(command, env=env, log=log)
        else:
            self.inner = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                          env=env, start_new_session=True)

    def wait(self, timeout):
        return self.inner.wait(timeout)

    def terminate(self):
        try:
            if os.name == 'nt':
                self.inner.terminate()
            else:
                import signal
                os.killpg(self.inner.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass

    def close(self):
        if os.name == 'nt':
            self.inner.close()


class AgentRunner:
    def __init__(self, service, home):
        self.service = service
        self.store = service.store
        self.home = Path(home)
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._child = None
        self._current = None
        self._thread = None
        self._closing = threading.Event()
        # run id -> why it was stopped; checked before and right after Codex starts.
        self._stopping: dict[str, str] = {}
        with closing(self.store._connect()) as db, db:
            db.executescript(SCHEMA)

    # ---- settings and records -------------------------------------------------
    def settings(self, project_id):
        raw = self.store.get_state('agent_auto:' + project_id)
        try:
            value = json.loads(raw) if raw else {}
        except ValueError:
            value = {}
        return {'enabled': bool(value.get('enabled')), 'connection_id': value.get('connection_id'),
                'cadence_days': int(value.get('cadence_days') or 7), 'enabled_at': value.get('enabled_at')}

    def _save_settings(self, project_id, value):
        self.store.set_state('agent_auto:' + project_id, json.dumps(value))

    def runs(self, project_id, limit=5):
        with closing(self.store._connect()) as db:
            return [dict(r) for r in db.execute(
                'SELECT id,trigger,status,created_at,started_at,finished_at,summary,error,input_tokens,output_tokens '
                'FROM studio_agent_runs WHERE project_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?', (project_id, limit))]

    def view(self, project_id):
        settings = self.settings(project_id)
        runs = self.runs(project_id)
        last = next((r for r in runs if r['status'] not in ACTIVE), None)
        next_at = None
        if settings['enabled'] and not any(r['status'] in ACTIVE for r in runs):
            # From the latest run or from switching on, whichever is later: switching on never starts a run by itself.
            starts = [_utc(t) for t in (runs[0]['created_at'] if runs else None, settings['enabled_at']) if t]
            next_at = (max(starts) + timedelta(days=settings['cadence_days'])).isoformat() if starts else None
        return {**settings, 'runs': runs, 'active': next((r for r in runs if r['status'] in ACTIVE), None),
                'last': last, 'next_at': next_at}

    def connection(self, connection_id):
        from project_watch.studio.agent_connections import connection_path

        path = connection_path(self.home, connection_id)
        try:
            config = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise HTTPException(409, '连接不存在，请重新连接 Codex') from None
        if (not isinstance(config, dict) or config.get('mode') != 'instance'
                or config.get('instance_id') != self.service.instance_id or config.get('client') != 'codex'):
            raise HTTPException(409, '这个连接不是本机 ProjectWatch 的 Codex 连接')
        return path, config

    def configure(self, project_id, body: AutoSettings):
        project = self.service.project(project_id)
        if project.get('execution_mode') != 'agent':
            raise HTTPException(409, '只有 Agent 项目可以由 Codex 自动研究')
        if body.enabled and project.get('status') == 'removed':
            raise HTTPException(409, '项目已移出关注')
        current = self.settings(project_id)
        connection_id = body.connection_id or current['connection_id']
        if body.enabled:
            if not connection_id:
                raise HTTPException(409, '先把项目授权给已连接的 Codex')
            _path, config = self.connection(connection_id)
            if project_id not in (config.get('projects') or {}):
                raise HTTPException(409, '先把项目授权给已连接的 Codex')
        enabled_at = (current['enabled_at'] or now() if current['enabled'] else now()) if body.enabled else None
        self._save_settings(project_id, {'enabled': body.enabled, 'connection_id': connection_id,
                                         'cadence_days': body.cadence_days, 'enabled_at': enabled_at})
        if body.enabled and body.run_now:
            self.enqueue(project_id, 'created')
        return self.view(project_id)

    def enqueue(self, project_id, trigger):
        settings = self.settings(project_id)
        if not settings['connection_id']:
            raise HTTPException(409, '先开启 Codex 自动研究')
        if self.service.project(project_id).get('status') == 'removed':
            raise HTTPException(409, '项目已移出关注')
        with self._lock, closing(self.store._connect()) as db, db:
            if db.execute(f"SELECT 1 FROM studio_agent_runs WHERE project_id=? AND status IN {ACTIVE}",
                          (project_id,)).fetchone():
                raise HTTPException(409, 'Codex 正在研究这个项目')
            run_id = uuid.uuid4().hex
            db.execute('INSERT INTO studio_agent_runs (id,project_id,connection_id,trigger,status,created_at) '
                       "VALUES (?,?,?,?,'queued',?)", (run_id, project_id, settings['connection_id'], trigger, now()))
        self._wake.set()
        return run_id

    def cancel(self, project_id, run_id):
        with self._lock, closing(self.store._connect()) as db, db:
            row = db.execute('SELECT status FROM studio_agent_runs WHERE id=? AND project_id=?', (run_id, project_id)).fetchone()
            if not row:
                raise HTTPException(404, '运行记录不存在')
            if row['status'] == 'queued':
                db.execute("UPDATE studio_agent_runs SET status='cancelled',finished_at=? WHERE id=?", (now(), run_id))
                return
            if row['status'] != 'running':
                return
            # Results written before the stop stay; the outcome of the rest is unknown.
            self._interrupt(run_id, '已停止；停止前可能已写回部分结果')

    def _finish(self, run_id, status, **fields):
        columns = ['status=?', 'finished_at=?'] + [f'{k}=?' for k in fields]
        with closing(self.store._connect()) as db, db:
            db.execute(f"UPDATE studio_agent_runs SET {','.join(columns)} WHERE id=? AND status='running'",
                       (status, now(), *fields.values(), run_id))

    # ---- scheduling -------------------------------------------------------------
    def schedule_due(self):
        with closing(self.store._connect()) as db:
            keys = [r['key'] for r in db.execute("SELECT key FROM studio_state WHERE key LIKE 'agent_auto:%'")]
        for key in keys:
            project_id = key.split(':', 1)[1]
            settings = self.settings(project_id)
            if not settings['enabled']:
                continue
            try:
                if self.service.project(project_id).get('status') == 'removed':
                    continue
            except Exception:  # the project no longer exists
                continue
            view = self.view(project_id)
            if view['next_at'] and _utc(view['next_at']) <= datetime.now(UTC):
                with suppress(HTTPException):
                    self.enqueue(project_id, 'schedule')

    def recover(self):
        """A run still marked running belongs to a process that no longer exists."""
        with closing(self.store._connect()) as db, db:
            db.execute("UPDATE studio_agent_runs SET status='unknown',finished_at=?,"
                       "error='ProjectWatch 在运行中退出；可能已写回部分结果' WHERE status='running'", (now(),))

    # ---- execution ----------------------------------------------------------------
    def _claim(self):
        with self._lock, closing(self.store._connect()) as db, db:
            row = db.execute("SELECT * FROM studio_agent_runs WHERE status='queued' ORDER BY created_at, rowid LIMIT 1").fetchone()
            if not row:
                return None
            db.execute("UPDATE studio_agent_runs SET status='running',started_at=? WHERE id=?", (now(), row['id']))
            return dict(row)

    def _grant(self, run, path, config, renew):
        """The saved project grant, renewed while auto research stays on. A revoked grant stops the project."""
        from project_watch.studio.agent_connections import generate

        project_id = run['project_id']
        entry = (config.get('projects') or {}).get(project_id)
        if not isinstance(entry, dict):
            raise RuntimeError('项目没有授权给这个 Codex 连接')
        grants = {g['id']: g for g in self.service.list_grants(project_id)}
        grant = grants.get(entry.get('grant_id'))
        if not grant or grant['revoked_at']:
            self._save_settings(project_id, {**self.settings(project_id), 'enabled': False})
            raise RuntimeError('授权已撤销，已关闭自动研究')
        if grant['expires_at'] <= time.time() and not renew:
            raise RuntimeError('授权已过期，请重新授权')
        if renew and grant['expires_at'] - time.time() < RENEW_BEFORE_SECONDS:
            generate(self.service, project_id, 'codex', bool(grant['allow_internal']), self.home, config['url'],
                     run['connection_id'])

    def execute(self, run):
        try:
            self._execute(run)
        except Exception as exc:  # never leave a run marked running
            launched = self._current == run['id']
            self._finish(run['id'], 'unknown' if launched else 'failed', error=(str(exc) or type(exc).__name__)[:300])
            raise
        finally:
            with self._lock:
                self._current = None
                self._child = None
                self._stopping.pop(run['id'], None)

    def _execute(self, run):
        run_id, project_id = run['id'], run['project_id']
        command = locate_codex()
        if not command:
            return self._finish(run_id, 'failed', error='未找到本机 Codex CLI')
        try:
            project = self.service.project(project_id)
            if project.get('status') == 'removed':
                return self._finish(run_id, 'failed', error='项目已移出关注')
            path, config = self.connection(run['connection_id'])
            self._grant(run, path, config, renew=self.settings(project_id)['enabled'])
        except HTTPException as exc:
            return self._finish(run_id, 'failed', error=str(exc.detail))
        except Exception as exc:  # the project, grant or connection changed underneath
            return self._finish(run_id, 'failed', error=str(exc)[:300] or type(exc).__name__)
        if run_id in self._stopping or self._closing.is_set():
            return self._finish(run_id, 'cancelled', error='已停止，Codex 未启动')
        folder = self.home / 'agent-runs' / run_id
        work = folder / 'work'
        work.mkdir(parents=True, exist_ok=True)
        name = 'projectwatch_' + run['connection_id'][:12]
        python, args = bridge_command()
        prefix = f'mcp_servers.{name}'
        argv = [*command, 'exec', '--ignore-user-config', '--skip-git-repo-check', '--sandbox', 'read-only',
                '--ephemeral', '--json', '--color', 'never', '-C', str(work), '-o', str(folder / 'last.txt'),
                *[arg for tool in LOCAL_TOOLS for arg in ('--disable', tool)],
                '-c', 'web_search="live"',
                '-c', f'{prefix}.command={json.dumps(python)}',
                '-c', f'{prefix}.args={json.dumps(args)}',
                '-c', f'{prefix}.env={{PROJECT_WATCH_AGENT_CONFIG={json.dumps(str(path.resolve()))}}}',
                # Only this server's tools, which the project grant already limits, run without a prompt.
                '-c', f'{prefix}.default_tools_approval_mode="approve"',
                auto_prompt(project_id, project['name'], name)]
        status, code, error = 'unknown', None, ''
        with open(folder / 'events.jsonl', 'wb') as log:
            try:
                child = Child(argv, _child_env(command), log)
            except OSError as exc:
                return self._finish(run_id, 'failed', error=f'Codex 无法启动：{exc}'[:300])
            with self._lock:
                self._child, self._current = child, run_id
                stopped = run_id in self._stopping or self._closing.is_set()
            try:
                if stopped:
                    child.terminate()
                code = child.wait(TIMEOUT_SECONDS)
                status = 'done' if code == 0 else 'failed'
            except subprocess.TimeoutExpired:
                child.terminate()
                error = f'超过 {TIMEOUT_SECONDS // 60} 分钟未结束，已停止；可能已写回部分结果'
            finally:
                with self._lock:
                    self._child = None
                child.close()
        summary, usage, lines, wrote, attempted = '', {}, [], 0, 0
        with suppress(OSError):
            summary = (folder / 'last.txt').read_text(encoding='utf-8')[:4000].strip()
        with suppress(OSError):
            for line in (folder / 'events.jsonl').read_text(encoding='utf-8', errors='replace').splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    lines.append(line)
                    continue
                item = event.get('item') or {}
                if (event.get('type') == 'item.started' and item.get('type') == 'mcp_tool_call'
                        and item.get('tool') in WRITE_TOOLS):
                    attempted += 1
                if (event.get('type') == 'item.completed' and item.get('type') == 'mcp_tool_call'
                        and item.get('tool') in WRITE_TOOLS and item.get('status') == 'completed'):
                    wrote += 1
                if event.get('type') == 'turn.completed':
                    usage = event.get('usage') or {}
                elif event.get('type') in ('error', 'turn.failed'):
                    lines.append(str(event.get('message') or event.get('error') or ''))
        reason = self._stopping.get(run_id) or ('ProjectWatch 退出时停止；可能已写回部分结果' if self._closing.is_set() else '')
        if reason and status != 'done':
            status, error = 'unknown', reason
        if status == 'done' and not wrote:
            # Codex finished without a confirmed write: never report that as a completed round. A write that was
            # sent but not confirmed may still have been saved, so its outcome is unknown rather than failed.
            status, error = (('unknown', '写回结果未确认，请查看项目记录') if attempted
                             else ('failed', 'Codex 结束了，但没有写回结果'))
        if status == 'failed' and not error:
            error = (next((line for line in reversed(lines) if line.strip()), '') or f'Codex 退出码 {code}')[-300:]
        self._finish(run_id, status, exit_code=code, summary=summary, error=error,
                     input_tokens=usage.get('input_tokens'), output_tokens=usage.get('output_tokens'))

    # ---- lifecycle --------------------------------------------------------------------
    def _interrupt(self, run_id, reason):
        """Caller holds the lock. Stops this run now, or as soon as it starts."""
        self._stopping[run_id] = reason
        if self._current == run_id and self._child is not None:
            self._child.terminate()

    def step(self):
        """One scheduling pass and at most one run. Used by the thread and by tests."""
        if self._closing.is_set():
            return None
        self.schedule_due()
        run = None if self._closing.is_set() else self._claim()
        if run:
            self.execute(run)
        return run

    def _loop(self):
        self.recover()
        while not self._closing.is_set():
            try:
                self.step()
            except Exception:  # keep the runner alive; the run is already recorded
                import logging
                logging.getLogger('project_watch.agent_runs').exception('agent run failed')
            self._wake.wait(60)
            self._wake.clear()

    def start(self):
        self._thread = threading.Thread(target=self._loop, name='agent-runs', daemon=True)
        self._thread.start()

    def request_stop(self):
        with self._lock:
            self._closing.set()
            if self._current:
                self._interrupt(self._current, 'ProjectWatch 退出时停止；可能已写回部分结果')
        self._wake.set()

    def join(self, timeout=15):
        if self._thread is not None:
            self._thread.join(timeout)


def attach(app, service, runner):
    from fastapi import Response

    @app.get('/api/studio/agent/codex')
    def codex(response: Response, refresh: bool = False):
        response.headers['Cache-Control'] = 'no-store'
        if refresh:
            _STATUS_CACHE.clear()
        return codex_status()

    @app.get('/api/studio/projects/{project_id}/agent-auto')
    def auto_view(project_id: str):
        service.project(project_id)
        return runner.view(project_id)

    @app.put('/api/studio/projects/{project_id}/agent-auto')
    def auto_update(project_id: str, body: AutoSettings):
        return runner.configure(project_id, body)

    @app.post('/api/studio/projects/{project_id}/agent-runs', status_code=202)
    def run_now(project_id: str):
        service.project(project_id)
        runner.enqueue(project_id, 'manual')
        return runner.view(project_id)

    @app.post('/api/studio/projects/{project_id}/agent-runs/{run_id}/cancel')
    def cancel(project_id: str, run_id: str):
        runner.cancel(project_id, run_id)
        return runner.view(project_id)
