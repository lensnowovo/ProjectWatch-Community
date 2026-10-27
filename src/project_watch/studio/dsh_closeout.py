"""Reserve bounded model turns for structured submission, never invent results."""
import hashlib
import json

from project_watch.studio.http import HttpError

STAGE = 'dsh_closeout_v1'
CLOSEOUT_TOOLS = frozenset({'pw_status', 'pw_context', 'pw_assess', 'pw_judge', 'pw_revise', 'pw_finish'})
RESERVE = 8


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
    return dict(requests=requests, reserve_model_calls=RESERVE,
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
        closing = status['phase'] == 'closeout' or (remaining is not None and remaining <= RESERVE)
        status['phase'] = 'closeout' if closing else 'research'
        advice = status
        execution.checkpoint(key, advice)
    if advice['phase'] == 'closeout':
        execution.checkpoint(STAGE, {'phase': 'closeout'})
        body = {**body, 'tools': [t for t in body.get('tools', [])
                                 if t.get('function', {}).get('name') in CLOSEOUT_TOOLS]}
        if not body['tools']:
            research.hold('dsh_tools_unavailable')
            raise HttpError('dsh_tools_unavailable')
        choice = body.get('tool_choice')
        if isinstance(choice, dict) and choice.get('function', {}).get('name') not in CLOSEOUT_TOOLS:
            body['tool_choice'] = 'auto'
    note = ('宿主请求预算（remaining 包含本次模型请求）：' + json.dumps(advice, ensure_ascii=False) +
            '\n在研究阶段优先解决 collection 的未执行范围与关键证据；无需逐条摘录全部候选。'
            '预留模型请求用于结构化收尾，预算不自动增加。'
            '进入 closeout 后使用已保存证据完成必要评估、判断、修订及 pw_finish；'
            '未完成项如实保留，不能把缺失证据或判断写成已完成。')
    return {**body, 'messages': [{'role': 'system', 'content': note}, *body.get('messages', [])]}
