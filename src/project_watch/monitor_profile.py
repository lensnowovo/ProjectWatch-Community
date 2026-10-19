"""Local monitoring context, with explicit human overrides and attributed suggestions.

Preferences are not formal facts, identity verification or permission to send
private text to a search/model provider. GET builds a projection, never a model call.
"""

import hashlib
import json

from project_watch.monitor_profile_store import (
    PROFILE_FIELDS as PROFILE_FIELDS,
    ensure_profile_schema as ensure_profile_schema,
    validate_edit as validate_edit,
)
FIELD_SUBJECTS = {
    "business": ("公司业务", "公司与团队"),
    "stage": ("发展阶段",),
    "products": ("产品与技术", "产品"),
    "customers": ("客户", "客户情况"),
    "thesis": (),  # A saved excerpt must not become the investor's judgment.
}


def project_profile(repo, project_id):
    from project_watch.repository import NotFoundError

    project = repo.get_project(project_id)
    if project is None:
        raise NotFoundError("项目不存在。")
    sources = {s.id: s for s in repo.list_sources(project_id)}
    all_observations = {o.id: o for o in repo.list_observations(project_id)
                        if o.project_id == project_id and o.source_id in sources}
    observations = {
        o.id: o
        for o in all_observations.values()
        if o.project_id == project_id
        and o.status != "rejected"
        and o.source_id in sources
        and o.source_locator.strip()
    }

    def refs(o):
        source = sources[o.source_id]
        return [
            {
                "source_id": source.id,
                "reference": source.reference,
                "locator": o.source_locator,
                "version": source.content_sha256,
                "observation_id": o.id,
                "observation_version": o.version,
            }
        ]

    # These are visibly attributed saved descriptions, not invented AI profiles.
    # Full-document compilation feeds the later company-state path.
    ordered = sorted(observations.values(), key=lambda o: (o.created_at, o.id), reverse=True)
    suggestions = {}
    for key, subjects in FIELD_SUBJECTS.items():
        matches = [o for o in ordered if o.subject in subjects and o.number is None]
        reviewed = [o for o in matches if o.status == "confirmed"]
        chosen = next(iter(reviewed or matches), None)
        suggestions[key] = {
            "text": chosen.statement if chosen else "",
            "origin": "saved_excerpt" if chosen else "missing",
            "review_status": chosen.status if chosen else "unknown",
            "source_refs": refs(chosen) if chosen else [],
            "as_of": chosen.period if chosen else None,
        }
    suggested_focus = []
    watches = repo.list_watch_items(project_id)
    for w in watches:
        observation = observations.get(w.current_observation_id)
        if w.status == "rejected" or w.resolved_at or observation is None:
            continue
        suggested_focus.append(
            {
                "id": w.id,
                "text": w.question,
                "title": w.title,
                "origin": "watch_item",
                "status": w.status,
                "source_refs": refs(observation),
            }
        )
    from project_watch.company_reading_view import CompanyStateView
    compiled = CompanyStateView(repo).read(project_id)
    chosen = set()
    current_batches = [b for b in compiled["batches"] if b["source_id"] in sources
                       and sources[b["source_id"]].content_sha256 == b["source_version"]]
    for batch in sorted(current_batches, key=lambda b: sources[b["source_id"]].created_at, reverse=True):
        for item in batch["items"]:
            key = item["topic"]
            if key not in {"business", "stage", "products", "customers"} or key in chosen:
                continue
            chosen.add(key)
            if suggestions[key]["review_status"] != "confirmed":
                suggestions[key] = {"text": item["text"], "origin": "ai_synthesis", "review_status": "unreviewed",
                                    "source_refs": item["source_refs"], "as_of": item["as_of"]}
    # Reuse already authorized, compiled concerns. Do not run another model or
    # manufacture formal watch records. Existing watch decisions own their source
    # locations, including rejected/resolved ones, and must not reappear via AI.
    watch_ids = {w.id for w in watches}
    owned_observations = {oid for w in watches for oid in (w.observation_id, w.current_observation_id)}
    if watches:
        owned_observations.update(c.evidence_observation_id for c in repo.list_watch_item_changes(project_id)
                                  if c.watch_item_id in watch_ids and c.status == 'accepted')
    covered = {(o.source_id, o.source_locator) for oid in owned_observations
               if (o := all_observations.get(oid)) is not None}
    suggested_focus.extend(compiled_focus(current_batches, sources, covered))
    history = repo.monitor_profile_history(project_id)
    latest = history[-1] if history else {"version": 0, "fields": {}, "focus": None}
    fields = {k: dict(v) for k, v in suggestions.items()}
    for key, value in latest["fields"].items():
        fields[key] = {
            "text": value,
            "origin": "human",
            "review_status": "personal_note",
            "source_refs": [],
            "as_of": None,
        }
    focus = suggested_focus
    if latest["focus"] is not None:
        focus = [{"text": t, "origin": "human", "source_refs": []} for t in latest["focus"]]
    peers = [
        {"id": p.id, "name": p.name, "status": p.status}
        for p in repo.list_competitive_peers(project_id)
        if p.status == "confirmed"
    ]
    fingerprint = json.dumps(
        {"suggestions": suggestions, "focus": suggested_focus, "peers": peers},
        sort_keys=True,
        ensure_ascii=False,
    )
    return {
        "project_id": project_id,
        "name": project.name,
        "identity_status": project.identity_status,
        "version": latest["version"],
        "fields": fields,
        "suggestions": suggestions,
        "focus": focus,
        "suggested_focus": suggested_focus,
        "focus_is_manual": latest["focus"] is not None,
        "peers": peers,
        "missing": [k for k in PROFILE_FIELDS[:-1] if not fields[k]["text"]],
        "input_revision": hashlib.sha256(fingerprint.encode()).hexdigest(),
        "rule_version": "monitor-profile-v2",
        "formal_facts": False,
    }


def compiled_focus(batches, sources, covered):
    """At most five distinct cited concerns, ordered by material arrival (not risk score)."""
    result = {}
    for batch in sorted(batches, key=lambda b: sources[b['source_id']].created_at, reverse=True):
        for item in batch['items']:
            if item['topic'] not in {'risks', 'milestones'}:
                continue
            if any((r['source_id'], r['locator']) in covered for r in item['source_refs']):
                continue
            key = item['text'].strip()
            if key in result:
                known = result[key]['source_refs']
                for ref in item['source_refs']:
                    if ref not in known:
                        known.append(dict(ref))
                continue
            if len(result) >= 5:
                continue
            result[key] = {
                'id': 'compiled-' + hashlib.sha256(key.encode()).hexdigest()[:20],
                'text': key if len(key) <= 300 else item['headline'],
                'detail': key, 'title': item['headline'], 'topic': item['topic'],
                'origin': 'ai_synthesis', 'review_status': 'unreviewed',
                'status': 'suggested', 'as_of': item['as_of'],
                'source_refs': [dict(ref) for ref in item['source_refs']],
                'support_status': item['support_status'],
            }
    return list(result.values())
