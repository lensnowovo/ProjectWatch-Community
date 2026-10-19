"""Industry templates for monitoring plans: generic method, no project data.

A template rewrites what each framework section monitors for an industry and offers focus-point
templates (question pattern, section, display module, cadence). A project may combine several
templates (e.g. 创新药 + 平台服务). Templates are data: a new industry needs a new entry here, not
new page code. Display modules are the fixed library in ``MODULES``.

Core: constants only; no model, search or network calls.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

_log = logging.getLogger(__name__)
SECTION_KEYS = ("asset", "operations", "peers", "alternatives", "customer", "partners", "market", "science", "rules")
# Local templates written for the earlier eight sections still load; their keys map onto the nine dimensions.
LEGACY_KEYS = {"self": "asset", "demand": "alternatives", "competition": "peers", "technology": "science",
               "commercial": "operations", "supply": "partners", "capital": "operations"}
# Instance-local templates (e.g. converted NEI industry methods or the owner's own), loaded from the data
# directory at start-up. They are validated like built-ins and never shadow a built-in key.
LOCAL: dict[str, dict] = {}

# Display modules the frontend knows how to render. Each focus point picks one.
MODULES = {
    "evidence": "证据列表：命中的报道、论文、公告，按时间倒序",
    "timeline": "里程碑时间线：已发生节点与计划节点，按时间排列",
    "peer_table": "同行对照表：每个对象最近一次动作与近 90 天动态数",
    "metric_series": "指标序列：带来源的数值点，可叠加目标线",
    "commitments": "承诺对照：资料中的承诺与公开进展逐项对照",
}

TEMPLATES: dict[str, dict] = {
    "innovative_drug": {
        "label": "创新药",
        "method": "创新药方法：开发阶段与关键读出、监管路径、同靶点/同病种竞争、交易与资本。",
        "sections": {
            "asset": "管线各资产的临床登记、入组、剂量与读出节点；IND 与关键监管里程碑。",
            "operations": "团队与关键人才、融资与资金跑道、股东结构、公开计划是否兑现。",
            "peers": "同靶点、同机制资产的开发阶段与读出；竞争门槛与差异化。",
            "alternatives": "同一适应症的其他治疗路线、现有标准治疗与指南变化、未满足需求的大小。",
            "partners": "授权与合作的真实状态（意向 → 协议 → 里程碑付款 → 销售分成）、CRO/CDMO 与临床中心依赖、收益分成与权利归属。",
            "market": "同赛道融资与 BD 交易、可比估值。",
            "science": "核心机制的人体证据强弱、论文与复现、负面结果。",
            "rules": "审评与特殊通道（突破性、孤儿药、快速通道）、临床试验法规、医保与定价。",
        },
        "focus": [
            {"section": "asset", "text": "{资产} 的临床是否按计划推进（入组、读出、下一阶段）？", "view": "timeline", "cadence": "high"},
            {"section": "peers", "text": "{靶点或机制} 同类资产的人体数据与开发节奏如何变化？", "view": "peer_table", "cadence": "medium"},
            {"section": "alternatives", "text": "{适应症} 患者的治疗标准与替代方案在变吗？", "view": "evidence", "cadence": "low"},
            {"section": "operations", "text": "资金能支撑到哪个临床节点，下一轮何时？", "view": "timeline", "cadence": "medium"},
            {"section": "operations", "text": "资料中的研发与融资承诺是否兑现？", "view": "commitments", "cadence": "medium"},
        ],
    },
    "platform_service": {
        "label": "平台与技术服务",
        "method": "平台型业务：方法学的外部验证、对外合作与服务收入的真实状态、复购与客户集中度。",
        "sections": {
            "science": "平台方法的预测能力、可复现性、外部验证与反例。",
            "customer": "服务客户的采用与复购、客户集中度。",
            "partners": "对外合作的状态（宣布 → 开展 → 交付 → 复购 → 收入）。",
            "rules": "监管对新方法学的态度。",
        },
        "focus": [
            {"section": "science", "text": "平台方法有没有新的外部验证或反例？", "view": "evidence", "cadence": "low"},
            {"section": "partners", "text": "平台合作有没有转成可持续收入？", "view": "timeline", "cadence": "medium"},
        ],
    },
    "auto_parts": {
        "label": "汽车零部件",
        "method": "汽车零部件方法：客户定点与车型生命周期、产能爬坡与良率、年降与原材料成本、接口与标准路线、车规认证。",
        "sections": {
            "asset": "主要产品的新品研发与定点、良率与一致性、新应用领域。",
            "operations": "新基地与产线的投产与爬坡、订单与交付规模、单价与年降、毛利、产能利用率、融资与上市安排、对外承诺的兑现。",
            "peers": "同类零部件厂商的定点、产能、价格与毛利；外资品牌的国产替代进度；新进入者。",
            "alternatives": "电子电气架构与连接方式的演进，是否让零部件需求扩张、收缩或被替代。",
            "customer": "主要客户（主机厂、Tier1、线束厂）的定点、量产、份额、车型改款与停产、回款；客户终端销量与单车需求。",
            "partners": "关键原材料（金属、塑料、线缆等）的供应商与替代来源、设备与模具交付；投资方动向。",
            "market": "汽车销量与新能源渗透率、原材料价格、行业融资与并购。",
            "science": "接口、传输技术与专利。",
            "rules": "车规认证（IATF 16949、AEC 等）、安全与电磁兼容标准、产业政策与地方扶持、出口与关税。",
        },
        "focus": [
            {"section": "customer", "text": "主要客户 {客户} 的定点与量产有没有变化？", "view": "evidence", "cadence": "medium"},
            {"section": "customer", "text": "{客户车系} 的销量能否支撑收入假设？", "view": "metric_series", "cadence": "medium"},
            {"section": "operations", "text": "新基地投产后，产能是否按计划爬坡？", "view": "timeline", "cadence": "medium"},
            {"section": "peers", "text": "同类厂商在定点、产能与价格上如何动作？", "view": "peer_table", "cadence": "medium"},
            {"section": "market", "text": "{原材料} 价格会不会压缩毛利？", "view": "metric_series", "cadence": "low"},
            {"section": "operations", "text": "资料中的扩产与收入承诺是否兑现？", "view": "commitments", "cadence": "medium"},
        ],
    },
    "new_materials": {
        "label": "新材料",
        "method": "新材料方法：性能指标与客户认证、规模化与一致性、成本曲线、下游应用导入与供应依赖。",
        "sections": {
            "asset": "关键产品的性能指标、认证进展、良率与一致性。",
            "operations": "中试与量产线进度、产能与成本。",
            "peers": "同类材料厂商的产能与价格。",
            "alternatives": "替代材料与替代工艺的进展。",
            "customer": "下游客户的验证、导入与批量采购；应用场景的需求变化。",
            "partners": "上游原料与设备的供应商与替代来源。",
            "market": "原料价格、下游行业景气度。",
            "science": "工艺路线、专利与第三方测试。",
        },
        "focus": [
            {"section": "customer", "text": "下游客户 {客户} 的验证与导入走到哪一步？", "view": "timeline", "cadence": "medium"},
            {"section": "market", "text": "{原料} 价格与供应会不会影响成本？", "view": "metric_series", "cadence": "low"},
        ],
    },
    "synthetic_biology": {
        "label": "合成生物",
        "method": "合成生物方法：菌株与酶、发酵与纯化放大、产品与市场、成本与客户导入。",
        "sections": {
            "asset": "菌株与工艺迭代、产品获批与上市。",
            "operations": "放大与产线投产、产能与成本。",
            "peers": "同类产品与工艺路线的厂商动作、价格竞争。",
            "alternatives": "化学合成、天然提取等替代来源的成本与供给。",
            "customer": "下游品牌与制造商的导入、复购与产品需求变化。",
            "partners": "发酵产能与外包、原料供应商。",
            "market": "糖等原料与能源成本、产品价格。",
            "science": "工艺放大的证据、转化率、论文与专利。",
            "rules": "新原料与新食品原料审批、化妆品原料备案、环保要求。",
        },
        "focus": [
            {"section": "operations", "text": "新产线或放大工艺是否按计划投产？", "view": "timeline", "cadence": "medium"},
            {"section": "rules", "text": "{产品} 的审批或备案进展如何？", "view": "timeline", "cadence": "medium"},
        ],
    },
}


def _all() -> dict[str, dict]:
    return {**TEMPLATES, **LOCAL}


def catalog() -> list[dict]:
    return [{"key": key, "label": t["label"], "method": t["method"], "sections": t["sections"], "focus": t["focus"],
             "source": t.get("source", "内置")} for key, t in _all().items()]


def validate(data: dict) -> dict:
    """One template from JSON: key/label/method, per-section baselines, focus-point templates."""
    if not isinstance(data, dict):
        raise ValueError("template must be an object")
    key = str(data.get("key") or "")
    if not re.fullmatch(r"[a-z][a-z0-9_]{2,40}", key) or key in TEMPLATES:
        raise ValueError("template key must be a new lowercase slug")
    raw = data.get("sections") or {}
    if not isinstance(raw, dict) or not raw or any(LEGACY_KEYS.get(k, k) not in SECTION_KEYS for k in raw):
        raise ValueError("template sections must use the framework keys")
    sections: dict[str, str] = {}
    for k, v in raw.items():
        key = LEGACY_KEYS.get(k, k)
        sections[key] = f"{sections[key]} {v}" if key in sections else str(v)
    focus = []
    for row in data.get("focus") or []:
        if isinstance(row, dict) and row.get("section") in LEGACY_KEYS:
            row = {**row, "section": LEGACY_KEYS[row["section"]]}
        if (not isinstance(row, dict) or row.get("section") not in SECTION_KEYS or row.get("view") not in MODULES
                or not 4 <= len(str(row.get("text") or "")) <= 60 or row.get("cadence", "medium") not in ("high", "medium", "low", "event")):
            raise ValueError("invalid focus template")
        focus.append({"section": row["section"], "text": str(row["text"]), "view": row["view"], "cadence": row.get("cadence", "medium")})
    return {"label": str(data.get("label") or key)[:20], "method": str(data.get("method") or "")[:400],
            "sections": {k: str(v)[:400] for k, v in sections.items()}, "focus": focus[:12],
            "source": str(data.get("source") or "本地")[:60]}


def load_local(directory: Path) -> int:
    """Read ``*.json`` templates from the instance data directory; invalid files are skipped and logged."""
    LOCAL.clear()
    if not directory.is_dir():
        return 0
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            LOCAL[data["key"]] = validate(data)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            _log.warning("plan template %s skipped: %s", path.name, exc)
    return len(LOCAL)


def combine(keys: list[str]) -> dict:
    """Merged method text and per-section baselines for the chosen templates (later templates add, not replace)."""
    chosen = [_all()[k] for k in keys if k in _all()]
    sections: dict[str, str] = {}
    for template in chosen:
        for section, text in template["sections"].items():
            sections[section] = f"{sections[section]} {text}" if section in sections and text not in sections[section] else text
    return {"method": " ".join(t["method"] for t in chosen), "sections": sections,
            "focus": [f for t in chosen for f in t["focus"]]}
