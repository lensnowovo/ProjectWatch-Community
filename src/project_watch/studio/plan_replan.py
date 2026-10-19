"""方案修订 (P4): after a monitoring round or newly analysed materials, the model proposes how the plan should grow.

Inputs are what the round produced: important events no focus point caught, the new hits per item,
deep-analysis gaps and "what would change the judgement", and — for the materials trigger — what the
new documents changed. Output is bounded and validated before anything is stored:

* add       → new focus point as a revertible low-cadence trial (at most ``MAX_ADDS`` per round);
* note      → refreshes an item's “当前认识” (system-maintained summary, logged and revertible);
* reduce / retire / promote / restore
            → applied directly only to the system's own trial items; for items a person added or
              reviewed, stored as a proposal the owner accepts or rejects.

Every change must cite the event ids (E#), material changes (M#) or gaps (G#) it rests on; uncited or
unknown ids are dropped. The plan's “不进入” scope is part of the prompt.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta

from project_watch.studio.monitoring_plan import (
    AXIS_LABEL,
    FRAMEWORK,
    MAX_HOP,
    MODULES,
    SECTION_OF,
    MonitoringPlan,
)
from project_watch.studio.plan_templates import catalog


def _same_question(a: str, b: str, names: list[str]) -> bool:
    """A reworded tracked question: equal once the company's own name and punctuation are dropped, or one
    contains the other and covers at least 80% of it. (Headline dedupe stays exact; this is only for questions.)"""
    def norm(text: str) -> str:
        for name in sorted(names, key=len, reverse=True):
            text = text.replace(name, "")
        return re.sub(r"[\s\W_]+", "", text.casefold())
    x, y = norm(a), norm(b)
    if not x or not y:
        return False
    short, long = sorted((x, y), key=len)
    return x == y or (short in long and len(short) >= 0.8 * len(long))


def _view(row: dict) -> tuple[str, dict]:
    """A display module the item can actually fill: commitments need stated claims, a metric series needs a metric."""
    view = row.get("view") if row.get("view") in MODULES else "evidence"
    if view == "commitments" and not row.get("commitments"):
        return "evidence", {}
    if view == "metric_series":
        metric = str(row.get("metric") or "").strip()
        if not metric:
            return "evidence", {}
        return view, {"metric": metric[:40], "unit": str(row.get("unit") or "")[:20]}
    if view == "commitments":
        claims = [{"claim": str(c.get("claim") if isinstance(c, dict) else c)[:120], "keywords": []}
                  for c in row.get("commitments")[:6]]
        return view, {"commitments": claims}
    return view, {}

_log = logging.getLogger(__name__)
MAX_ADDS = 3
MAX_EVENTS = 30

SYSTEM = """你是投资机构的投后研究员，负责维护一家公司的“监控方案”。方案按四圈九类组织（asset 资产与产品、operations 经营团队与资金（公司本体）；peers 同路线竞品、alternatives 同需求替代（竞争）；customer 客户与渠道、partners 供应合作与资本方（产业链）；market 行业与市场、science 学术与技术证据、rules 政策与规则（环境）），每类下有若干关注点（W 编号）。按监控对象归类，不按信息来源：竞品的论文归 peers，不归 science；science 只放整条路线的证据和团队学术产出。
根据本轮新出现的信息，判断方案应当如何生长或收缩。只能依据给出的编号材料：事件 E#、新资料变化 M#、分析缺口 G#。
先处理临时专题的退出条件，再考虑拓展监控。既有专题依据虽然不是新动态，仍可回答“是否曾获批、是否已有量产”等问题。
逐题区分已经完成、仅有计划和证据不足；有本题依据且满足退出条件就输出 finish，不必继续寻找新新闻。
答案只保留回答本题所需的事实，逐句对应 refs；不要为丰富答案添加未被所引材料支持的数字、客户或阶段。

