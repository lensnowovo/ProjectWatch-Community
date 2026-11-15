"""Section drafts that weigh all material: extract per batch, consolidate, synthesize once.

A cumulative draft (each batch revising the one before) rewrote earlier conclusions
away, so a published section reflected roughly its last batch. Here every batch is
read on its own for candidate points (PARALLEL requests at a time), candidates are
consolidated in bounded merges, and one synthesis writes the sections from all of
them plus, in an incremental round, the published version. The draft keeps the
cumulative draft's shape, so review and publication are unchanged.

``drafted_fragments`` stays a prefix: the batches before the first one without an
extraction. Material after it may still inform this draft; a later round reads it again.
"""
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

from project_watch.studio.store import digest

MODE = 'analyze_draft_protocol_v1'
EXTRACT = 'analyze_extract_v1'
MERGE = 'analyze_merge_v1'
SYNTHESIS = 'analyze_synthesis_v1'
PARALLEL = 3  # independent requests of one job sent at the same time
FAILURE_LIMIT = 2  # after this many failed extractions, send no more (provider trouble)
POOL_CHARS = 48000  # serialized candidates per merge or synthesis request
MAX_LEVELS = 4
WEIGHTS = ('high', 'medium', 'low')
FIELDS = {'position': ('for', 'against', 'signals', 'peers'),
          'technology': ('route', 'academic', 'industry', 'risks'),
          'industry': ('drivers', 'headwinds'),
          'operations': ('for', 'against'),
          'chain': ('for', 'against', 'players')}
FAILED, NOT_SENT = object(), object()

_POINTS = ('输出 JSON：{"points": [{"section": "position", "field": "for", "text": "…", "refs": ["S12"], '
           '"weight": "high"}]}')

EXTRACT_SYSTEM = """你是一级市场投资研究员。materials 只是全部素材中的一批。请从本批素材中提取对各板块判断有用的候选论点，
之后会与其他批次的论点一起综合；这里不写总判断，不写总结。
板块与字段（fields 给出本次需要的板块）：
- position 竞争位置：for 支持公司领先的依据；against 相反依据；signals 竞品动态，写成“竞品名：做了什么（含时间）→ 对本公司意味着什么”；
  peers 素材中可与本公司对比的竞争对手，text 只写公司名。
- technology 技术方向：route 公司押注的技术路线；academic 学术界信号；industry 产业界信号；risks 技术路线风险。
- industry 行业发展：drivers 驱动；headwinds 阻力。
- operations 公司运营：for 向好；against 承压。
- chain 产业链：for 有利；against 不利；players 产业链上下游环节中的公司，写成“环节：公司名”。
要求：
- 每条只写一个能被所附来源直接支持的断言，refs 附本批素材编号（1–4 个）；数字保留主体、指标、期间和单位。
- weight 是对投资判断的重要性：high 为规模、份额、财务、获批、重大订单或政策等会改变判断的事实；其次 medium、low。
- 只提取有判断价值的内容，每个板块每个字段最多 6 条，宁缺毋滥；与本公司及其竞争、行业、产业链无关的素材跳过。
- 竞品的事不能写成本公司的事；旧闻写明时间。
"""

MERGE_SYSTEM = """你是一级市场投资研究员。candidates 是从同一家公司多批素材中提取的候选论点（按板块、字段分组），
只是全部候选的一部分。请整理：合并重复或说同一件事的论点，保留口径最完整、时间最新的表述，refs 取并集（最多 4 个）；
同一指标不同期间的数字分开保留；去掉价值低或已被更新信息取代的论点。
不得新增候选之外的事实，不得改动数字、主体和期间；refs 只能使用候选中出现的编号。
每个板块每个字段最多保留 8 条；weight 沿用，或按重要性调整。
"""

SYNTHESIS_NOTE = """
本次输入不是原文素材，而是从全部素材逐批提取、整理过的候选论点 candidates（按板块、字段分组，附来源编号和重要性 weight）；
sources 给出各编号的日期和标题。若提供 published，它是各板块上一次发布的版本，其论点同样有来源。
请基于全部候选（以及 published）写出各板块：
- 选出对投资判断最重要、证据最充分的论点，优先 weight 为 high 的；同一事实只写一次，数字以最新且口径一致的为准。
- published 中的论点，除非被更新的候选取代或推翻，应当保留；被推翻时写入相反依据或 change_if。
- verdict 和 summary 要综合全部论点，不能只依据其中一小部分。
- 不得引入候选与 published 之外的事实；refs 只能使用其中出现的编号。
- position.peers 从 peers 候选和竞品动态中选取；chain.outline 的 players 只能用 players 候选里的公司。"""


