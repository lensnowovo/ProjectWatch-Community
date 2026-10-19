"""Stored judgment contracts and history; no private generation implementation."""

from __future__ import annotations

import json
from contextlib import closing
from datetime import UTC, datetime

STATE_KEY = "insight:{}"
HISTORY_KEY = "insight_history:{}"

VERDICTS: dict[str, dict[str, str]] = {
    "position": {"leading": "领先", "ahead": "略领先", "on_par": "相当", "behind": "略落后", "lagging": "落后", "unclear": "无法判断"},
    "maturity": {"frontier": "前沿", "advanced": "较先进", "mainstream": "主流", "mature": "成熟", "dated": "趋于落后", "unclear": "无法判断"},
    "consensus": {"consensus": "行业共识", "forming": "共识正在形成", "contrarian": "非共识", "unclear": "无法判断"},
    "industry": {"expanding": "快速扩张", "growing": "增长", "stable": "平稳", "consolidating": "整合", "cooling": "降温", "unclear": "无法判断"},
    # 四圈 judgments shown on 当前判断: the company itself and its chain (customers, suppliers, partners, investors).
    "operations": {"improving": "向好", "steady": "平稳", "pressured": "承压", "unclear": "无法判断"},
    "chain": {"favorable": "有利", "steady": "平稳", "pressured": "承压", "unclear": "无法判断"},
}

SOURCE_LABEL = {"D": "内部资料", "M": "财务指标", "F": "融资数据库", "N": "公司新闻", "C": "竞品动态", "I": "行业与政策", "A": "论文",
                "P": "专利", "T": "临床试验", "L": "竞争与行业整理", "W": "公开档案"}

def counts(numbered: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    for code in numbered:
        out[code[0]] = out.get(code[0], 0) + 1
    return {p: out[p] for p in SOURCE_LABEL if p in out}

def load(store, project_id: str) -> dict | None:
    raw = store.get_state(STATE_KEY.format(project_id))
    return json.loads(raw) if raw else None


def history(store, project_id: str) -> list[dict]:
    raw = store.get_state(HISTORY_KEY.format(project_id))
    return json.loads(raw) if raw else []


def save(store, project_id: str, analysis: dict, gathered: dict, model: str = "") -> dict:
    record = {"analysis": analysis, "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
              "fingerprint": gathered["fingerprint"], "counts": counts(gathered["numbered"]),
              "total": len(gathered["numbered"]), "model": model}
    store.set_state(STATE_KEY.format(project_id), json.dumps(record, ensure_ascii=False))
    past = history(store, project_id)
    past.insert(0, {"generated_at": record["generated_at"], "headline": analysis["headline"],
                    "position": analysis["position"]["verdict"], "maturity": analysis["technology"]["maturity"],
                    "consensus": analysis["technology"]["consensus"], "industry": analysis["industry"]["verdict"],
                    "operations": analysis["operations"]["verdict"], "chain": analysis["chain"]["verdict"]})
    store.set_state(HISTORY_KEY.format(project_id), json.dumps(past[:10], ensure_ascii=False))
    return record

def held(store, project_id: str) -> bool:
    """A paid model request with an unknown outcome holds the whole project until its owner confirms recovery.

    Deep analysis is another model round; it must not bypass that hold (project-wide, like question rounds).
    """
    from project_watch.studio.recovery import UNRESOLVED

    with closing(store._connect()) as db:
        return bool(db.execute(f"SELECT 1 FROM studio_job_calls c JOIN studio_jobs j ON j.id=c.job_id "
                               f"WHERE j.project_id=? AND c.kind='model' AND {UNRESOLVED} LIMIT 1", (project_id,)).fetchone())
