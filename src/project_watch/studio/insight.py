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
    record = json.loads(raw) if raw else None
    if record and record.get('artifact_key'):
        dependency = store.evidence.dependencies.get(project_id, record['artifact_key'])
        record.update(dependency=dependency, status=(dependency or {}).get('status', 'stale'))
    return record


def history(store, project_id: str) -> list[dict]:
    raw = store.get_state(HISTORY_KEY.format(project_id))
    result = json.loads(raw) if raw else []
    with closing(store._connect()) as db:
        available = db.execute("SELECT 1 FROM sqlite_master WHERE name='studio_insight_versions'").fetchone()
        if available:
            rows = db.execute('SELECT v.record FROM studio_insight_versions v JOIN studio_primary_objects p '
                "ON v.project_id=p.project_id AND v.artifact_key='insight:' || p.object_id WHERE v.project_id=? "
                'ORDER BY v.created_at DESC,v.rowid DESC LIMIT 10', (project_id,)).fetchall()
            for row in rows:
                version = json.loads(row[0])
                analysis = version['analysis']
                result.append(dict(version_id=version['id'], generated_at=version['generated_at'], headline=analysis['headline'],
                    position=analysis['position']['verdict'], maturity=analysis['technology']['maturity'],
                    consensus=analysis['technology']['consensus'], industry=analysis['industry']['verdict']))
    return sorted(result, key=lambda r: r['generated_at'], reverse=True)[:10]


def save(store, project_id: str, analysis: dict, gathered: dict, model: str = "") -> dict:
    current = load(store, project_id)
    if current and current.get('artifact_key'):
        raise ValueError('versioned insight requires fixed-input publication; legacy write cannot replace it')
    record = {"analysis": analysis, "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
              "fingerprint": gathered["fingerprint"], "counts": counts(gathered["numbered"]),
              "total": len(gathered["numbered"]), "model": model}
    past = history(store, project_id)
    past.insert(0, {"generated_at": record["generated_at"], "headline": analysis["headline"],
                    "position": analysis["position"]["verdict"], "maturity": analysis["technology"]["maturity"],
                    "consensus": analysis["technology"]["consensus"], "industry": analysis["industry"]["verdict"],
                    "operations": analysis["operations"]["verdict"], "chain": analysis["chain"]["verdict"]})
    with closing(store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        table = db.execute("SELECT 1 FROM sqlite_master WHERE name='studio_insight_heads'").fetchone()
        if table and db.execute('SELECT 1 FROM studio_insight_heads WHERE project_id=?', (project_id,)).fetchone():
            raise ValueError('versioned insight requires fixed-input publication; legacy write cannot replace it')
        for key, value in ((STATE_KEY.format(project_id), record), (HISTORY_KEY.format(project_id), past[:10])):
            db.execute('INSERT INTO studio_state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                       (key, json.dumps(value, ensure_ascii=False)))
    return record

def held(store, project_id: str) -> bool:
    """A paid model request with an unknown outcome holds the whole project until its owner confirms recovery.

    Deep analysis is another model round; it must not bypass that hold (project-wide, like question rounds).
    """
    from project_watch.studio.recovery import UNRESOLVED

    with closing(store._connect()) as db:
        return bool(db.execute(f"SELECT 1 FROM studio_job_calls c JOIN studio_jobs j ON j.id=c.job_id "
                               f"WHERE j.project_id=? AND c.kind='model' AND {UNRESOLVED} LIMIT 1", (project_id,)).fetchone())