def _rules(system):
    """The drafting rules (data handling, periods, comparisons) without the output schema."""
    body = system.split('规则：', 1)[-1]
    return '规则：' + body.split('输出 JSON：', 1)[0] if '规则：' in system else ''


def extracting(execution):
    return (execution.read_checkpoint(MODE) or {}).get('mode') == 'extract'


def freeze(execution, batch, *, fresh):
    """Decide once per job; drafting already under way keeps the cumulative protocol.

    A retry of a collection batch whose earlier analysis drafted cumulatively keeps it,
    so a different request cannot sidestep an earlier unknown one.
    """
    if execution.read_checkpoint(MODE) is not None:
        return
    from project_watch.studio.analyze_job import DRAFT
    from project_watch.studio.execution import JobExecution
    mode = 'extract' if fresh else 'cumulative'
    with closing(execution.store._connect()) as db:
        if db.execute('SELECT 1 FROM studio_job_checkpoints WHERE job_id=? AND stage LIKE ? LIMIT 1',
                      (execution.job['id'], DRAFT + ':%')).fetchone():
            mode = 'cumulative'
        if mode == 'extract' and batch:
            for row in db.execute("SELECT id FROM studio_jobs WHERE project_id=? AND kind='analyze' "
                                  "AND input_version IN (?,?) AND id<>?",
                                  (execution.job['project_id'], 'collect:' + batch, 'redo:' + batch,
                                   execution.job['id'])).fetchall():
                sibling = JobExecution(execution.store, {'id': row[0]}).read_checkpoint(MODE, db=db)
                if (sibling or {}).get('mode') != 'extract':
                    mode = 'cumulative'
                    break
    execution.checkpoint(MODE, {'mode': mode, 'version': 1})


def _shape_error(message):
    from project_watch.studio.llm import ModelError
    return ModelError(message, code='model_output_invalid_shape')


def _clean(value, targets, codes):
    """Keep well-formed points on requested fields citing allowed codes; drop the rest."""
    if isinstance(value, dict):
        value = value.get('points')
    if not isinstance(value, list):
        raise _shape_error('candidate points must be a list')
    points = []
    for point in value:
        if not isinstance(point, dict):
            continue
        section, field, text = point.get('section'), point.get('field'), point.get('text')
        if section not in targets or field not in FIELDS.get(section, ()):
            continue
        if not isinstance(text, str) or not text.strip():
            continue
        refs = [r for r in dict.fromkeys(point.get('refs') or []) if isinstance(r, str) and r in codes][:4]
        if not refs:
            continue
        weight = point.get('weight') if point.get('weight') in WEIGHTS else 'medium'
        points.append({'section': section, 'field': field, 'text': text.strip()[:300], 'refs': refs,
                       'weight': weight})
    return points


def _nested(points, targets):
    out = {}
    for point in points:
        out.setdefault(point['section'], {}).setdefault(point['field'], []).append(
            {'text': point['text'], 'refs': point['refs'], 'weight': point['weight']})
    return {name: out[name] for name in targets if name in out}


def _size(points, targets):
    return len(json.dumps(_nested(points, targets), ensure_ascii=False))


def _codes(points):
    return {ref for point in points for ref in point['refs']}


