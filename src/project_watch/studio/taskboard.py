"""任务看板: what is running, waiting, done or failed across watched projects, and where each uploaded file is.

Read-only projection over stored job, intake and call records; no model or network calls.
"""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime, timedelta

KIND_LABEL = {"internal": "分析内部资料", "external": "收集公开信息", "external_followup": "补全外部信息", "brief": "生成当前判断",
              "public_profile": "补充公开档案", "landscape": "整理竞争与行业", "business": "整理产品与用途",
              "competition": "更新竞对资料", "details": "整理商业关系与政策条件", "commercial": "查询专业数据来源",
              "insight": "深度分析"}
INTAKE_LABEL = {"queued": "等待识别", "processing": "正在识别文字", "completed": "已识别", "needs_ocr": "部分页面未识别", "failed": "识别失败"}


def _removed_filter(alias: str) -> str:
    return f"NOT EXISTS (SELECT 1 FROM studio_projects rp WHERE rp.project_id={alias}.project_id AND rp.status='removed')"


def board(store, *, worker_state: str, model_ready: bool, model_binding: str = "", project_id: str | None = None,
          limit: int = 120) -> dict:
    scope, args = ("AND j.project_id=?", [project_id]) if project_id else ("", [])
    with closing(store._connect()) as db:
        jobs = [dict(r) for r in db.execute(
            f"""SELECT j.id, j.project_id, j.kind, j.status, j.stage, j.error, j.created_at, j.started_at, j.finished_at,
                       j.progress_at,j.heartbeat_at,j.stalled_at,j.yield_count,j.attempts,j.lease_until,
                       p.name AS project_name
                FROM studio_jobs j JOIN projects p ON p.id=j.project_id
                WHERE {_removed_filter('j')} {scope}
                ORDER BY CASE j.status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END,
                         CASE WHEN j.status='queued' THEN COALESCE(j.queue_at,j.created_at) END ASC,
                         COALESCE(j.finished_at, j.started_at, j.created_at) DESC
                LIMIT ?""", (*args, limit)).fetchall()]
        from project_watch.studio.runtime_health import projection

        for job in jobs:
            job['runtime'] = projection(db, job, worker=worker_state)
        ids = [j["id"] for j in jobs]
        calls = {}
        if ids:
            from project_watch.studio.recovery import _RECEIVED_MODEL_FAILURE, UNRESOLVED

            marks = ",".join("?" * len(ids))
            calls = {r[0]: {"calls": r[1], "unknown": r[2], "model_failure": r[3]} for r in db.execute(
                f"SELECT job_id, sum(kind='model'), sum(CASE WHEN {UNRESOLVED} THEN 1 ELSE 0 END), "
                f"sum(CASE WHEN ({_RECEIVED_MODEL_FAILURE}) THEN 1 ELSE 0 END) "
                f"FROM studio_job_calls c WHERE job_id IN ({marks}) GROUP BY job_id", ids)}
        documents = [dict(r) for r in db.execute(
            f"""SELECT s.id, s.project_id, s.reference AS name, s.created_at, p.name AS project_name,
                       i.status AS intake_status, i.error_code, i.updated_at AS intake_updated_at,
                       (SELECT count(*) FROM document_fragments f WHERE f.source_id=s.id) AS fragments,
                       a.analyzed_at,
                       (SELECT model_binding FROM studio_source_permissions sp WHERE sp.source_id=s.id) AS permission
                FROM sources s JOIN projects p ON p.id=s.project_id
                LEFT JOIN intake_jobs i ON i.source_id=s.id
                LEFT JOIN studio_analyzed_sources a ON a.project_id=s.project_id AND a.source_id=s.id
                WHERE s.kind='document' AND {_removed_filter('s')} {scope.replace('j.', 's.')}
                ORDER BY s.created_at DESC LIMIT 40""", args).fetchall()]
        internal = {}
        for r in db.execute(f"""SELECT j.id, j.project_id, j.status, j.stage, j.error, j.created_at FROM studio_jobs j
                                WHERE j.kind='internal' {scope} ORDER BY j.created_at""", args):
            internal[r['project_id']] = dict(r)  # latest internal job per project
            internal[r['project_id']]['runtime'] = next((j['runtime'] for j in jobs if j['id'] == r['id']), {})

    dismissed = set()
    if any(j["status"] == "failed" or j['runtime']['state'] == 'partial' for j in jobs):
        with closing(store._connect()) as db:
            dismissed = {r[0].removeprefix("task_dismissed:") for r in db.execute(
                "SELECT key FROM studio_state WHERE key LIKE 'task_dismissed:%'")}
    for job in jobs:
        job["label"] = KIND_LABEL.get(job["kind"], job["kind"])
        job.update(calls.get(job["id"], {"calls": 0, "unknown": 0}))
        if job["status"] == "failed" or job['runtime']['state'] in ('partial', 'needs_confirmation'):
            job["advice"] = advice(job)
            job["dismissed"] = job["id"] in dismissed
        start, end = job.get("started_at"), job.get("finished_at")
        job["seconds"] = (int((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds())
                          if start and end else None)
    queued = [j for j in jobs if j["status"] == "queued"]
    for n, job in enumerate(queued, 1):
        job["position"] = n

    for doc in documents:
        doc["steps"] = _document_steps(doc, internal.get(doc["project_id"]), model_ready, model_binding)

    since = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
    recent = [j for j in jobs if (j.get("finished_at") or "") >= since]
    return {"worker": worker_state, "model_ready": model_ready,
            "summary": {"running": sum(j["status"] == "running" for j in jobs), "queued": len(queued),
                        "failed_24h": sum(j["status"] == "failed" for j in recent),
                        "done_24h": sum(j["runtime"]['state'] == "done" for j in recent),
                        "partial_24h": sum(j["runtime"]['state'] == "partial" for j in recent)},
            "jobs": jobs, "documents": documents}


def _document_steps(doc: dict, job: dict | None, model_ready: bool, model_binding: str) -> list[dict]:
    """上传 → 识别文字 → AI 吸收 → 纳入档案, each with a plain status: done / running / waiting / failed / blocked."""
    intake = doc.get("intake_status") or "queued"
    parse_state = {"completed": "done", "needs_ocr": "partial", "failed": "failed", "processing": "running"}.get(intake, "waiting")
    parse_note = INTAKE_LABEL.get(intake, intake) + (f" · 已读 {doc['fragments']} 段" if doc.get("fragments") else "")
    if doc.get("error_code") and parse_state == "failed":
        errors = {'parser_stalled': '长时间无进展，已停止解析；原件保留，可重新识别',
                  'parser_cancelled': '停机时已取消解析；原件保留，可重新识别',
                  'parser_crashed': '解析进程异常退出，可重新识别',
                  'parser_timeout': '解析超时，可重新识别'}
        parse_note += f"（{errors.get(doc['error_code'], doc['error_code'])}）"
    readable = parse_state in ("done", "partial") and doc.get("fragments")
    analysed = bool(doc.get("analyzed_at") and doc.get("intake_updated_at") and doc["analyzed_at"] >= doc["intake_updated_at"])
    if analysed:
        absorb, note = "done", "已完成"
    elif not readable:
        absorb, note = "waiting", "等识别完成"
    elif doc.get("permission") is not None and (doc["permission"] == "" or doc["permission"] != model_binding):
        absorb, note = "blocked", "未授权发送给当前模型"
    elif not model_ready:
        absorb, note = "blocked", "需先连接 AI 模型"
    elif job and job.get('runtime', {}).get('state') in ('stalled', 'recovering', 'blocked', 'needs_confirmation', 'paused'):
        absorb, note = 'blocked', job['runtime']['label']
    elif job and job["status"] == "running":
        absorb, note = "running", (job.get('runtime', {}).get('label') if job.get('runtime', {}).get('state') == 'waiting_response'
                                   else job.get("stage") or "正在分析")
    elif job and job["status"] == "queued":
        absorb, note = "waiting", "已排队"
    elif job and job["status"] == "failed" and job["created_at"] >= (doc.get("intake_updated_at") or ""):
        absorb, note = "failed", job.get("error") or "分析未完成"
    else:
        absorb, note = "waiting", "等待后台开始"
    return [
        {"key": "upload", "label": "已上传", "state": "done", "note": ""},
        {"key": "parse", "label": "识别文字", "state": parse_state, "note": parse_note},
        {"key": "absorb", "label": "AI 吸收", "state": absorb, "note": note},
        {"key": "profile", "label": "纳入档案", "state": "done" if analysed else "waiting",
         "note": "已更新公司档案与指标" if analysed else ""},
    ]


def advice(job: dict) -> dict:
    """An incomplete outcome in plain words: what happened and what can be done.

    action: recover = review and resend the requests whose outcome is unknown (may bill twice);
            rerun = start the task again; settings = check a service first, then rerun; none = no execution action.
    """
    error = (job.get("error") or "").strip()
    if job.get('model_failure') or error.startswith('model_http_5xx'):
        return {'why': '模型服务已返回 HTTP 5xx，但没有可用回答。已有结果保留，计费仍待核。',
                'todo': '查看恢复选项，确认指定请求后继续；不会自动重发，也不会改写历史完成记录。',
                'action': 'recover'}
    if error.startswith('request_outcome_unknown: site_recovery_blocked: '):
        return {'why': error.split('site_recovery_blocked: ', 1)[1],
                'todo': '到任务记录核对其他阻塞请求；本次网页授权尚未使用，不能据此恢复其他请求。',
                'action': 'none'}
    if job.get('kind') == 'commercial' and job.get('unknown'):
        return {'why': '部分专业来源请求结果不明，已有信息保留。',
                'todo': '请先检查服务商调用记录；本轮不会自动重发。', 'action': 'none'}
    if job.get('runtime', {}).get('state') == 'needs_confirmation' or job.get("unknown") or error.startswith(("request_outcome_unknown", "interrupted")):
        n = job.get("unknown") or 1
        return {"why": f"有 {n} 次请求已经发出，但没有收到明确结果，服务商可能已经计费。为避免重复扣费，系统停下来等你确认。",
                "todo": "点“查看并重发”，确认只重发这几次请求；也可收起提醒，任务仍保留为未完成。", "action": "recover"}
    if job.get('runtime', {}).get('state') == 'partial':
        return {'why': job['runtime'].get('label') or '部分内容已完成，其余暂未取得结果。',
                'todo': '已有结果可继续查看。请查看来源状态，确认缺口后再决定是否补全；不会自动重跑。',
                'action': 'none'}
    if error.startswith(("dsh_model_timeout", "dsh_interrupted_or_deadline")):
        return {"why": "研究执行器等待超时，本轮没有完成。已收到的请求结果和用量记录保留。",
                "todo": "先检查任务调用记录；如有结果不明的请求，须单独确认恢复，不要直接重复运行。",
                "action": "none"}
    if error.startswith('dsh_tools_unavailable'):
        return {'why': '研究执行器未加载 ProjectWatch 工具，本次模型请求未发送。',
                'todo': '请检查安装完整性或联系维护者修复运行时；已有结果保留，不要反复重试。',
                'action': 'none'}
    if error.startswith(("dsh_no_valid_submission", "dsh_runtime_error")):
        return {"why": "研究执行器已结束，但没有完成有效提交，不能视为已完成判断或专题已停止。",
                "todo": "请查看任务记录和校验原因，确认卡点后再决定下一步；已有信息保留。",
                "action": "none"}
    if error.startswith('watchdog_'):
        return {'why': '任务长时间没有进展，多次恢复后仍未完成。已有结果保留。',
                'todo': '请先检查后台或联系维护人员，避免反复执行同一任务。', 'action': 'none'}
    if error.startswith(("provider_backoff", "search_quota_exhausted", "search_access_denied", "search_key")):
        return {"why": "检索或外部服务拒绝了请求（常见原因：额度用完、Key 失效或被限流），系统已暂停调用该服务约 1 小时。",
                "todo": "先到“设置”里点“测试连接”；显示正常后回来点“重新运行”。", "action": "settings"}
    if error.startswith("connection_failed"):
        return {"why": "网络连接失败。", "todo": "检查网络或代理后点“重新运行”。", "action": "rerun"}
    if error.startswith("request_budget"):
        return {"why": "这一轮的请求次数到了上限，已经读到的内容都已保存。",
                "todo": "通常不用处理，下次定时更新会接着做；想马上补全可以点“重新运行”。", "action": "rerun"}
    if "拒绝分析" in error:
        return {"why": "模型服务商按其内容规则拒绝分析这批资料，其他结果不受影响。",
                "todo": "可以在“设置”里换一个模型后重新运行；暂不处理可收起提醒。", "action": "settings"}
    if "专业来源" in error:
        return {"why": "部分专业数据源（如烯牛、医药魔方）没有返回结果，成功的部分已经保存。",
                "todo": "打开项目“外部动态”页的专业来源查看缺口；处理后点“重新运行”，暂不处理可收起提醒。",
                "action": "rerun"}
    if "依据已变化" in error or "恢复监控" in error:
        return {"why": "任务运行期间公司信息或监控状态被修改，旧结果没有发布。", "todo": "点“重新运行”按最新信息再做一次。", "action": "rerun"}
    if error.startswith("内部错误"):
        return {"why": "程序处理时出错，已有结果保留。", "todo": "点“重新运行”；如果再次失败，请把这条记录反馈给开发。", "action": "rerun"}
    return {"why": "任务没有完成，已有结果保留。", "todo": "点“重新运行”再试一次，或收起提醒；收起不改变任务结果。", "action": "rerun"}
