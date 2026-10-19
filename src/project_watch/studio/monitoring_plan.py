"""监控方案: each project's living set of questions, built on the existing watch points / question ledger.

A plan item is a watch point with the plan fields: axis, objects, reason, current state, sources,
cadence, trigger, exit rule, tier and status. Every change — by the system, a person or an Agent —
is written to ``studio_plan_changes`` with who, why and before/after, so it can be shown and reverted.
Items the system adds start as ``trial`` (low cadence, revertible); promoting, retiring human items or
widening paid collection stays with the owner. Retired items keep their judgement history.

Core: storage and rules only; no model, search or network calls.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from datetime import UTC, datetime, timedelta

from project_watch.studio.exploration import Explorations
from project_watch.studio.plan_templates import MODULES, catalog, combine
from project_watch.studio.store import now

# The fixed framework every plan grows from: four rings around the company, nine dimensions, ordered from
# the company outwards. Dimensions are about *whom* we watch; an item goes where its object is, not where the
# news came from (a competitor's paper is 同路线竞品, not 学术). A project may override a dimension's baseline
# (industry method) and record what is already known there.
RINGS = (("company", "公司本体"), ("competition", "竞争"), ("chain", "产业链"), ("environment", "环境"))
FRAMEWORK = (
    ("asset", "资产与产品", "每个核心资产或产品单独看：研发与临床节点、注册与获批、量产与交付、在售产品的销售与口碑。"),
    ("operations", "经营、团队与资金", "公司整体经营：收入与订单规模、产能与效率、团队与关键人才、融资与资金跑道、股权与治理、工商变更、对外承诺是否兑现。"),
    ("peers", "同路线竞品", "同靶点、同机制、同类技术或同类产品的对手：开发阶段与读出、定点与产能、价格、融资与合作。"),
    ("alternatives", "同需求替代", "为同一患者或客户解决同一问题的其他方式：现有标准方案、不同机制或产品形态的新方案、需求本身的扩张或收缩。"),
    ("customer", "客户与渠道", "下游谁在买：主要客户与订单、采用与复购、客户集中度与份额、渠道与终端需求。"),
    ("partners", "供应、合作与资本方", "上游与合作对象：关键原料与供应商、代工与外包、合作方与授权方、投资方与潜在买方的动向。"),
    ("market", "行业与市场", "所在赛道：市场规模与景气度、价格趋势、行业融资热度与可比交易、资本市场窗口。"),
    ("science", "学术与技术证据", "整条技术路线的证据：论文与复现、反例与失败、专利、学术会议、团队的学术产出。"),
    ("rules", "政策与规则", "监管审批与准入、行业标准与认证、产业政策与补贴、贸易与合规要求。"),
)
RING_OF = {"asset": "company", "operations": "company", "peers": "competition", "alternatives": "competition",
           "customer": "chain", "partners": "chain", "market": "environment", "science": "environment", "rules": "environment"}
# The earlier eight question-type sections. Still accepted (older data, imports, model output) and placed into a
# dimension by their wording; stored items are migrated once by ``migrate_framework``.
LEGACY = {"self": ("公司自身进展", "asset"), "demand": ("需求与应用", "alternatives"), "competition": ("竞争与替代", "peers"),
          "technology": ("技术路线与证据", "science"), "commercial": ("商业化与经营", "operations"),
          "supply": ("供应链与产业链", "partners"), "capital": ("资本与股权", "operations")}
_PLACE = {
    "self": ((r"团队|人才|创始|管理层|融资|资金|股东|股权|工商|估值|招聘|治理|承诺|产能|产线|投产|基地|爬坡|扩产", "operations"),),
    "technology": ((r"管线|适应症|产品", "asset"),),
    "commercial": ((r"合作|授权|许可|BD|伙伴|分成", "partners"), (r"客户|采购|复购|订单|采用|渠道", "customer")),
    "competition": ((r"替代|标准治疗|治疗标准|其他治疗|其他路线|不同路线", "alternatives"),),
    "customer": ((r"替代|标准治疗|治疗标准|治疗选择", "alternatives"),),
    "demand": ((r"客户|采购|订单|复购", "customer"),),
    "capital": ((r"合作|授权|分成|收益归属", "partners"), (r"同行|赛道|行业|可比", "market"),
                (r"投资方|投资人|并购方|买方", "partners")),
}
AXES = (*(key for key, _l, _b in FRAMEWORK), *LEGACY)
AXIS_LABEL = {key: label for key, label, _b in FRAMEWORK} | {k: v[0] for k, v in LEGACY.items()}
SECTION_OF = {key: key for key, _l, _b in FRAMEWORK} | {k: v[1] for k, v in LEGACY.items()}
OWN = ("asset", "operations", "self", "capital", "commercial")  # dimensions about the company itself
FRAMEWORK_VERSION = "rings-1"


def place(axis: str, text: str = "") -> str:
    """The dimension for an axis key; an older key is placed by the item's wording (融资 → 经营, 替代 → 同需求替代)."""
    for pattern, key in _PLACE.get(axis, ()):
        if re.search(pattern, text or "", re.I):
            return key
    return SECTION_OF.get(axis, "asset")


COVERAGE = ("covered", "partial", "internal", "none")
COVERAGE_LABEL = {"covered": "已覆盖", "partial": "部分覆盖", "internal": "需内部资料", "none": "暂未覆盖"}
CADENCES = ("high", "medium", "low", "event")
CADENCE_LABEL = {"high": "每次更新", "medium": "每周", "low": "每月", "event": "事件驱动"}
CADENCE_DAYS = {"high": 0, "medium": 6, "low": 27}
TIERS = ("core", "background", "topic")
TIER_LABEL = {"core": "核心", "background": "背景", "topic": "临时专题"}
STATUSES = ("active", "trial", "reduced", "retired")
STATUS_LABEL = {"active": "运行", "trial": "试运行", "reduced": "已降频", "retired": "已关闭"}
ACTORS = ("system", "human", "agent")
ORIGINS = ("system", "method", "internal", "human", "draft", "bootstrap")
ORIGIN_LABEL = {"system": "系统", "method": "行业方法", "internal": "内部资料", "human": "人", "draft": "首版方案",
                "bootstrap": "系统首版"}
# Items whose news is about others go to the matching collection category, not "company".
AXIS_CATEGORY = {"peers": "competitor", "alternatives": "industry", "customer": "industry", "partners": "industry",
                 "market": "industry", "science": "industry", "rules": "policy",
                 "competition": "competitor", "demand": "industry", "technology": "industry", "supply": "industry"}