可以输出的修订（没有必要就少输出，宁缺毋滥）：
- add：新增关注点。只在出现了现有关注点覆盖不到、且确实影响这家公司的问题时新增（例如新出现的竞品读出、新的替代疗法、
  新资料披露的关键客户或原料依赖、分析缺口指出的关键未知）。每轮最多 3 个。不得超出“不进入”范围，不得与现有关注点重复。
  需给出：section（九类之一的英文键）、text（≤40 字的问题）、label（≤8 字）、objects（具体对象）、queries（2-3 个检索词，
  竞品/需求/技术类不必含公司名）、reason（与公司的关系）、view（evidence|timeline|peer_table|metric_series|commitments）。
  关联链：每个新增关注点必须用 from 写明关联来源的 W 编号（例如从“TEAD 路线”延伸到某个新竞品资产）；不能省略来源重置层级。
  链条最多延伸 2 层（公司自身 → 直接相关 → 二阶相关），已在第 2 层的关注点不能再延伸。
  reason 同样写成因果句：说明它和公司的具体关系，再说明因此要看什么：如“BeiCell 试剂盒是公司主要的在售产品，所以看采用、复购与客户集中度”“Vertex 的 VX-880 与公司在同一适应症（1 型糖尿病）用同类细胞疗法，所以看其读出”。关系要落到具体类型：主要产品/核心资产、同一适应症或同一靶点的竞品、替代同一需求的方案、关键客户、关键原料或供应商、投资方或合作方、监管路径；不写“很重要”“核心资产”这类没有因果的空话。
- explore：新资料或新信息指向一个值得专门探索、但尚不确定是否需要长期跟踪的方向时，开一个临时专题（字段同 add，
  另给 exit：什么条件下结束这个专题）。临时专题探完就关闭或转成常规关注点。
- note：更新某个关注点的“当前认识”（一句话，≤80 字，写清新变化）。只在本轮命中带来实质新信息时更新。
- finish：仅对临时专题，在本题的退出条件已满足时结束本次探索。给 item、text（答案）、refs（本题的 E/M 依据）、why（怎样满足退出条件）。
  text 只写直接回答题目的一至两句，不写公司综述；题目未问的产能、营收、排名、份额、累计数量不要附带。
  数字和单位沿用所引标题/摘要的写法，不自行换算、取整或补齐日期；事件日期只是时间索引，不当成批准/交付日期。
  一个已发生的案例足以满足退出条件时，不继续拼接将来的订单/定点当成已完成采用。
  “有进展”不等于回答完成；分析缺口 G 不能作为完成依据。结束不关闭常规监控，相关新证据会重新唤醒。
