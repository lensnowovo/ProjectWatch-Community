"""Stored-only explanations for news/report windows; never start research."""
from datetime import UTC, datetime, timedelta

from project_watch.studio import progress


def iter_events(store, **filters):
    offset = 0
    while True:
        batch = store.list_events(limit=500, offset=offset, **filters)
        yield from batch
        if len(batch) < 500:
            break
        offset += len(batch)


def summarize(store, days, *, project_id=None, category=None, importance=None, exclude_paused=False):
    end = datetime.now(UTC)
    start = end - timedelta(days=days)
    projects = [p for p in store.list_projects() if (not project_id or p['id'] == project_id)
                and p.get('status') != 'removed' and (not exclude_paused or p.get('status') != 'paused')]
    ids = {p['id'] for p in projects}
    counts = dict(total=0, in_period=0, matching=0, undated=0, future=0, older=0,
                  pending=0, routine=0, major=0, notable=0)
    for event in iter_events(store, project_id=project_id, category=category):
        if event['project_id'] not in ids:
            continue
        counts['total'] += 1
        value = event.get('news_date')
        try:
            stamp = datetime.fromisoformat(value)
            stamp = stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp.astimezone(UTC)
        except (ValueError, TypeError):
            counts['undated'] += 1
            continue
        if stamp > end:
            counts['future'] += 1
        elif stamp < start:
            counts['older'] += 1
        else:
            counts['in_period'] += 1
            level = 'pending' if event.get('importance_status') == 'pending' else event['importance']
            counts[level] += 1
            if not importance or event['importance'] == importance:
                counts['matching'] += 1
    states = [{'id': p['id'], **progress.read(store, p['id'], days=days)} for p in projects]
    return {'counts': counts, 'progress': states}


def explanation(summary):
    c = summary['counts']
    parts = []
    if c['in_period']:
        parts.append(f"期内有 {c['in_period']} 条动态：重大 {c['major']} 条，关注 {c['notable']} 条，"
                     f"一般 {c['routine']} 条，待评级 {c['pending']} 条。")
    else:
        parts.append('所选期间暂无有可靠日期的动态。')
    if c['undated']:
        parts.append(f"另有 {c['undated']} 条日期待核，可在项目外部信息查看。")
    if c['older']:
        parts.append(f"{c['older']} 条早于所选期间。")
    if c['future']:
        parts.append(f"{c['future']} 条日期在未来，暂不计入本期。")
    return ' '.join(parts)