def _send(execution, llm, requests, accept, *, failure_limit=None, after_wave=None):
    """Send unsaved requests PARALLEL at a time; return each key's value, FAILED or NOT_SENT.

    Each request keeps its own journal row, so an unknown one is never resent. The first
    unsaved request goes alone: on resume it may be the uncertain one. A terminal failure
    is recorded and isolated; anything else is raised once the wave's answers are saved.
    Yield only between waves, never with a request in flight.
    """
    from project_watch.studio.http import HttpError
    from project_watch.studio.llm import ModelError
    from project_watch.studio.review_source_traversal import safe_yield
    from project_watch.studio.step_outcomes import CHECKPOINT as FAILURES
    from project_watch.studio.step_outcomes import ISOLATABLE

    failures = execution.read_checkpoint(FAILURES) or {}
    results, pending = {}, []
    for request in requests:
        key = request[0]
        saved = execution.read_checkpoint(key)
        if saved is not None:
            results[key] = saved['value']
        elif 'draft:' + key in failures:
            results[key] = FAILED
        else:
            pending.append(request)
    failed = sum(value is FAILED for value in results.values())
    waves = [pending[:1]] + [pending[i:i + PARALLEL] for i in range(1, len(pending), PARALLEL)]
    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        for items in waves:
            if not items:
                continue
            if failure_limit is not None and failed >= failure_limit:
                break
            wave = [(request, pool.submit(llm.json, request[1], request[2], max_tokens=request[4],
                                          repair_invalid=False)) for request in items]
            error = None
            for request, future in wave:
                key = request[0]
                try:
                    value = accept(future.result(), request[3])
                except (ModelError, HttpError) as exc:
                    if exc.code not in ISOLATABLE:
                        error = error or exc
                        continue
                    failures['draft:' + key] = {'status': 'blocked', 'code': exc.code}
                    results[key] = FAILED
                    failed += 1
                    continue
                execution.checkpoint_chunked(key, {'value': value})
                results[key] = value
            if any(results.get(request[0]) is FAILED for request, _ in wave):
                execution.checkpoint_chunked(FAILURES, failures)
            if error is not None:
                raise error
            if after_wave is not None:
                after_wave(results)
            safe_yield(execution)
    return {request[0]: results.get(request[0], NOT_SENT) for request in requests}


def _extract(execution, llm, system, company, sources, context, targets, progress):
    from project_watch.studio.analyze_job import DRAFT_BATCH, LEGACY_BATCH, source_batches
    limits = execution.read_checkpoint(DRAFT_BATCH) or LEGACY_BATCH
    batches = list(source_batches(sources, limits))
    prompt = EXTRACT_SYSTEM + _rules(system) + '\n' + _POINTS
    fields = {name: list(FIELDS[name]) for name in targets if name in FIELDS}
    requests = []
    for index, batch in enumerate(batches):
        key = EXTRACT + ':' + str(index) + ':' + digest([context['id'], targets, batch])[:24]
        payload = dict(company=company, materials=batch, snapshot_id=context['id'], fields=fields)
        requests.append((key, prompt, json.dumps(payload, ensure_ascii=False), set(batch), 8192))

    def prefix(results):
        drafted = 0
        for (key, *_), batch in zip(requests, batches, strict=True):
            value = results.get(key)
            if value is None or value is FAILED or value is NOT_SENT:
                break
            drafted += len(batch)
        return drafted

    results = _send(execution, llm, requests, lambda value, codes: _clean(value, targets, codes),
                    failure_limit=FAILURE_LIMIT, after_wave=lambda results: progress(prefix(results)))
    drafted = prefix(results)
    points = [p for key, *_ in requests if isinstance(results[key], list) for p in results[key]]
    return points, drafted, drafted < len(sources)


def _order(points, targets):
    rank = {name: i for i, name in enumerate(targets)}
    return sorted(points, key=lambda p: (rank[p['section']], FIELDS[p['section']].index(p['field'])))


def _weight(point):
    """Serialized size of one candidate, close enough to pack requests without re-encoding."""
    return len(json.dumps({'text': point['text'], 'refs': point['refs'], 'weight': point['weight']},
                          ensure_ascii=False)) + 2


