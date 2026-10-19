"""Read-only, attributed excerpts. Never promote extracts to reviewed facts.

This projection reuses saved observations; it performs no model calls and does not
claim to have compiled a complete company report from all uploaded documents.
"""

from collections import defaultdict

from project_watch.company_state import CompanyState
from project_watch.domain import RESEARCH_TOPICS
from project_watch.reading_updates import reading_updates


def can_display_reading_item(item, *, project_id, allowed_source_ids):
    refs = item.get("source_refs") or []
    return bool(
        item.get("project_id") == project_id
        and item.get("text", "").strip()
        and item.get("review_status") in {"unreviewed", "confirmed"}
        and not item.get("is_stale")
        and refs
        and all(
            r.get("source_id") in allowed_source_ids and r.get("locator") and r.get("version")
            for r in refs
        )
    )


def build_reading(project_id, sources, observations):
    sources = {s.id: s for s in sources if s.project_id == project_id}
    groups = defaultdict(list)
    amounts = defaultdict(set)
    valid = [
        o
        for o in observations
        if o.project_id == project_id
        and o.status != "rejected"
        and o.source_id in sources
        and o.source_locator.strip()
    ]
    for o in valid:
        if o.number is not None:
            amounts[(o.subject, ' '.join(o.statement.split()), o.unit, o.period, o.scope, o.basis)].add(o.number)
    for o in sorted(valid, key=lambda o: (o.status != "confirmed", o.created_at, o.id)):
        source = sources[o.source_id]
        item = {
            "id": o.id,
            "project_id": project_id,
            "text": o.statement,
            "origin": "saved_excerpt",  # Producer was not recorded; do not invent AI/company attribution.
            "review_status": "confirmed" if o.status == "confirmed" else "unreviewed",
            "source_refs": [
                {
                    "source_id": source.id,
                    "reference": source.reference,
                    "locator": o.source_locator,
                    "version": source.content_sha256,
                }
            ],
            "as_of": o.period,
            "recorded_at": o.created_at.isoformat(),
            "is_stale": False,
            "has_conflict": o.number is not None
            and len(amounts[(o.subject, ' '.join(o.statement.split()), o.unit, o.period, o.scope, o.basis)]) > 1,
            "number": str(o.number) if o.number is not None else None,
            "unit": o.unit,
            "scope": o.scope,
            "basis": o.basis,
        }
        if can_display_reading_item(item, project_id=project_id, allowed_source_ids=set(sources)):
            groups[o.subject].append(item)
    sections = [
        {"title": topic, "items": groups[topic]}
        for topic in [*RESEARCH_TOPICS, *sorted(set(groups) - set(RESEARCH_TOPICS))]
        if groups[topic]
    ]
    # One excerpt per business topic, reviewed excerpts first. Not the arbitrary
    # first observation (which could be financial, a risk, or an unrelated metric).
    choices = [
        next((i for i in groups[t] if i["number"] is None), None)
        for t in ("公司与团队", "产品与技术", "经营情况")
    ]
    choices = [i for i in choices if i]
    reviewed = [i for i in choices if i["review_status"] == "confirmed"]
    summary_items = reviewed or choices
    summary = (
        {
            "text": " ".join(i["text"] for i in summary_items),
            "review_status": "confirmed" if reviewed else "unreviewed",
            "source_refs": [r for i in summary_items for r in i["source_refs"]],
        }
        if summary_items
        else None
    )
    return {
        "project_id": project_id,
        "sections": sections,
        "summary": summary,
        "coverage": {
            "sources_total": len(sources),
            "sources_with_excerpts": len({o.source_id for o in valid}),
        },
        "mode": "saved_excerpts",
        "rule_version": "reading-v1",
    }


def project_reading(repo, project_id):
    sources = repo.list_sources(project_id)
    result = build_reading(
        project_id, sources, repo.list_observations(project_id)
    )
    result["compiled"] = CompanyState(repo, ensure=False).read(project_id)
    result['updates'] = reading_updates(project_id, sources, result['compiled'])
    return result
