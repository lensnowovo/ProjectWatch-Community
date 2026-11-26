"""Reuse a relevant verdict for the same headline reposted by another outlet.

Within three days, a report whose cleaned headline equals one already judged relevant
(`events.same_report`) takes that judgement instead of a new model request, so one story is
screened once and graded alike. Only relevant verdicts are reused: another outlet's text may name
the company where the first did not, so an item judged irrelevant is screened again. Verdicts the
model never gave (no model, a cut-off or refused reply, an item the model skipped, the person's own
exclusion rules) are never copied. Reposts within one batch wait for the first copy's verdict.
"""
from __future__ import annotations

import json
import re
from contextlib import closing

from project_watch.studio import events as ev

FALLBACKS = frozenset({'no_model', 'model_cut_off', 'model_rejected'})


def key(title: str) -> str | None:
    cleaned = ev.headline_assertion(ev.clean_headline(title or ''))
    return cleaned if len(re.sub(r'\d', '', cleaned)) >= 10 else None


def _when(row):
    return ev.parse_time(row.get('published_at') or row.get('collected_at'))


def _usable(row) -> bool:
    try:
        extra = json.loads(row['extra']) if isinstance(row.get('extra'), str) else (row.get('extra') or {})
    except ValueError:
        extra = {}
    binding = extra.get('triage_binding') if isinstance(extra, dict) else None
    if isinstance(binding, dict) and binding.get('status') in FALLBACKS:
        return False
    if (row.get('impact') or '').startswith(('按你的规则排除', '系统尚未从公告原文定位本公司')):
        return False  # the person's rule, or a filing note, belongs to that item alone
    return (row.get('summary') or '') != (row.get('title') or '')[:200]  # the model skipped it


def plan(store, project_id: str, batch: list[dict], deferred: set[str] | None = None
         ) -> tuple[list[tuple[dict, dict]], list[dict]]:
    """(item, verdict) pairs reused from earlier items, and the items still to screen now.

    A repost of another item in this batch waits for the next batch, by which time that item's
    verdict is stored. It waits once: if that verdict cannot be reused (irrelevant, cut off), the
    remaining copies are screened together rather than one request each. ``deferred`` records
    the items that already waited.
    """
    deferred = set() if deferred is None else deferred
    news = [b for b in batch if b.get('category') not in ('paper', 'patent') and key(b['title']) and _when(b)]
    if not news:
        return [], batch
    keys = {key(b['title']) for b in news}
    since = (min(_when(b) for b in news) - ev.WINDOW).isoformat()
    earlier: dict[str, list[dict]] = {}
    with closing(store._connect()) as db:
        db.row_factory = lambda cursor, values: {c[0]: v for c, v in zip(cursor.description, values, strict=True)}
        # Never from an item or event the person dismissed or corrected: their judgement, not the
        # model's first verdict, is what the story now carries.
        rows = db.execute("SELECT i.id, i.title, i.importance, i.summary, i.impact, i.category, i.published_at, "
                          "i.collected_at, i.extra FROM studio_items i LEFT JOIN studio_events e ON e.id=i.event_id "
                          "WHERE i.project_id=? AND i.triaged=1 AND i.relevant=1 AND i.status!='dismissed' "
                          "AND i.category NOT IN ('paper', 'patent') AND COALESCE(i.published_at, i.collected_at)>=? "
                          "AND COALESCE(e.status, '')!='dismissed' AND NOT EXISTS (SELECT 1 FROM studio_feedback f "
                          "WHERE f.project_id=i.project_id AND f.event_id=i.event_id)",
                          (project_id, since)).fetchall()
    for row in rows:
        row_key = key(row['title'])
        if row_key in keys and _when(row) and _usable(row):
            earlier.setdefault(row_key, []).append(row)
    reused, screen, first = [], [], {}
    for item in batch:
        item_key = key(item['title']) if item in news else None
        if item_key is None:
            screen.append(item)
            continue
        close = [r for r in earlier.get(item_key, []) if abs(_when(r) - _when(item)) <= ev.WINDOW]
        if close:
            source = min(close, key=lambda r: abs(_when(r) - _when(item)))
            reused.append((item, {'id': item['id'], 'relevant': True, 'importance': source['importance'],
                                  'summary': source['summary'], 'impact': source['impact'],
                                  'category': source['category'],
                                  'triage_binding': {'version': 1, 'status': 'repost', 'of': source['id']}}))
        elif (item['id'] not in deferred and item_key in first
              and abs(_when(first[item_key]) - _when(item)) <= ev.WINDOW):
            deferred.add(item['id'])  # screened with the next batch, after its first copy
        else:
            first.setdefault(item_key, item)
            screen.append(item)
    return reused, screen