META_KEY = "monitoring_plan_meta:{}"
# 常规采集: the standing collection every round runs regardless of focus points, mapped to the framework.
# (channel key in the round report, label, section, event category for the 30-day count, needs)
CHANNELS = (
    ("trial", "临床试验登记（ClinicalTrials.gov）", "asset", "trial", ""),
    ("cn_trial", "国内临床登记与审评", "asset", None, "search"),
    ("license", "资质与许可", "asset", None, "search"),
    ("company", "公司新闻", "operations", "company", ""),
    ("site", "公司官网页面", "operations", None, ""),
    ("registry", "工商变更", "operations", None, "registry"),
    ("filing", "上市公司公告（巨潮资讯）", "operations", None, ""),
    ("commercial", "专业数据源融资记录（烯牛 / 医药魔方 / Wind）", "operations", None, ""),
    ("competitor", "竞品动态", "peers", "competitor", ""),
    ("tender", "招投标（客户采购）", "customer", None, "search"),
    ("facts", "商业关系与政策条件（资料与报道中提取）", "partners", None, ""),
    ("industry", "赛道与市场动态", "market", "industry", ""),
    ("paper", "论文（OpenAlex）", "science", "paper", ""),
    ("patent", "专利", "science", "patent", "search"),
    ("policy", "政策监管", "rules", "policy", ""),
)
MAX_ITEMS = 16  # active + trial + reduced; retired items do not count
MAX_HOP = 2  # 0 = the company itself, 1 = directly related (competitor asset, disease, customer, material), 2 = second order
MAX_PER_SECTION = 4
STABLE_AFTER = 3  # consecutive revisions with no additions or proposals
EDITABLE = ("text", "label", "queries", "criteria", "axis", "objects", "reason", "current_state", "sources",
            "cadence", "trigger_rule", "exit_rule", "tier", "status", "alert", "view", "view_params", "hop", "parent_id")


def _clean(fields: dict) -> dict:
    out = {}
    for key, value in fields.items():
        if key not in EDITABLE or value is None:
            continue
        if key == "queries":
            value = [str(q).strip()[:80] for q in value if str(q).strip()][:4]
        elif key == "objects":
            value = [str(o).strip()[:60] for o in value if str(o).strip()][:8]
        elif key == "alert":
            value = bool(value)
        elif key == "view_params":
            if not isinstance(value, dict) or len(json.dumps(value, ensure_ascii=False)) > 4000:
                raise ValueError("模块参数格式不正确")
        else:
            value = str(value).strip()
        if key == "axis":
            if value not in AXES:
                raise ValueError("unknown axis")
            value = place(value, str(fields.get("text") or ""))
        if key == "cadence" and value not in CADENCES:
            raise ValueError("unknown cadence")
        if key == "tier" and value not in TIERS:
            raise ValueError("unknown tier")
        if key == "status" and value not in STATUSES:
            raise ValueError("unknown status")
        if key == "hop" and value not in ("0", "1", "2"):
            raise ValueError(f"关联链最多延伸 {MAX_HOP} 层")
        if key == "view" and value not in MODULES:
            raise ValueError("unknown display module")
        if key == "text" and not 2 <= len(value) <= 200:
            raise ValueError("问题应为 2–200 字")
        out[key] = value[:600] if isinstance(value, str) else value
    return out


def _row(watch: dict) -> dict:
    return {k: watch.get(k) for k in EDITABLE}


def _encode(fields: dict) -> dict:
    return {k: (json.dumps(v, ensure_ascii=False) if k in ("queries", "objects", "view_params") else (1 if v else 0) if k == "alert" else v)
            for k, v in fields.items()}



def _norm(title: str) -> str:
    return re.sub(r"[\s\W_]+", "", title.casefold())


def _same_story(a: str, b: str) -> bool:
    """Only identical normalized headlines are safe to collapse without reading their meaning."""
    x, y = _norm(a), _norm(b)
    return bool(x and y) and x == y


_SUFFIX = re.compile(r"(股份有限公司|有限公司|股份|集团|科技|技术|智能|生物|医药|公司)$")


def peer_names(entry: str) -> list[str]:
    """Names to look for in headlines: "电连技术|电连" or "英矽智能（ISM6331）". Company suffixes are also tried
    without the suffix (电连技术 → 电连). Chinese names need 2+ characters, Latin names 3+ (bare "TE" is too ambiguous)."""
    raw = [p for part in re.split(r"[|/、，,]", entry) for p in re.split(r"[（()）]", part)]
    out: list[str] = []
    for name in raw:
        name = name.strip()
        for variant in (name, _SUFFIX.sub("", name)):
            latin = re.fullmatch(r"[\x00-\x7f]+", variant) is not None
            if variant and len(variant) >= (3 if latin else 2) and variant not in out:
                out.append(variant)
    return out

