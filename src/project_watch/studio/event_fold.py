"""Once per project: fold stored events that are separate copies of one real-world event.

Uses the clustering rules (`events.Group`), the model only for ambiguous pairs, and the
receipted, undoable `EventRevisions.merge`: members move to one event, the others are
dismissed as merged. No new assertions are written.
"""

from __future__ import annotations

from contextlib import closing

from project_watch.studio import events as ev

KEY = 'event_fold_v1:{}'


def _as_item(row) -> dict:
    return {'id': row['id'], 'title': row['title'] or '', 'summary': row['summary'] or '',
            'category': row['category'], 'importance': row['importance'],
            'published_at': row['event_at'], 'collected_at': row['created_at']}


def plan(rows: list, confirm=None) -> list[list[str]]:
    """Groups of event ids (two or more) that are one event, earliest first."""
    items = [_as_item(r) for r in rows]
    groups, ambiguous = ev.plan_groups(items, [])
    if ambiguous and confirm is not None:
        redirected = {}
        confirmed = confirm(ambiguous)
        for index in sorted(confirmed):
            if not 0 <= index < len(ambiguous):
                continue
            item, target = ambiguous[index]
            while target in redirected:
                target = redirected[target]
            own = next((g for g in groups if item in g.items), None)
            # A confirmed pair moves the item's whole group, if the target takes every member.
            if target not in groups or own is target or own is None or not all(target.admits(i, getattr(confirmed, "proofs", {}).values()) for i in own.items):
                continue
            groups.remove(own)
            redirected[own] = target
            for member in own.items:
                target.add(member)
    return [[i['id'] for i in g.items] for g in groups if len(g.items) > 1]


def fold(pipeline, execution, project_id: str, llm) -> int:
    """Merge duplicate events once per project; returns how many events were folded away."""
    store = pipeline.store
    key = KEY.format(project_id)
    if store.get_state(key):
        return 0
    with closing(store._connect()) as db:
        rows = db.execute("SELECT id, title, summary, category, importance, event_at, created_at FROM studio_events "
                          "WHERE project_id=? AND status!='dismissed' ORDER BY COALESCE(event_at, created_at), created_at",
                          (project_id,)).fetchall()
    complete = True

    def confirm(pairs):
        nonlocal complete
        if llm is None or not llm.ready:
            complete = False
            return set()
        confirmed = pipeline.same_event_pairs(llm, pairs)
        complete &= confirmed.complete
        return confirmed
    by_id = {r['id']: r for r in rows}
    folded = 0
    for ids in plan(rows, confirm=confirm):
        # The lead keeps its title and grade: the highest importance, then the earliest stored.
        target = min(ids, key=lambda i: (ev.RANK.get(by_id[i]['importance'], 2), by_id[i]['created_at']))
        # Each receipt is bounded to 20 events, but no members are discarded.
        sources = [i for i in ids if i != target]
        for offset in range(0, len(sources), 19):
            chunk = [target, *sources[offset:offset + 19]]
            with closing(store._connect()) as db:
                heads = dict(db.execute('SELECT event_id, revision_id FROM studio_event_revision_heads WHERE event_id IN '
                                        f'({",".join("?" * len(chunk))})', chunk).fetchall())
            if len(heads) != len(chunk):
                complete = False
                continue
            try:
                store.event_revisions.merge(execution, target, heads)
            except ValueError:
                complete = False  # stale/withdrawn members remain eligible for a later review
                continue
            folded += len(heads) - 1
    if complete:
        store.set_state(key, str(folded))
    return folded
