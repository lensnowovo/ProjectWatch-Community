"""Bounded read-only projections over shared attributed facts."""

import json
from contextlib import closing

from project_watch.studio.business_relations import project_relation
from project_watch.studio.commitments import project_commitment
from project_watch.studio.event_state import project_event
from project_watch.studio.funding_facts import project_funding
from project_watch.studio.milestones import project_milestone
from project_watch.studio.policy_applicability import project_policy


def read(service, project_id, *, include_internal=False, limit=50, after='', kind=None):
    if not 1 <= limit <= 100 or len(after) > 100:
        raise ValueError('invalid observation bounds')
    if kind not in (None, 'business_relation', 'policy', 'event', 'milestone', 'funding_fact', 'commitment'):
        raise ValueError('invalid observation kind')
    service.project(project_id) if hasattr(service, 'project') else service.store.get_project(project_id)
    with closing(service.store._connect()) as db:
        rows = db.execute("SELECT record FROM studio_fact_records WHERE project_id=? AND id>? "
                          "AND (? OR visibility='public') AND json_extract(record,'$.attributes.kind') IS NOT NULL "
                          "AND (? IS NULL OR json_extract(record,'$.attributes.kind')=?) "
                          "ORDER BY id LIMIT ?", (project_id, after, include_internal, kind, kind, limit + 1)).fetchall()
    items = []
    for row in rows[:limit]:
        fact = json.loads(row[0])
        try:
            projector = {'business_relation': project_relation, 'policy': project_policy, 'event': project_event,
                         'milestone': project_milestone, 'funding_fact': project_funding,
                         'commitment': project_commitment}[fact['attributes']['kind']]
            result = projector(service, project_id, fact, include_internal=include_internal)
            items.append({'kind': fact['attributes']['kind'], **result})
        except KeyError:
            continue  # A changed visibility scope must not leak dependent identities.
    return {'project_id': project_id, 'schema_version': 1, 'items': items,
            'next_cursor': json.loads(rows[limit-1][0])['id'] if len(rows) > limit else None,
            'truncated': len(rows) > limit, 'investment_judgment': False}