- reduce / retire：某关注点已结束、被证伪或长期无关时降频或关闭；restore：恢复；promote：升为核心。
每条修订必须给 refs（引用的 E#/M#/G#）和 why（一句话理由）。不写投资建议，不把计划写成已完成，不编造数字。
输出 JSON：{"changes":[{"action":"add","from":"W1","section":"peers","text":"","label":"","objects":[],"queries":[],
"reason":"","view":"evidence","refs":["E3"],"why":""},{"action":"note","item":"W2","text":"","refs":["E5"],"why":""},
{"action":"reduce","item":"W4","refs":["E1"],"why":""},
{"action":"finish","item":"W5","text":"已完成交付；资料记载客户甲已验收该产品。","refs":["E6"],"why":"交付及客户验收记录满足退出条件"}]}"""


def gather(pipeline, project_id: str, *, since: str, trigger: str) -> dict | None:
    """New plan signals plus routed history for currently active finite topics.

    Topic answers can depend on older approvals/adoption, not just recent news.
    History is labelled separately and never becomes a new event or a wake signal.
    """
    plan = MonitoringPlan(pipeline.monitor)
    states = plan.explorations.view(project_id)
    items = [w for w in plan.items(project_id) if w["status"] != "retired"
             and (w['id'] not in states or not plan.explorations.managed(w)
                  or states[w['id']]['state'] == 'active')]
    if not items:
        return None
    meta = plan.meta(project_id)
    watch_keys = {f"W{n + 1}": w for n, w in enumerate(items)}
    numbered: dict[str, dict] = {}
    store = pipeline.store
    with store._connect() as db:
        watch_events: dict[str, set] = {}
        for watch_id, event_id in db.execute(
                "SELECT DISTINCT h.watch_id, i.event_id FROM studio_watch_hits h JOIN studio_items i ON i.id=h.item_id "
                "JOIN studio_watchpoints w ON w.id=h.watch_id WHERE w.project_id=? AND i.project_id=w.project_id "
                "AND i.status!='dismissed' AND i.event_id IS NOT NULL", (project_id,)):
            watch_events.setdefault(watch_id, set()).add(event_id)
        hit_events = set().union(*watch_events.values()) if watch_events else set()
        new_hits = {}
        for watch_id, title in db.execute(
                "SELECT h.watch_id, i.title FROM studio_watch_hits h JOIN studio_items i ON i.id=h.item_id "
                "JOIN studio_watchpoints w ON w.id=h.watch_id WHERE w.project_id=? AND h.created_at>=? "
                "ORDER BY h.created_at DESC", (project_id, since)):
            new_hits.setdefault(watch_id, []).append(title)
    events = [e for e in store.list_events(project_id=project_id, since_days=30, limit=200)
              if (e.get("created_at") or "") >= since and e.get("importance") in ("major", "notable")]
    lines = []
    for e in events[:MAX_EVENTS]:
        key = f"E{len([k for k in numbered if k.startswith('E')]) + 1}"
        summary = str(e.get("summary") or "")
        numbered[key] = {"kind": "event", "id": e["id"], "title": e["title"], "text": summary}
        caught = "已被关注点命中" if e["id"] in hit_events else "未被任何关注点覆盖"
        lines.append(f"[{key}] {e['title']}｜{e.get('category')}/{e.get('importance')}｜{caught}"
                     + (f"｜摘要：{summary}" if summary else "")
                     + (f"｜意味着：{e['impact'][:120]}" if e.get("impact") else ""))
    topic_lines = []
    event_keys = {v['id']: k for k, v in numbered.items() if v['kind'] == 'event'}
    for wkey, watch in watch_keys.items():
        if watch.get('tier') != 'topic' or states.get(watch['id'], {}).get('state') != 'active':
            continue
        # Scope to this project's non-dismissed routed public events. Routine
        # importance and old publication dates do not make an answer irrelevant.
        ids = sorted(watch_events.get(watch['id'], set()))
        historical = store.list_events(project_id=project_id, event_ids=ids, limit=MAX_EVENTS)
        if len(ids) > MAX_EVENTS:
            topic_lines.append(f"[{wkey}] 已关联 {len(ids)} 个事件，本次展示最近 {MAX_EVENTS} 个；未展示不等于无证据。")
        for e in historical:
            key = event_keys.get(e['id'])
            if key is None:
                key = f"E{len(event_keys) + 1}"
                numbered[key] = {'kind': 'event', 'id': e['id'], 'title': e['title'],
                                 'text': str(e.get('summary') or '')}
                event_keys[e['id']] = key
            topic_lines.append(f"[{key}] 关联 {wkey}｜{e['title']}｜摘要：{e.get('summary') or '（无摘要）'}"
                               f"｜来源：{e.get('url') or '未记录'}")
    material = []
    if trigger == "internal":
        for change in (store.doc_changes(project_id, 1) or [{}])[0].get("changes", [])[:12]:
            text = str(change.get("text") or "")[:200]
            if text:
                key = f"M{len(material) + 1}"
                numbered[key] = {"kind": "material", "text": text}
                material.append(f"[{key}] {change.get('kind', '')}：{text}")
    gaps = []
    try:
        from project_watch.studio import insight

        record = insight.load(store, project_id) or {}
        analysis = record.get("analysis") or {}
        for text in [*(analysis.get("gaps") or []), *((analysis.get("position") or {}).get("change_if") or [])][:8]:
            key = f"G{len(gaps) + 1}"
            numbered[key] = {"kind": "gap", "text": str(text)[:200]}
            gaps.append(f"[{key}] {str(text)[:200]}")
    except Exception:  # deep analysis is optional input
        gaps = []
    if not lines and not material and not topic_lines:
        return None
    plan_lines = []
    for key, w in watch_keys.items():
        hits = new_hits.get(w["id"], [])
        plan_lines.append(f"[{key}] {AXIS_LABEL.get(w['axis'], w['axis'])}｜第 {w.get('hop') or '0'} 层｜{w['text']}｜对象：{'、'.join(w['objects']) or '—'}"
                          f"｜状态：{w['status']}/{w['tier']}｜来源：{w.get('plan_origin') or w.get('origin')}"
                          f"｜当前认识：{(w.get('current_state') or '—')[:100]}｜本轮新命中 {len(hits)} 条"
                          f"｜触发条件：{w.get('trigger_rule') or '—'}｜退出条件：{w.get('exit_rule') or '—'}"
                          + (f"：{'；'.join(t[:40] for t in hits[:3])}" if hits else ""))
    project = store.get_project(project_id) or {}
    # A company name alone does not connect an office move to a clinical question.
    # Reuse routing's topic terms and identifier boundaries, without another model call.
    own_keys = {k.casefold() for k in plan._keys(project_id, {"axis": "asset"})}
    for w in items:
        terms = {k.casefold() for k in plan._keys(project_id, w)} - own_keys
        terms.update(t.casefold() for t in pipeline.questions._material_terms(w, project))
        terms -= own_keys
        for entry in numbered.values():
            if entry["kind"] in ("material", "gap") and any(
                    pipeline.questions._term_match(term, entry["text"].casefold()) for term in terms):
                entry.setdefault("watch_ids", []).append(w["id"])
    text = "\n".join([
        f"公司：{project.get('legal_name') or project.get('name')}",
        f"项目理解：{meta.get('understanding') or project.get('description') or '—'}",
        f"不进入：{'；'.join(meta.get('out_of_scope') or []) or '—'}",
        "四圈九类：" + "、".join(f"{k}={label}" for k, label, _b in FRAMEWORK),
        "\n现有关注点：", *plan_lines,
        "\n本轮新事件：", *(lines or ["（无）"]),
        *(["\n既有专题依据（不是本轮新进展，仅用于回答对应专题）：", *topic_lines] if topic_lines else []),
        *(["\n新资料带来的变化：", *material] if material else []),
        *(["\n深度分析的缺口与判断改变条件：", *gaps] if gaps else []),
    ])
    return {"text": text, "numbered": numbered, "items": watch_keys, "watch_events": watch_events,
            "explorations": states,
            "major": any(e.get("importance") == "major" for e in events[:MAX_EVENTS]),
            "keys": {w["id"]: [k.casefold() for k in plan._keys(project_id, w)] for w in items}}


def run(pipeline, llm, project_id: str, *, since: str, trigger: str) -> dict:
    """One bounded model call; validated changes are applied or proposed. Returns counts for the job log."""
    plan = MonitoringPlan(pipeline.monitor)
    plan.explorations.refresh(project_id, pipeline.questions)
    gathered = gather(pipeline, project_id, since=since, trigger=trigger)
    if gathered is None or not getattr(llm, "ready", False):
        return {"skipped": True}
    # Steady state: a stable plan is revisited only for major news or new materials (saves a model call per round).
    if (trigger == "external" and plan.state(project_id)["phase"] == "stable"
            and not any(plan.explorations.managed(w) and gathered.get('explorations', {}).get(w['id'], {}).get('state') == 'active'
                        for w in gathered['items'].values())
            and not gathered.get("major") and not any(v["kind"] == "material" for v in gathered["numbered"].values())):
        return {"skipped": True, "reason": "stable"}
    data = llm.json(SYSTEM, gathered["text"], max_tokens=2500)
    rows = data.get("changes") if isinstance(data, dict) else None
    counts = apply(plan, project_id, rows if isinstance(rows, list) else [], gathered, trigger=trigger,
                   numbers_ok=lambda note, event_ids, texts: pipeline.questions._numbers_supported(project_id, note, event_ids, texts))
    if not isinstance(rows, list):
        counts['incomplete'] = 1
    if trigger == 'external':
        report = (pipeline.store.get_plan(project_id) or {}).get('last_report') or {}
        if any(r.get('errors') or r.get('deferred') for r in report.values() if isinstance(r, dict)):
            counts['incomplete'] = 1
    plan.record_revision(project_id, counts, trigger)
    return counts


def _about(gathered: dict, watch: dict, ref: str) -> bool:
    """A cited event counts for a focus point only if that point caught it, or its headline names the point's
    objects (or, for the company's own points, the company). Materials and gaps need a topic/object link."""
    entry = gathered["numbered"][ref]
    if entry["kind"] != "event":
        return watch["id"] in entry.get("watch_ids", [])
    if entry["id"] in gathered.get("watch_events", {}).get(watch["id"], set()):
        return True
    title = str(entry.get("title") or "").casefold()
    return any(k in title for k in gathered.get("keys", {}).get(watch["id"], []))


def apply(plan: MonitoringPlan, project_id: str, rows: list, gathered: dict, *, trigger: str, numbers_ok=None) -> dict:
    counts = {"added": 0, "noted": 0, "applied": 0, "proposed": 0, "dropped": 0}
    rejected: list[str] = []
    numbered, items = gathered["numbered"], gathered["items"]
    existing = {w["text"] for w in plan.items(project_id)}
    origin = "internal" if trigger == "internal" else "system"
    for row in rows[:12]:
        if not isinstance(row, dict):
            counts["dropped"] += 1
            continue
        refs = [r for r in row.get("refs") or [] if isinstance(r, str) and r in numbered]
        why = str(row.get("why") or "").strip()[:200]
        if not refs or not why:
            counts["dropped"] += 1
            continue
        evidence = [{"ref": r, **{k: v for k, v in numbered[r].items() if k in ("kind", "id", "title", "text")}} for r in refs]
        action = row.get("action")
        try:
            if action in ("add", "explore"):
                section = row.get("section")
                text = str(row.get("text") or "").strip()
                if counts["added"] >= MAX_ADDS or section not in SECTION_OF or not 4 <= len(text) <= 60 or text in existing:
                    counts["dropped"] += 1
                    continue
                parent = items.get(str(row.get("from") or "")) if row.get("from") else None
                if parent is None:
                    counts["dropped"] += 1
                    continue
                # A sleeping topic cannot extend its chain using a stale model response.
                if plan.explorations.managed(parent) and not plan.explorations.runnable(parent):
                    counts['dropped'] += 1
                    continue
                parent_run = gathered.get('explorations', {}).get(parent['id'])
                parent_generation = parent_run['generation'] if parent_run else None
                if int(parent.get('hop') or 0) >= MAX_HOP:
                    plan.explorations.stop(parent, 'relation_limit', evidence=[e['ref'] for e in evidence], generation=parent_generation)
                    counts['dropped'] += 1
                    continue
                live = [w for w in plan.items(project_id) if w['status'] != 'retired']
                from project_watch.studio.monitoring_plan import MAX_ITEMS, MAX_PER_SECTION
                if len(live) >= MAX_ITEMS or sum(w['axis'] == section for w in live) >= MAX_PER_SECTION:
                    plan.explorations.stop(parent, 'capacity_limit', evidence=[e['ref'] for e in evidence], generation=parent_generation)
                    counts['dropped'] += 1
                    continue
                explore = action == "explore"
                plan.add(project_id, {
                    "text": text, "label": str(row.get("label") or text[:8])[:12], "axis": section,
                    "objects": [str(o) for o in row.get("objects") or []][:6],
                    "queries": [str(q) for q in row.get("queries") or []][:3], "reason": str(row.get("reason") or why)[:300],
                    "view": _view(row)[0], "view_params": _view(row)[1],
                    "tier": "topic" if explore else "background",
                    "exit_rule": str(row.get("exit") or ("探索完成后关闭或转为常规关注点" if explore else ""))[:300],
                    "parent_id": parent["id"],
                }, actor="system", origin=origin, reason=why, evidence=evidence,
                   expected_parent_version=str(parent.get('lifecycle_version', '0')), expected_parent_generation=parent_generation)
                existing.add(text)
                counts["added"] += 1
                continue
            watch = items.get(str(row.get("item") or ""))
            if watch is None:
                counts["dropped"] += 1
                continue
            if plan.explorations.managed(watch) and not plan.explorations.runnable(watch):
                counts['dropped'] += 1
                continue
            if action in ("note", "finish"):
                note = str(row.get("text") or "").strip()[:200]
                # The summary must rest on this point's own material, and every figure must appear in it.
                refs = [r for r in refs if _about(gathered, watch, r)]
                if action == 'finish':
                    refs = [r for r in refs if numbered[r]['kind'] in ('event', 'material')]
                    if watch.get('tier') != 'topic' or not watch.get('exit_rule'):
                        counts['dropped'] += 1
                        continue
                texts = ["\n".join(str(numbered[r].get(k) or "") for k in ("title", "text")) for r in refs]
                if not note or not refs:
                    rejected.append("note: 依据与关注点无关")
                    counts["dropped"] += 1
                    continue
                # Validate the frozen numbered input the model actually received, not unseen
                # linked bodies or a database event that may have changed during the call.
                if numbers_ok is not None and not numbers_ok(note, [], texts):
                    rejected.append("note: 数字在依据中找不到")
                    counts["dropped"] += 1
                    continue
                evidence = [e for e in evidence if e["ref"] in refs]
                snapshot = gathered.get('explorations', {}).get(watch['id'])
                plan.update(watch["id"], {"current_state": note}, actor="system", reason=why, evidence=evidence,
                            expected_lifecycle_version=str(watch.get('lifecycle_version', '0')),
                            expected_exploration_generation=snapshot['generation'] if snapshot else None,
                            exploration_answer=action == 'finish')
                counts["noted"] += 1
                if action == 'finish':
                    counts['applied'] += 1
                continue
            fields = {"reduce": {"status": "reduced"}, "retire": {"status": "retired"},
                      "restore": {"status": "active"}, "promote": {"tier": "core"}}.get(action)
            if fields is None:
                counts["dropped"] += 1
                continue
            if plan._owned_by_people(watch) or action == "promote":
                if plan.propose(watch["id"], fields, reason=why, evidence=evidence,
                                expected_lifecycle_version=str(watch.get('lifecycle_version', '0'))):
                    counts["proposed"] += 1
            else:
                plan.update(watch["id"], fields, actor="system", reason=why, evidence=evidence,
                            expected_lifecycle_version=str(watch.get('lifecycle_version', '0')))
                counts["applied"] += 1
        except (ValueError, PermissionError, KeyError) as exc:
            _log.info("replan change dropped: %s", exc)
            counts["dropped"] += 1
    if rejected:
        _log.info("replan notes dropped: %s", rejected)
    return counts


STATE_KEY = "plan_replan:{}"


def since_last(store, project_id: str) -> str:
    """Everything new since the previous replan (first run: the last 7 days)."""
    return store.get_state(STATE_KEY.format(project_id)) or (datetime.now(UTC) - timedelta(days=7)).isoformat()


def mark(store, project_id: str) -> None:
    store.set_state(STATE_KEY.format(project_id), datetime.now(UTC).isoformat())


# ---------------------------------------------------------------- 名字 → 首版方案
BOOTSTRAP = """你是投资机构的投后研究员。现在只知道一家公司的名称和首轮收集到的资料，请为它设计一份“监控方案”的首版。
方案在统一的四圈九类框架下组织：asset 资产与产品、operations 经营团队与资金（公司本体）；peers 同路线竞品、alternatives 同需求替代（竞争）；customer 客户与渠道、partners 供应合作与资本方（产业链）；market 行业与市场、science 学术与技术证据、rules 政策与规则（环境）。
按监控对象归类，不按信息来源：竞品的论文归 peers，不归 science；science 只放整条路线的证据和团队学术产出。

要求：
- 先判断业务，再选行业模板（从给出的模板中选 1-2 个，按真实产品与商业模式选，不能只看行业标签）。
- understanding：2-3 句话说清公司做什么、价值线有哪些、哪些需要分开看；in_scope / out_of_scope 写清边界。
- sections：每一类写 known（本项目已知的基础信息，没有就留空）和 coverage（covered|partial|internal|none；需要内部资料才能看清的写 internal）。
- items：首批 6-12 个关注点，覆盖关键价值线，并沿关联链向外看：竞品要落到具体资产/产品，需求要看同一需求的替代方案，供应链要落到关键原料或客户。
  每个关注点给 section、text（≤40 字问题）、label（≤8 字）、objects、queries（2-3 个检索词；竞品/需求/技术/供应链类不必含公司名）、
  reason（与公司的关系）、view（evidence|timeline|peer_table|metric_series|commitments；metric_series 须另给 metric 与 unit，
  commitments 须另给 commitments 列表——资料里公司自己说过的承诺；给不出就用 evidence）、tier（core|background）、cadence（high|medium|low）、
  hop（0 公司自身 / 1 直接相关 / 2 二阶相关，最多 2）、trigger（出现什么就提醒）、exit（何时关闭或降频）。
- “已有跟踪问题”里值得保留的，在 items 里原样写出它的 text（可补全其他字段）；没写出的会并入首版方案后关闭。
- reason 写成因果句，说明它和公司的具体关系，再说明因此要看什么：如“BeiCell 试剂盒是公司主要的在售产品，所以看采用、复购与客户集中度”“Vertex 的 VX-880 与公司在同一适应症（1 型糖尿病）用同类细胞疗法，所以看其读出”。关系要落到具体类型：主要产品/核心资产、同一适应症或同一靶点的竞品、替代同一需求的方案、关键客户、关键原料或供应商、投资方或合作方、监管路径；不写“很重要”“核心资产”这类没有因果的空话。
- 只依据给出的资料；资料里没有的事实不写进 known，不编造数字。
输出 JSON：{"templates":["innovative_drug"],"understanding":"","in_scope":[],"out_of_scope":[],
"sections":{"asset":{"known":"","coverage":"partial"}},"items":[{"section":"asset","text":"","label":"","objects":[],"queries":[],
"reason":"","view":"timeline","tier":"core","cadence":"medium","hop":0,"trigger":"","exit":""}]}"""


def bootstrap_input(pipeline, project_id: str) -> str | None:
    store = pipeline.store
    project = store.get_project(project_id) or {}
    if not project:
        return None
    dossier = store.get_public_profile(project_id) or {}
    plan = store.get_plan(project_id) or {}
    facts = [f"- {f.get('label')}：{f.get('text')}" for f in (dossier.get("facts") or [])[:24]]
    events = [f"- {e['title']}（{e.get('category')}）" for e in store.list_events(project_id=project_id, limit=30)
              if e.get("importance") in ("major", "notable")][:20]
    templates = [f"- {t['key']}：{t['label']}。{t['method']}" for t in catalog()]
    legacy = [f"- {w['text']}" for w in MonitoringPlan(pipeline.monitor).items(project_id)
              if w["status"] != "retired" and not w.get("plan_origin")]
    return "\n".join([
        f"公司：{project.get('legal_name') or project.get('name')}（简称 {project.get('short_name') or '—'}）",
        f"行业标签：{project.get('industry') or '—'}；简介：{project.get('description') or '—'}",
        "首轮检索方案中的竞品：" + ("、".join(c.get("name", "") for c in plan.get("competitors") or []) or "—"),
        "\n公开档案：", *(facts or ["（无）"]),
        "\n首轮重要动态：", *(events or ["（无）"]),
        "\n已有跟踪问题：", *(legacy or ["（无）"]),
        "\n可选行业模板：", *templates,
    ])


def bootstrap(pipeline, llm, project_id: str) -> dict:
    """Name → first plan: templates, summary, section baselines and 6-12 focus points, all marked “系统首版，待审阅”."""
    plan = MonitoringPlan(pipeline.monitor)
    if plan.has_plan(project_id):
        return {"skipped": True}
    legacy = {w["text"]: w for w in plan.items(project_id) if w["status"] != "retired" and not w.get("plan_origin")}
    from project_watch.studio.structured_facts import own_names

    project = pipeline.store.get_project(project_id) or {}
    names = own_names(project.get("legal_name") or project.get("name") or "", project)
    text = bootstrap_input(pipeline, project_id)
    if text is None or not getattr(llm, "ready", False):
        return {"skipped": True}
    data = llm.json(BOOTSTRAP, text, max_tokens=4000)
    if not isinstance(data, dict):
        raise ValueError("bootstrap output is not an object")
    if not isinstance(data.get("items"), list) or not data["items"]:
        # No plan came back: leave templates, summary and every earlier question exactly as they were.
        return {"skipped": True, "reason": "no plan items"}
    keys = [k for k in data.get("templates") or [] if k in {t["key"] for t in catalog()}][:2]
    if keys:
        plan.apply_templates(project_id, keys, actor="system")
    sections = {}
    for key, value in (data.get("sections") or {}).items():
        if key in SECTION_OF and isinstance(value, dict):
            key = SECTION_OF[key]
            current = (plan.meta(project_id).get("sections") or {}).get(key) or {}
            cover = value.get("coverage") if value.get("coverage") in ("covered", "partial", "internal", "none") else "none"
            sections[key] = {"baseline": current.get("baseline", ""), "known": str(value.get("known") or "")[:600], "coverage": cover}
    plan.set_meta(project_id, {"understanding": str(data.get("understanding") or "")[:1200],
                               "in_scope": data.get("in_scope") or [], "out_of_scope": data.get("out_of_scope") or [],
                               "sections": sections, "review": "pending"}, actor="system", reason="系统根据首轮资料生成首版方案")
    added = dropped = kept = 0
    for row in (data.get("items") or [])[:12]:
        if not isinstance(row, dict) or row.get("section") not in SECTION_OF:
            dropped += 1
            continue
        text = str(row.get("text") or "").strip()
        view, params = _view(row)
        # An earlier question the plan kept (same or near-same wording): same item, history intact.
        match = next((t for t in legacy if t == text or _same_question(t, text, names)), None)
        old = legacy.pop(match) if match else None
        if old is not None:
            plan.update(old["id"], {"axis": row["section"], "reason": str(row.get("reason") or "")[:300],
                                    "objects": [str(o) for o in row.get("objects") or []][:6],
                                    "view": view, "view_params": params},
                        actor="system", reason="并入系统首版方案")
            with plan.monitor._db() as db, db:
                db.execute("UPDATE studio_watchpoints SET plan_origin='bootstrap' WHERE id=?", (old["id"],))
            kept += 1
            continue
        try:
            hop = str(min(int(row.get("hop") or 0), MAX_HOP))
        except (TypeError, ValueError):
            hop = "0"
        try:
            plan.add(project_id, {
                "text": text[:60], "label": str(row.get("label") or "")[:12],
                "axis": row["section"], "objects": [str(o) for o in row.get("objects") or []][:6],
                "queries": [str(q) for q in row.get("queries") or []][:3], "reason": str(row.get("reason") or "")[:300],
                "view": view, "view_params": params,
                "tier": row.get("tier") if row.get("tier") in ("core", "background") else "background",
                "cadence": row.get("cadence") if row.get("cadence") in ("high", "medium", "low") else "medium",
                "trigger_rule": str(row.get("trigger") or "")[:300], "exit_rule": str(row.get("exit") or "")[:300], "hop": hop,
            }, actor="system", origin="bootstrap", reason="系统首版方案")
            added += 1
        except (ValueError, PermissionError):
            dropped += 1
    proposed = 0
    for old in legacy.values():  # earlier questions the first plan did not keep: closed, history kept
        if plan._owned_by_people(old):  # a person's question is only proposed for closing
            plan.propose(old["id"], {"status": "retired"}, reason="系统首版方案未保留这个问题，建议关闭")
            proposed += 1
        else:
            plan.update(old["id"], {"status": "retired"}, actor="system", reason="并入系统首版方案（未保留）")
    plan.record_revision(project_id, {"added": added, "dropped": dropped}, "bootstrap")
    return {"added": added, "kept": kept, "retired": len(legacy) - proposed, "proposed": proposed, "dropped": dropped, "templates": keys}