class MonitoringPlan:
    def __init__(self, monitor) -> None:
        self.monitor = monitor
        self.explorations = Explorations(monitor)

    # ------------------------------------------------------------------ reads
    def items(self, project_id: str) -> list[dict]:
        return self.monitor.watchpoints(project_id)

    def meta(self, project_id: str) -> dict:
        raw = self.monitor.store.get_state(META_KEY.format(project_id))
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return {}

    def set_meta(self, project_id: str, meta: dict, *, actor: str, reason: str) -> dict:
        """Plan-level understanding, method, scope and per-section baseline/known facts. Logged like item changes."""
        current = self.meta(project_id)
        clean: dict = {}
        for key in ("understanding", "method"):
            if key in meta:
                clean[key] = str(meta[key] or "").strip()[:1200]
        if "review" in meta:
            if meta["review"] not in ("pending", "done"):
                raise ValueError("unknown review state")
            clean["review"] = meta["review"]
        if "templates" in meta:
            clean["templates"] = [t for t in meta["templates"] or [] if t in {c["key"] for c in catalog()}][:4]
        for key in ("in_scope", "out_of_scope"):
            if key in meta:
                clean[key] = [str(x).strip()[:200] for x in meta[key] or [] if str(x).strip()][:12]
        if "sections" in meta:
            sections = dict(current.get("sections") or {})
            for key, value in (meta["sections"] or {}).items():
                if key not in SECTION_OF or not isinstance(value, dict):
                    raise ValueError("unknown plan section")
                key = SECTION_OF[key]
                cover = value.get("coverage", sections.get(key, {}).get("coverage", "none"))
                if cover not in COVERAGE:
                    raise ValueError("unknown coverage")
                sections[key] = {"baseline": str(value.get("baseline", "") or "").strip()[:800],
                                 "known": str(value.get("known", "") or "").strip()[:1200], "coverage": cover}
            clean["sections"] = sections
        merged = {**current, **clean, "updated_at": now()}
        self.monitor.store.set_state(META_KEY.format(project_id), json.dumps(merged, ensure_ascii=False))
        with self.monitor._db() as db, db:
            self._log(db, project_id, "", "meta", actor, reason, {k: current.get(k) for k in clean}, clean)
        return merged

    def view(self, project_id: str, *, changes: int = 30) -> dict:
        items = self.items(project_id)
        with self.monitor._db() as db:
            rows = db.execute("SELECT * FROM studio_plan_changes WHERE project_id=? ORDER BY created_at DESC LIMIT ?",
                              (project_id, changes)).fetchall()
        names = {w["id"]: w["label"] or w["text"][:24] for w in items} | {"": "方案概要与分类基础"}
        log = [{**dict(r), "before": json.loads(r["before"]), "after": json.loads(r["after"]),
                "evidence": json.loads(r["evidence"] or "[]"), "item": names.get(r["watch_id"], "")} for r in rows]
        with self.monitor._db() as db:
            pending = [dict(r) for r in db.execute("SELECT * FROM studio_plan_changes WHERE project_id=? AND state='proposed' "
                                                    "ORDER BY created_at DESC", (project_id,))]
        proposals = [{**r, "before": json.loads(r["before"]), "after": json.loads(r["after"]),
                      "evidence": json.loads(r["evidence"] or "[]"), "item": names.get(r["watch_id"], "")} for r in pending]
        live = [w for w in items if w["status"] != "retired"]
        week_ago = (datetime.now(UTC) - timedelta(days=7)).isoformat()
        meta = self.meta(project_id)
        own = meta.get("sections") or {}
        framework = [{"key": key, "label": label, "default_baseline": base,
                      "baseline": (own.get(key) or {}).get("baseline") or base,
                      "known": (own.get(key) or {}).get("known", ""),
                      "coverage": (own.get(key) or {}).get("coverage") or ("partial" if any(
                          SECTION_OF.get(w["axis"], "asset") == key for w in live) else "none"),
                      "ring": RING_OF[key]}
                     for key, label, base in FRAMEWORK]
        for w in items:
            w["section"] = place(w.get("axis") or "asset", w.get("text") or "")
        return {"items": items, "changes": [c for c in log if c.get("state", "applied") != "proposed"],
                "proposals": proposals, "framework": framework,
                "explorations": self.explorations.view(project_id),
                "rings": [{"key": k, "label": label} for k, label in RINGS],
                "meta": {k: meta.get(k) for k in ("understanding", "method", "in_scope", "out_of_scope", "updated_at", "templates", "review")},
                "state": self.state(project_id),
                "limits": {"max_hop": MAX_HOP, "per_section": MAX_PER_SECTION},
                "modules": {w["id"]: self.module(project_id, w) for w in live},
                "coverage": self.coverage(project_id),
                "latest": (latest := {w["id"]: self.latest(project_id, w) for w in live}),
                "week": {"new_items": sum(1 for w in live if (w.get("created_at") or "") >= week_ago),
                         "fresh": sum(1 for v in latest.values() if v["fresh"])},
                "health": self.health(project_id, live),
                "module_labels": {k: v.split("：")[0] for k, v in MODULES.items()},
                "templates": catalog(),
                "summary": {t: sum(w["tier"] == t for w in live) for t in TIERS},
                "labels": {"axis": AXIS_LABEL, "cadence": CADENCE_LABEL, "tier": TIER_LABEL,
                           "status": STATUS_LABEL, "origin": ORIGIN_LABEL, "coverage": COVERAGE_LABEL},
                "max_items": MAX_ITEMS}

    # ------------------------------------------------------------------ writes
    def _log(self, db, project_id, watch_id, action, actor, reason, before, after, *, state="applied", evidence=()):
        if actor not in ACTORS:
            raise ValueError("unknown actor")
        change_id = uuid.uuid4().hex
        db.execute("INSERT INTO studio_plan_changes (id, project_id, watch_id, action, actor, reason, before, after, created_at, state, evidence) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (change_id, project_id, watch_id, action, actor, reason[:300],
                    json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False), now(), state,
                    json.dumps(list(evidence)[:6], ensure_ascii=False)))
        return change_id

    # ------------------------------------------------------------------ proposals (owner decides)
    def propose(self, watch_id: str, fields: dict, *, reason: str, evidence=(),
                expected_lifecycle_version: str | None = None) -> str:
        """A system suggestion that changes a person's (or the reviewed draft's) item: stored, not applied."""
        data = _clean(fields)
        with self.monitor._db() as db:
            row = db.execute("SELECT project_id FROM studio_watchpoints WHERE id=?", (watch_id,)).fetchone()
        if not row:
            raise KeyError(watch_id)
        current = self._get(row[0], watch_id)
        changed = {k: v for k, v in data.items() if current.get(k) != v}
        if not changed:
            return ""
        action = ("retire" if changed.get("status") == "retired" else "reduce" if changed.get("status") == "reduced"
                  else "restore" if changed.get("status") == "active" else "modify")
        with self.monitor._db() as db, db:
            db.execute('BEGIN IMMEDIATE')
            if expected_lifecycle_version is not None:
                state = db.execute('SELECT status,lifecycle_version FROM studio_watchpoints WHERE id=?', (watch_id,)).fetchone()
                if not state or state['status'] == 'retired' or str(state['lifecycle_version']) != expected_lifecycle_version:
                    raise ValueError('条目状态已变化，旧建议不再适用')
            dup = db.execute("SELECT id FROM studio_plan_changes WHERE watch_id=? AND state='proposed' AND after=?",
                             (watch_id, json.dumps(changed, ensure_ascii=False))).fetchone()
            if dup:
                return dup[0]
            return self._log(db, row[0], watch_id, action, "system", reason,
                             {k: current.get(k) for k in changed}, changed, state="proposed", evidence=evidence)

    def decide(self, change_id: str, accept: bool) -> dict:
        with self.monitor._db() as db:
            row = db.execute("SELECT * FROM studio_plan_changes WHERE id=?", (change_id,)).fetchone()
        if not row or row["state"] != "proposed":
            raise KeyError(change_id)
        result = {}
        if accept:
            result = self.update(row["watch_id"], json.loads(row["after"]), actor="human", reason=f"接受系统建议：{row['reason']}")
        with self.monitor._db() as db, db:
            db.execute("UPDATE studio_plan_changes SET state=? WHERE id=?", ("accepted" if accept else "rejected", change_id))
        return result

    def _owned_by_people(self, watch: dict) -> bool:
        return bool(watch.get("reviewed_at")) or watch.get("plan_origin") in ("human", "draft", "internal") or watch.get("origin") == "human"

    def _validate_write(self, db, project_id: str, candidate: dict, *, current=None, actor: str) -> dict[str, str]:
        """Validate the proposed graph under the same write lock as its mutation.

        Existing over-capacity legacy plans can still be edited/closed; only a
        new allocation must fit. Human-authored section sizes remain explicit
        choices, but restoring an automatic item cannot bypass its section cap.
        """
        if actor not in ACTORS:
            raise ValueError("unknown actor")
        nodes = {r['id']: dict(r) for r in db.execute(
            'SELECT * FROM studio_watchpoints WHERE project_id=?', (project_id,))}
        wid = candidate['id']
        others = [w for k, w in nodes.items() if k != wid and w['status'] != 'retired']
        live = candidate.get('status', 'active') != 'retired'
        allocating = current is None or current['status'] == 'retired'
        if live and allocating and len(others) >= MAX_ITEMS:
            raise ValueError(f"每个项目最多 {MAX_ITEMS} 条运行中的方案条目，请先关闭或合并")
        section = SECTION_OF.get(candidate.get('axis', 'asset'), 'asset')
        moving = current is None or section != SECTION_OF.get(current['axis'], 'asset')
        automatic = actor == 'system' or candidate.get('plan_origin') in ('system', 'bootstrap', 'internal')
        if (live and (allocating or moving) and automatic
                and sum(SECTION_OF.get(w['axis'], 'asset') == section for w in others) >= MAX_PER_SECTION):
            raise ValueError(f"「{AXIS_LABEL.get(section, section)}」已有 {MAX_PER_SECTION} 个关注点")
        if (live and (allocating or current.get('text') != candidate['text'])
                and any(_norm(w['text']) == _norm(candidate['text']) for w in others)):
            raise ValueError('已有相同关注点，请更新原条目')
        parent = candidate.get('parent_id') or ''
        if current and current.get('parent_id') and not parent:
            raise ValueError('不能移除关联来源来重置层级')
        if (parent and (not current or parent != current.get('parent_id') or (live and allocating))
                and (parent not in nodes or nodes[parent]['status'] == 'retired')):
            raise ValueError('延伸来源不存在或已关闭')
        if current and not parent and candidate.get('hop', '0') != current['hop']:
            raise ValueError('层级由关联来源计算，不能单独修改')
        nodes[wid] = candidate
        depths: dict[str, str] = {}
        def depth(key, visiting):
            if key in visiting:
                raise ValueError('关联来源不能形成循环')
            if key not in nodes:
                raise ValueError('延伸来源不属于当前项目')
            node = nodes[key]
            above = node.get('parent_id') or ''
            value = depth(above, visiting | {key}) + 1 if above else int(node.get('hop') or 0)
            if value > MAX_HOP:
                raise ValueError(f'关联链最多延伸 {MAX_HOP} 层')
            depths[key] = str(value)
            return value
        depth(wid, set())
        # A legal move for a parent must also be legal for every descendant.
        affected = {wid}
        while True:
            children = {key for key, node in nodes.items() if node.get('parent_id') in affected}
            if children <= affected:
                break
            affected |= children
        for key in affected:
            depth(key, set())
        return {key: depths[key] for key in affected}

    def add(self, project_id: str, fields: dict, *, actor: str, origin: str, reason: str = "",
            visibility: str = "internal", evidence=(), expected_parent_version: str | None = None,
            expected_parent_generation: int | None = None) -> dict:
        if origin not in ORIGINS or visibility not in ('public', 'internal'):
            raise ValueError("unknown origin")
        data = {'axis': 'asset', 'hop': '0', 'parent_id': '', 'status': 'active', **_clean(fields)}
        if "text" not in data:
            raise ValueError("缺少问题")
        if actor == "system" and origin != "bootstrap":  # later system additions start as revertible, low-cadence trials
            data["status"], data["cadence"] = "trial", data.get("cadence") if data.get("cadence") == "event" else "low"
        wid = uuid.uuid4().hex
        with self.monitor._db() as db, db:
            db.execute('BEGIN IMMEDIATE')
            if expected_parent_version is not None:
                parent = db.execute('SELECT lifecycle_version FROM studio_watchpoints WHERE id=? AND project_id=?',
                                    (data.get('parent_id'), project_id)).fetchone()
                if not parent or str(parent[0]) != expected_parent_version:
                    raise ValueError('关联来源已变化，旧延伸不再适用')
            if expected_parent_generation is not None:
                parent_run = db.execute('SELECT generation,state FROM studio_explorations WHERE watch_id=?', (data.get('parent_id'),)).fetchone()
                if not parent_run or parent_run['generation'] != expected_parent_generation or parent_run['state'] != 'active':
                    raise ValueError('关联来源的探索已变化，旧延伸不再适用')
            depths = self._validate_write(db, project_id, {**data, 'id': wid, 'plan_origin': origin}, actor=actor)
            data['hop'] = depths[wid]
            db.execute('INSERT INTO studio_watchpoints (id,project_id,text,label,queries,criteria,alert,created_at,origin,visibility) '
                       'VALUES (?,?,?,?,?,?,?,?,?,?)',
                       (wid, project_id, data['text'], data.get('label') or data['text'][:24],
                        json.dumps(data.get('queries') or [], ensure_ascii=False), data.get('criteria') or '',
                        int(data.get('alert', True)), now(), 'human' if actor == 'human' else 'ai', visibility))
            rest = _encode({k: v for k, v in data.items() if k not in ('text', 'label', 'queries', 'criteria', 'alert')})
            db.execute(f"UPDATE studio_watchpoints SET {', '.join(f'{k}=?' for k in rest)}, plan_origin=?, updated_at=? WHERE id=?",
                       (*rest.values(), origin, now(), wid))
            self._log(db, project_id, wid, 'add', actor, reason, {}, data, evidence=evidence)
        return self._get(project_id, wid)

    def update(self, watch_id: str, fields: dict, *, actor: str, reason: str = "", evidence=(),
               expected_lifecycle_version: str | None = None, expected_exploration_generation: int | None = None,
               exploration_answer: bool = False) -> dict:
        data = _clean(fields)
        with self.monitor._db() as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM studio_watchpoints WHERE id=?', (watch_id,)).fetchone()
            if not row:
                raise KeyError(watch_id)
            current = dict(row)
            if expected_lifecycle_version is not None and str(current['lifecycle_version']) != expected_lifecycle_version:
                raise ValueError('条目状态已变化，旧修订不再适用')
            exploration = None
            if expected_exploration_generation is not None:
                exploration = db.execute('SELECT * FROM studio_explorations WHERE watch_id=?', (watch_id,)).fetchone()
                if (not exploration or exploration['generation'] != expected_exploration_generation
                        or exploration['version'] != str(current['lifecycle_version']) or exploration['state'] != 'active'):
                    raise ValueError('探索已变化，旧答案不再适用')
            if exploration_answer and (exploration is None or current['tier'] != 'topic' or not current['exit_rule'] or not evidence):
                raise ValueError('结束探索需要当前代次、退出条件和本题依据')
            if actor == 'system' and current['status'] == 'retired' and data.get('status') != 'active':
                raise ValueError('已关闭的条目不再接受自动修订')
            for key in ('queries', 'objects', 'view_params'):
                current[key] = json.loads(current[key])
            current['alert'] = bool(current['alert'])
            project_id = current['project_id']
            self._update_locked(db, project_id, current, data, actor=actor, reason=reason, evidence=evidence)
            if exploration_answer:
                db.execute("UPDATE studio_explorations SET state='stopped',reason='answered',updated_at=? WHERE watch_id=?",
                           (now(), watch_id))
                self.explorations._history(db, {**dict(exploration), 'state': 'stopped', 'reason': 'answered'},
                    [json.dumps(e, ensure_ascii=False, sort_keys=True) for e in evidence])
        return self._get(project_id, watch_id)

    def _update_locked(self, db, project_id, current, data, *, actor, reason, evidence):
        watch_id = current['id']
        slower = ("cadence" in data and
                  (data["cadence"] == "event" or CADENCE_DAYS.get(data["cadence"], 0) > CADENCE_DAYS.get(current["cadence"], 0)))
        if actor == "system" and self._owned_by_people(current) and (data.get("status") in ("retired", "reduced") or slower):
            raise PermissionError("系统不能关闭或降频人添加或审阅过的条目，只能提出建议")
        if actor == "system" and data.get("tier") == "core" and current["tier"] != "core":
            raise PermissionError("升为核心需要人确认")
        depths = self._validate_write(db, project_id, {**current, **data}, current=current, actor=actor)
        data = {**data, 'hop': depths[watch_id]}
        changed = {k: v for k, v in data.items() if current.get(k) != v}
        # Keep provenance intact; a person's confirmation is a separate, durable decision.
        if actor == "human" and data and not current.get("reviewed_at"):
            changed["reviewed_at"] = now()
        if not changed:
            return current
        before = {k: current.get(k) for k in changed}
        action = ("retire" if changed.get("status") == "retired" else "reduce" if changed.get("status") == "reduced"
                  else "restore" if changed.get("status") == "active" and before.get("status") in ("retired", "reduced", "trial")
                  else "modify")
        enc = _encode(changed)
        if set(changed) & {'status', 'text', 'queries', 'objects', 'tier', 'cadence', 'parent_id', 'hop', 'exit_rule', 'trigger_rule'}:
            enc['lifecycle_version'] = str(int(current.get('lifecycle_version') or 0) + 1)
        if "queries" in changed:
            enc.update(completed_queries="[]", last_run_at="", last_attempt_at="")
        db.execute(f"UPDATE studio_watchpoints SET {', '.join(f'{k}=?' for k in enc)}, updated_at=? WHERE id=?",
                   (*enc.values(), now(), watch_id))
        for wid, hop in depths.items():
            if wid != watch_id:
                db.execute('UPDATE studio_watchpoints SET hop=? WHERE id=?', (hop, wid))
        self._log(db, project_id, watch_id, action, actor, reason, before, changed, evidence=evidence)

    def revert(self, change_id: str, *, actor: str = "human") -> dict:
        with self.monitor._db() as db:
            row = db.execute("SELECT * FROM studio_plan_changes WHERE id=?", (change_id,)).fetchone()
        if not row:
            raise KeyError(change_id)
        if row["reverted_at"]:
            raise ValueError("这项变更已撤销")
        if row["action"] == "regroup":
            raise ValueError("框架调整不能撤销；如果归类不对，请在关注点里选择“移到”")
        if row["action"] == "meta":
            before = json.loads(row["before"])
            after = json.loads(row["after"])
            # Exact restoration, not set_meta's incremental section merge. Preserve later unrelated
            # edits and refuse to overwrite a subsequent change to the same field.
            with self.monitor._db() as db, db:
                db.execute("BEGIN IMMEDIATE")
                key = META_KEY.format(row["project_id"])
                saved = db.execute("SELECT value FROM studio_state WHERE key=?", (key,)).fetchone()
                result = json.loads(saved[0]) if saved else {}
                if any(result.get(k) != v for k, v in after.items()):
                    raise ValueError("这些内容已有后续修改，请手动调整，避免覆盖新内容")
                for k, v in before.items():
                    if v is None:
                        result.pop(k, None)
                    else:
                        result[k] = v
                result["updated_at"] = now()
                db.execute("INSERT INTO studio_state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           (key, json.dumps(result, ensure_ascii=False)))
                self._log(db, row["project_id"], "", "meta", actor, "撤销方案概要变更", after, before)
                db.execute("UPDATE studio_plan_changes SET reverted_at=? WHERE id=?", (now(), change_id))
            return result
        elif row["action"] == "add":
            result = self.update(row["watch_id"], {"status": "retired"}, actor=actor, reason="撤销新增")
        else:
            result = self.update(row["watch_id"], json.loads(row["before"]), actor=actor, reason="撤销变更")
        with self.monitor._db() as db, db:
            db.execute("UPDATE studio_plan_changes SET reverted_at=? WHERE id=?", (now(), change_id))
        return result

    def import_items(self, project_id: str, items: list[dict], *, origin: str = "draft", reason: str) -> list[dict]:
        """Load a reviewed plan (e.g. the first draft). Existing questions with the same text are updated, not duplicated."""
        existing = {w["text"]: w for w in self.items(project_id)}
        out = []
        for item in items:
            if item.get("text") in existing:
                out.append(self.update(existing[item["text"]]["id"], item, actor="human", reason=reason))
            else:
                out.append(self.add(project_id, item, actor="human", origin=origin, reason=reason))
        return out

    def migrate_framework(self) -> int:
        """Once per store: move items from the earlier eight question-type sections into the nine dimensions,
        and plan summaries' per-section notes with them. Plan items' moves are logged as 归类调整."""
        store = self.monitor.store
        if store.get_state("monitoring_plan_framework") == FRAMEWORK_VERSION:
            return 0
        moved = 0
        reason = "监控框架改为四圈九类，按监控对象重新归类"
        with self.monitor._db() as db, db:
            marks = ",".join("?" * len(LEGACY))
            rows = db.execute(f"SELECT id, project_id, axis, text, plan_origin FROM studio_watchpoints WHERE axis IN ({marks})",
                              tuple(LEGACY)).fetchall()
            for row in rows:
                new = place(row["axis"], row["text"])
                db.execute("UPDATE studio_watchpoints SET axis=? WHERE id=?", (new, row["id"]))
                if row["plan_origin"]:
                    self._log(db, row["project_id"], row["id"], "regroup", "system", reason, {"axis": row["axis"]}, {"axis": new})
                    moved += 1
            metas = db.execute("SELECT key, value FROM studio_state WHERE key LIKE 'monitoring_plan_meta:%'").fetchall()
        for key, raw in metas:
            project_id = key.split(":", 1)[1]
            try:
                meta = json.loads(raw)
            except ValueError:
                continue
            old = meta.get("sections") or {}
            if not any(k in LEGACY for k in old):
                continue
            baselines = combine(meta.get("templates") or [])["sections"]
            sections: dict[str, dict] = {}
            for k, value in old.items():
                target = SECTION_OF.get(k)
                if not target or not isinstance(value, dict):
                    continue
                cur = sections.setdefault(target, {"baseline": baselines.get(target, ""), "known": "", "coverage": "none"})
                known = str(value.get("known") or "")
                if known and known not in cur["known"]:
                    cur["known"] = f"{cur['known']}；{known}" if cur["known"] else known
                rank = {"none": 0, "internal": 1, "partial": 2, "covered": 3}
                if rank.get(value.get("coverage"), 0) > rank.get(cur["coverage"], 0):
                    cur["coverage"] = value["coverage"]
            meta = {**meta, "sections": sections, "updated_at": now()}
            store.set_state(key, json.dumps(meta, ensure_ascii=False))
            with self.monitor._db() as db, db:
                self._log(db, project_id, "", "regroup", "system", reason, {"sections": sorted(old)}, {"sections": sorted(sections)})
        store.set_state("monitoring_plan_framework", FRAMEWORK_VERSION)
        return moved

    def _get(self, project_id: str, watch_id: str) -> dict:
        return next(w for w in self.items(project_id) if w["id"] == watch_id)

    # ------------------------------------------------------------------ execution
    def due(self, project_id: str, *, at: datetime | None = None, cycle: str = '') -> list[dict]:
        """Items to search this round: running or trial, by cadence; core first. Event-driven items wait for a trigger."""
        moment = at or datetime.now(UTC)
        order = {"core": 0, "background": 1, "topic": 2}
        out = []
        exploration = self.explorations.view(project_id)
        for w in self.items(project_id):
            if w["status"] == "retired" or not w["queries"]:
                continue
            if self.explorations.managed(w) and w['id'] in exploration:
                if not self.explorations.runnable(w, cycle):
                    continue
                if w['cadence'] == 'event' or self.explorations.immediate(w, cycle):
                    out.append(w)
                    continue
            if w['cadence'] == 'event':
                continue
            cadence = "low" if w["status"] in ("trial", "reduced") else w["cadence"]
            last = w.get("last_run_at") or ""
            if last and cadence != "high" and last > (moment - timedelta(days=CADENCE_DAYS[cadence])).isoformat():
                continue
            out.append(w)
        # Longest wait first across tiers; priority breaks ties, but cannot starve background work.
        return sorted(out, key=lambda w: (w.get("last_attempt_at") or w.get("last_run_at") or "",
                                          order.get(w["tier"], 3), w["created_at"]))

    def query_jobs(self, project_id: str, limit: int, *, cycle: str = '') -> list[tuple[dict, str]]:
        """One query per due item per pass; persist completed siblings across limited rounds."""
        pending = [(w, [q for q in dict.fromkeys(w["queries"])
                        if q not in json.loads(w.get("completed_queries") or "[]")]) for w in self.due(project_id, cycle=cycle)]
        jobs = []
        for index in range(max((len(qs) for _, qs in pending), default=0)):
            for watch, queries in pending:
                if len(jobs) >= limit:
                    return jobs
                if index < len(queries):
                    jobs.append((watch, queries[index]))
        return jobs

    def mark_query(self, watch_id: str, query: str, *, success: bool, lifecycle_version: str | None = None) -> bool:
        """Only a complete successful query set advances cadence. Failures remain pending."""
        with self.monitor._db() as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT queries, completed_queries, status, lifecycle_version FROM studio_watchpoints WHERE id=?", (watch_id,)).fetchone()
            if not row or row['status'] == 'retired':
                return
            if lifecycle_version is not None and str(row['lifecycle_version']) != lifecycle_version:
                return
            queries = set(json.loads(row["queries"]))
            if query not in queries:  # edited while collection was running
                return
            done = set(json.loads(row["completed_queries"] or "[]")) & queries
            if success:
                done.add(query)
            stamp = now()
            if done == queries:
                db.execute("UPDATE studio_watchpoints SET completed_queries='[]', last_run_at=?, last_attempt_at=? WHERE id=?",
                           (stamp, stamp, watch_id))
            else:
                db.execute("UPDATE studio_watchpoints SET completed_queries=?, last_attempt_at=? WHERE id=?",
                           (json.dumps(sorted(done), ensure_ascii=False), stamp, watch_id))
            return done == queries

    def mark_run(self, watch_ids: list[str]) -> None:
        with self.monitor._db() as db, db:
            db.executemany("UPDATE studio_watchpoints SET last_run_at=? WHERE id=?", [(now(), w) for w in watch_ids])

    def has_plan(self, project_id: str) -> bool:
        """A plan exists once any item came through the plan (or a summary was written). Older one-off AI
        questions alone do not count — the first plan absorbs them."""
        return bool(self.meta(project_id)) or any(w.get("plan_origin") for w in self.items(project_id))

    def confirm_review(self, project_id: str) -> dict:
        """The owner reviewed the system's first plan: every current item becomes “reviewed” (system may only
        suggest closing or reducing them from now on). One change-log entry, not one per item."""
        with self.monitor._db() as db, db:
            count = db.execute("UPDATE studio_watchpoints SET reviewed_at=? WHERE project_id=? AND status!='retired' "
                               "AND (reviewed_at IS NULL OR reviewed_at='')", (now(), project_id)).rowcount
        self.set_meta(project_id, {"review": "done"}, actor="human", reason=f"确认首版方案（{count} 个关注点）")
        return self.view(project_id)

    # ------------------------------------------------------------------ growth → steady state
    def record_revision(self, project_id: str, counts: dict, trigger: str) -> None:
        log = self.revisions(project_id)
        log.insert(0, {"at": now(), "trigger": trigger, **{k: int(counts.get(k, 0)) for k in ("added", "noted", "applied", "proposed", "dropped", "incomplete")}})
        self.monitor.store.set_state(f"plan_revisions:{project_id}", json.dumps(log[:20], ensure_ascii=False))

    def revisions(self, project_id: str) -> list[dict]:
        raw = self.monitor.store.get_state(f"plan_revisions:{project_id}")
        try:
            return json.loads(raw) if raw else []
        except ValueError:
            return []

    def state(self, project_id: str) -> dict:
        """Stable requires complete, accepted rounds without structural changes."""
        log = self.revisions(project_id)
        if not self.items(project_id):
            return {"phase": "empty", "label": "尚未形成方案", "revisions": 0}
        recent = log[:STABLE_AFTER]
        stable = len(recent) == STABLE_AFTER and all(
            not any(r.get(k, 0) for k in ('added', 'proposed', 'applied', 'dropped', 'incomplete')) for r in recent)
        report = (self.monitor.store.get_plan(project_id) or {}).get('last_report') or {}
        if any(r.get('errors') or r.get('deferred') for r in report.values() if isinstance(r, dict)):
            stable = False
        return {"phase": "stable" if stable else "growing", "label": "稳定" if stable else "生长中",
                "revisions": len(log), "last": log[0] if log else None}

    def health(self, project_id: str, live: list[dict]) -> dict:
        """How the plan is doing: which items ran and caught anything, and how the owner treated system changes."""
        recent = (datetime.now(UTC) - timedelta(days=14)).isoformat()
        month = (datetime.now(UTC) - timedelta(days=30)).isoformat()
        with self.monitor._db() as db:
            hit_recent = {r[0] for r in db.execute(
                "SELECT DISTINCT h.watch_id FROM studio_watch_hits h JOIN studio_watchpoints w ON w.id=h.watch_id "
                "WHERE w.project_id=? AND h.created_at>=?", (project_id, month))}
            hit_ever = {r[0] for r in db.execute(
                "SELECT DISTINCT h.watch_id FROM studio_watch_hits h JOIN studio_watchpoints w ON w.id=h.watch_id "
                "WHERE w.project_id=?", (project_id,))}
            rows = db.execute("SELECT action, actor, state, reverted_at FROM studio_plan_changes WHERE project_id=?",
                              (project_id,)).fetchall()
        system_adds = [r for r in rows if r["actor"] == "system" and r["action"] == "add"]
        return {
            "items": len(live),
            "ran_14d": sum(1 for w in live if (w.get("last_run_at") or "") >= recent),
            "hit_30d": sum(1 for w in live if w["id"] in hit_recent),
            "never_hit": sum(1 for w in live if w["id"] not in hit_ever),
            "system_added": len(system_adds), "system_reverted": sum(1 for r in system_adds if r["reverted_at"]),
            "system_notes": sum(1 for r in rows if r["actor"] == "system" and r["action"] == "modify"),
            "proposals": {state: sum(1 for r in rows if r["state"] == state) for state in ("proposed", "accepted", "rejected")},
        }

    # ------------------------------------------------------------------ templates
    def apply_templates(self, project_id: str, keys: list[str], *, actor: str = "human") -> dict:
        """Use industry templates as this project's method and section baselines; sections with project-specific
        baselines already set keep them. Focus-point templates are offered in the page, not auto-added."""
        merged = combine(keys)
        if not merged["sections"]:
            raise ValueError("请选择至少一个行业模板")
        current = self.meta(project_id).get("sections") or {}
        sections = {key: {"baseline": (current.get(key) or {}).get("baseline") or text,
                          "known": (current.get(key) or {}).get("known", ""),
                          "coverage": (current.get(key) or {}).get("coverage", "none")}
                    for key, text in merged["sections"].items()}
        return self.set_meta(project_id, {"templates": keys, "method": self.meta(project_id).get("method") or merged["method"],
                                          "sections": sections}, actor=actor, reason="套用行业模板：" + "、".join(keys))

    # ------------------------------------------------------------------ module data
    def _hits(self, db, watch_id: str, limit: int = 30) -> list[dict]:
        rows = db.execute("""SELECT i.id, i.title, i.url, i.source, i.importance, i.category, i.summary,
                                    COALESCE(i.published_at, i.collected_at) AS date
                             FROM studio_watch_hits h JOIN studio_items i ON i.id=h.item_id
                             WHERE h.watch_id=? AND i.relevant=1 ORDER BY date DESC LIMIT ?""", (watch_id, limit)).fetchall()
        return [{**dict(r), "date": (r["date"] or "")[:10]} for r in rows]

    def _keys(self, project_id: str, watch: dict) -> list[str]:
        """Names a hit should mention to count for this item: its objects (with short forms) and, for the
        company's own sections, the company's names. Empty means no filter."""
        keys: list[str] = []
        for obj in watch.get("objects") or []:
            keys += peer_names(str(obj))
        for peer in (watch.get("view_params") or {}).get("peers") or []:
            keys += peer_names(str(peer))
        if (watch.get("axis") or "asset") in OWN:
            from project_watch.studio.structured_facts import own_names

            project = self.monitor.store.get_project(project_id) or {}
            # 希格生科(深圳)有限公司 → 希格生科…; never the bare region or "有限公司".
            keys += own_names(project.get("legal_name") or project.get("name") or "", project)
        return [k for k in dict.fromkeys(keys) if k not in ("本公司融资", "本公司")]

    def _focused(self, project_id: str, watch: dict, hits: list[dict]) -> list[dict]:
        """What the module shows (raw hits stay stored). Items about the company itself (自身进展、商业化、资本) show
        only headlines that name the company or the item's assets — a model summary mentioning the company is not
        enough. External items (竞争、需求、技术…) were already judged against the question in triage; reposts merge."""
        own = (watch.get("axis") or "asset") in OWN
        keys = [k.casefold() for k in self._keys(project_id, watch)] if own else []
        kept: list[dict] = []
        for hit in hits:
            if keys and not any(k in hit["title"].casefold() for k in keys):
                continue
            if any(hit.get("date") == k.get("date") and _same_story(hit["title"], k["title"]) for k in kept):
                continue
            kept.append(hit)
        return kept

    def coverage(self, project_id: str) -> dict:
        """常规采集 per section: what is collected every round, how much in 30 days, and what is off or failing."""
        plan = self.monitor.store.get_plan(project_id) or {}
        report = plan.get("last_report") or {}
        since = (datetime.now(UTC) - timedelta(days=30)).isoformat()
        with self.monitor._db() as db:
            events = dict(db.execute("SELECT category, count(*) FROM studio_events WHERE project_id=? AND status!='dismissed' "
                                     "AND COALESCE(event_at, created_at)>=? GROUP BY category", (project_id, since)).fetchall())
            sites = db.execute("SELECT count(*) FROM studio_site_pages WHERE project_id=?", (project_id,)).fetchone()[0]

            def count(sql, *args):
                try:
                    return db.execute(sql, args).fetchone()[0]
                except Exception:  # optional tables on older stores
                    return 0
            facts = count("SELECT count(*) FROM studio_fact_records WHERE project_id=?", project_id)
            vendor = count("SELECT count(*) FROM studio_commercial_calls WHERE project_id=? AND status IN ('ok','partial')", project_id)
        out: dict[str, list[dict]] = {key: [] for key, _l, _b in FRAMEWORK}  # 同需求替代 has no standing source yet
        for key, label, section, category, needs in CHANNELS:
            row = report.get(key) or {}
            off = (needs == "search" and not plan.get("has_search")) or (needs == "registry" and not plan.get("has_registry"))
            if key == "site":
                amount, unit = sites, "个页面"
                off = off or not sites
            elif key == "facts":
                amount, unit = facts, "条"
            elif key == "commercial":
                amount, unit = vendor, "次查询"
                off = not vendor
            else:
                amount, unit = (events.get(category, 0) if category else row.get("added", 0)), "条"
            state = "off" if off else "error" if row.get("errors") and not row.get("checked") else "on"
            if not plan and key not in ("facts", "commercial", "site"):
                state = "pending"
            out[section].append({"key": key, "label": label, "count": amount, "unit": unit, "state": state,
                                 "window": "近 30 天" if category else "上一轮" if key not in ("site", "facts", "commercial") else "累计"})
        return out

    def latest(self, project_id: str, watch: dict) -> dict:
        """The one line a reader needs per focus point: the system's current understanding if it has one,
        else the newest on-topic hit, else whether it has been searched at all. ``fresh`` = within 7 days."""
        week_ago = (datetime.now(UTC) - timedelta(days=7)).isoformat()[:10]
        with self.monitor._db() as db:
            hits = self._focused(project_id, watch, self._hits(db, watch["id"], limit=20))
        # Newest major/notable hit first; a routine listing only when nothing more telling exists.
        top = next((h for h in hits if h.get("importance") in ("major", "notable")), None)
        if watch.get("current_state"):
            date = (watch.get("updated_at") or "")[:10]
            return {"kind": "note", "text": watch["current_state"], "date": date, "url": "",
                    "count": len(hits), "fresh": bool(date and date >= week_ago) or bool(top and top["date"] >= week_ago)}
        if top:
            return {"kind": "hit", "text": top["title"], "date": top["date"], "url": top["url"], "count": len(hits),
                    "fresh": bool(top["date"] and top["date"] >= week_ago)}
        if hits:  # only routine listings: not news, but the reader should know the search found something
            return {"kind": "none", "text": f"已检索，仅有一般信息（{len(hits)} 条）", "date": hits[0]["date"], "url": "",
                    "count": len(hits), "fresh": False}
        return {"kind": "none", "text": "已检索，暂无新信息" if watch.get("last_run_at") else "尚未检索",
                "date": (watch.get("last_run_at") or "")[:10], "url": "", "count": 0, "fresh": False}

    def module(self, project_id: str, watch: dict) -> dict:
        """Data for the item's display module, from stored records only (hits, events, entered data points)."""
        view = watch.get("view") or "evidence"
        params = watch.get("view_params") or {}
        with self.monitor._db() as db:
            hits = self._focused(project_id, watch, self._hits(db, watch["id"], limit=60))
            if view == "timeline":
                planned = [{"date": str(m.get("date", ""))[:10], "title": str(m.get("label", ""))[:120], "planned": True}
                           for m in params.get("milestones", []) if isinstance(m, dict) and m.get("label")]
                done = []
                for h in hits:  # Different events may occur on the same date.
                    done.append({"date": h["date"], "title": h["title"], "url": h["url"], "importance": h["importance"], "planned": False})
                    if len(done) >= 10:
                        break
                return {"view": view, "points": sorted(done + planned, key=lambda x: x["date"] or "9999")}
            if view == "peer_table":
                since90 = (datetime.now(UTC) - timedelta(days=90)).isoformat()
                rows = []
                for entry in (params.get("peers") or watch.get("objects") or [])[:8]:
                    names = peer_names(str(entry))
                    if not names:
                        continue
                    where = " OR ".join("title LIKE ?" for _ in names)
                    likes = [f"%{n}%" for n in names]
                    found = db.execute(f"""SELECT title, category, COALESCE(event_at, created_at) AS at FROM studio_events
                                           WHERE project_id=? AND status!='dismissed' AND ({where}) ORDER BY at DESC LIMIT 1""",
                                       (project_id, *likes)).fetchone()
                    recent = db.execute(f"""SELECT count(*) FROM studio_events WHERE project_id=? AND status!='dismissed'
                                            AND ({where}) AND COALESCE(event_at, created_at) >= ?""",
                                        (project_id, *likes, since90)).fetchone()[0]
                    rows.append({"name": str(entry).split("|")[0].strip(), "latest": dict(found) if found else None,
                                 "recent_90d": recent})
                return {"view": view, "rows": rows}
            if view == "metric_series":
                points = [dict(r) for r in db.execute("SELECT id, period, value, unit, note, url, actor FROM studio_metric_points "
                                                      "WHERE watch_id=? ORDER BY period", (watch["id"],))]
                return {"view": view, "metric": params.get("metric", ""), "unit": params.get("unit", ""),
                        "target": params.get("target"), "target_label": params.get("target_label", ""),
                        "source_hint": params.get("source_hint", ""), "points": points}
            if view == "commitments":
                rows = []
                for c in params.get("commitments", []):
                    if not isinstance(c, dict) or not c.get("claim"):
                        continue
                    keys = [k for k in c.get("keywords", []) if isinstance(k, str) and len(k) >= 2]
                    match = next((h for h in hits if any(k in h["title"] for k in keys)), None)
                    rows.append({"claim": c["claim"], "source": c.get("source", ""), "due": c.get("due", ""),
                                 "progress": c.get("progress", ""), "evidence": match})
                return {"view": view, "rows": rows}
        return {"view": "evidence", "items": hits, "total": len(hits)}

    def add_point(self, watch_id: str, *, period: str, value: float, unit: str = "", note: str = "", url: str = "",
                  actor: str = "human") -> dict:
        with self.monitor._db() as db:
            row = db.execute("SELECT project_id FROM studio_watchpoints WHERE id=?", (watch_id,)).fetchone()
        if not row:
            raise KeyError(watch_id)
        if actor not in ACTORS:
            raise ValueError("unknown actor")
        period = str(period).strip()[:20]
        if not period:
            raise ValueError("缺少期间")
        if isinstance(value, bool) or not math.isfinite(float(value)):
            raise ValueError("请输入有效数值")
        if url and not url.startswith(("http://", "https://")):
            raise ValueError("来源链接格式不正确")
        point = {"id": uuid.uuid4().hex, "project_id": row[0], "watch_id": watch_id, "period": period, "value": float(value),
                 "unit": unit[:20], "note": note[:200], "url": url[:500], "actor": actor, "created_at": now()}
        with self.monitor._db() as db, db:
            db.execute("INSERT INTO studio_metric_points VALUES (:id,:project_id,:watch_id,:period,:value,:unit,:note,:url,:actor,:created_at)", point)
        return point

    def remove_point(self, point_id: str) -> None:
        with self.monitor._db() as db, db:
            if db.execute("DELETE FROM studio_metric_points WHERE id=?", (point_id,)).rowcount == 0:
                raise KeyError(point_id)
