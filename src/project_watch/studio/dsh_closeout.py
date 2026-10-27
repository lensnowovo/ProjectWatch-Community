"""Reserve bounded model turns for structured submission, never invent results."""
import hashlib
import json

from project_watch.studio.http import HttpError

STAGE = 'dsh_closeout_v1'
CLOSEOUT_TOOLS = frozenset({'pw_status', 'pw_context', 'pw_assess', 'pw_judge', 'pw_revise', 'pw_finish'})
RESERVE = 8


def next_submission(research):
    """Select an existing submission handler from persisted prerequisites.

    The host selects the operation, never its evidence, judgment or arguments.
    Tool validation and review still decide whether that operation can commit.
    """
    if 'pw_context' not in research.tools:
        return 'pw_finish'
    if research.state.get('judged'):
        return 'pw_finish' if research.state.get('reviewed') else 'pw_revise'
    from project_watch.studio.dsh_bootstrap import digest
    if research.state['records'] and research.state.get('published') != digest(research.state['records']):
        return 'pw_context'
    try:
        ctx = research.frozen()
    except ValueError:
        return 'pw_context'
    return 'pw_assess' if research.pending_assessments(ctx) else 'pw_judge'


def budget(research):
    execution = research.execution
    admission = execution.read_checkpoint('collection_admission_v1') or {}
    if not admission.get('enabled'):
        return None  # Do not retroactively change already-started legacy rounds.
    usage = execution.usage()
    requests = {}
    for kind, limit in execution.budgets.items():
        used = usage.get(kind, {}).get('requests', 0)
        requests[kind] = dict(limit=limit, used=used, remaining=None if limit is None else max(0, limit - used))
    from project_watch.studio.dsh_record_review import pending_calls
    record_review = pending_calls(research)
    return dict(requests=requests, reserve_model_calls=RESERVE + record_review,
                pending_record_review_calls=record_review,
                phase=(execution.read_checkpoint(STAGE) or {}).get('phase', 'research'))


def guard_tool(research, name):
    if (research.execution.read_checkpoint(STAGE) or {}).get('phase') == 'closeout' and name not in CLOSEOUT_TOOLS:
        raise ValueError('budget closeout: use saved evidence for judgment, revision and finish; report unfinished coverage')


def finish_saved_monitor(research):
    """Complete local bookkeeping only after accepted judgment and revision.

    This is not model recovery: held/unknown requests remain blocked by tool().
    The existing finish handler still computes partial coverage and stale inputs.
    """
    status = budget(research)
    if (status is None or status['phase'] != 'closeout'
            or status['requests']['model']['remaining'] != 0
            or not research.state.get('judged') or not research.state.get('reviewed')):
        return False
    research.tool('pw_finish', {'reason': '模型预算已用完；根据已保存的判断和修订执行本地收尾。'})
    research.execution.checkpoint('dsh_host_finish_v1', {
        'origin': 'budget_closeout', 'status': research.state['finished']['status'],
        'model_requests': status['requests']['model']['used'],
        'finish_event_ordinal': research.state['events'][-1]['ordinal'],
    })
    return True


def prepare_model(research, body):
    status = budget(research)
    if status is None:
        return body
    execution = research.execution
    # Freeze the advice with the original request. Replaying an identical SDK
    # request must keep its HTTP fingerprint and reuse the settled journal reply.
    key = 'dsh_budget_advice:' + hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    advice = execution.read_checkpoint(key)
    if advice is None:
        remaining = status['requests']['model']['remaining']
        if remaining == 0:
            research.hold('request_budget')
            raise HttpError('request_budget')
        closing = status['phase'] == 'closeout' or (
            remaining is not None and remaining <= status['reserve_model_calls'])
        status['phase'] = 'closeout' if closing else 'research'
        if closing:
            status['next_tool'] = next_submission(research)
        advice = status
        execution.checkpoint(key, advice)
    if advice['phase'] == 'closeout':
        execution.checkpoint(STAGE, {'phase': 'closeout'})
        # Old frozen advice retains its original payload for journal replay.
        allowed = {advice['next_tool']} if advice.get('next_tool') else CLOSEOUT_TOOLS
        body = {**body, 'tools': [t for t in body.get('tools', [])
                                 if t.get('function', {}).get('name') in allowed]}
        if not body['tools']:
            research.hold('dsh_tools_unavailable')
            raise HttpError('dsh_tools_unavailable')
        choice = body.get('tool_choice')
        if isinstance(choice, dict) and choice.get('function', {}).get('name') not in allowed:
            body['tool_choice'] = 'auto'
    note = ('宿主请求预算（remaining 包含本次模型请求）：' + json.dumps(advice, ensure_ascii=False) +
            '\n在研究阶段优先解决 collection 的未执行范围与关键证据；无需逐条摘录全部候选。'
            '预留模型请求用于结构化收尾，预算不自动增加。'
            '进入 closeout 后使用已保存证据完成必要评估、判断、修订及 pw_finish；'
            '未完成项如实保留，不能把缺失证据或判断写成已完成。')
    return {**body, 'messages': [{'role': 'system', 'content': note}, *body.get('messages', [])]}