def _budget():
    return POOL_CHARS - min(2000, POOL_CHARS // 4)  # room for section and field keys


def _groups(points):
    groups, current, used = [], [], 0
    budget = _budget()
    for point in points:
        if current and used + _weight(point) > budget:
            groups.append(current)
            current, used = [], 0
        current.append(point)
        used += _weight(point)
    return groups + ([current] if current else [])


def _trim(points, targets):
    """The strongest candidates that fit one request, in their original order."""
    if _size(points, targets) <= POOL_CHARS:
        return points
    budget, used, keep = _budget(), 0, set()
    for i in sorted(range(len(points)), key=lambda i: WEIGHTS.index(points[i]['weight'])):
        if used + _weight(points[i]) <= budget:
            keep.add(i)
            used += _weight(points[i])
    return [points[i] for i in sorted(keep)]


def _consolidate(execution, llm, company, targets, points):
    """Merge candidates in rounds until they fit one synthesis request."""
    prompt = MERGE_SYSTEM + '\n' + _POINTS
    points = _order(points, targets)
    for level in range(MAX_LEVELS):
        if _size(points, targets) <= POOL_CHARS:
            break
        groups = _groups(points)
        requests = [(MERGE + ':' + str(level) + ':' + str(i) + ':' + digest([targets, group])[:24], prompt,
                     json.dumps(dict(company=company, candidates=_nested(group, targets)), ensure_ascii=False),
                     _codes(group), 8192) for i, group in enumerate(groups)]
        results = _send(execution, llm, requests, lambda value, codes: _clean(value, targets, codes))
        merged = []
        for (key, *_), group in zip(requests, groups, strict=True):
            value = results[key]
            merged.extend(value if isinstance(value, list) else group)  # an unmerged group passes up as is
        merged = _order(merged, targets)
        if _size(merged, targets) >= _size(points, targets):
            break
        points = merged
    return _trim(points, targets)


def _restrict_refs(value, allowed):
    if isinstance(value, dict):
        return {k: ([r for r in v if r in allowed] if k == 'refs' and isinstance(v, list)
                    else _restrict_refs(v, allowed)) for k, v in value.items()}
    if isinstance(value, list):
        return [_restrict_refs(v, allowed) for v in value]
    return value


def _cited(value):
    if isinstance(value, dict):
        return {r for k, v in value.items() for r in (v if k == 'refs' and isinstance(v, list) else _cited(v))
                if isinstance(r, str)}
    if isinstance(value, list):
        return {r for v in value for r in _cited(v)}
    return set()


def _synthesize(execution, llm, system, company, targets, points, seed, sources, context):
    allowed = _codes(points) | _cited(seed or {})
    index = {code: ' | '.join(filter(None, [str(sources[code].get('date') or '')[:10],
                                             str(sources[code].get('title') or '')[:60]]))
             for code in sorted(allowed, key=lambda c: int(c[1:]) if c[1:].isdigit() else 0) if code in sources}
    payload = dict(company=company, snapshot_id=context['id'], requested_sections=targets,
                   candidates=_nested(points, targets), sources=index)
    if seed:
        payload['published'] = seed
    key = SYNTHESIS + ':' + digest([context['id'], targets, points, seed or {}])[:24]

    def accept(value, _):
        if not isinstance(value, dict):
            raise _shape_error('analysis draft must be an object')
        return _restrict_refs(value, allowed)
    results = _send(execution, llm, [(key, system + SYNTHESIS_NOTE, json.dumps(payload, ensure_ascii=False),
                                      allowed, 8192)], accept)
    value = results[key]
    return value if isinstance(value, dict) else None


def draft(execution, llm, system, company, sources, context, targets, seed=None):
    """The section draft over ``sources``; ``seed`` is the published version an incremental round extends."""
    from project_watch.studio.analyze_job import COVERAGE
    carried = context['coverage'].get('carried_fragments', 0)

    def progress(drafted, **extra):
        execution.checkpoint_chunked(COVERAGE, {
            **context['coverage'], 'processed_fragments': carried + drafted, 'drafted_fragments': drafted,
            'remaining_fragments': len(sources) - drafted, **extra})
    if not sources:
        progress(0)  # an incremental round with nothing new
        return dict(seed or {})
    points, drafted, blocked = _extract(execution, llm, system, company, sources, context, targets, progress)
    if not points or (seed and blocked and not drafted):
        # Nothing new to weigh, or a published seed stands because its first new batch is
        # missing (seed_stands): a synthesis would be paid for and then discarded.
        progress(drafted, **({'blocked': True} if blocked else {}))
        return dict(seed or {})
    points = _consolidate(execution, llm, company, targets, points)
    value = _synthesize(execution, llm, system, company, targets, points, seed, sources, context)
    if value is None:
        # Without a synthesis nothing new is drafted; a published seed stands as it is.
        progress(0, blocked=True)
        return dict(seed or {})
    progress(drafted, **({'blocked': True} if blocked else {}))
    return value
