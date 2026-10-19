"""Convert cited extraction results without upgrading subject/semantic confidence."""

from project_watch.studio.monitoring_contracts import FactInput, LocatedRef


def ingest_metrics(project_id: str, metrics: list[dict], markers: dict, objects, evidence) -> int:
    inserted = 0
    project = objects.store.get_project(project_id) or {}
    names = {str(project.get(k) or "").strip() for k in ("name", "legal_name", "short_name")}
    names.discard("")
    for metric in metrics:
        quote = metric.get("quote") or ""
        if not quote:
            continue
        candidates = [r for r in markers.values() if r["source_id"] == metric["source_id"]
                      and r["locator"] == metric["locator"] and r.get("evidence_version_id")]
        ref = None
        for candidate in candidates:
            record = evidence.get(project_id, candidate["evidence_version_id"])
            part = record["parts"][candidate["part_index"]]
            start = part["text"].find(quote)
            if start >= 0:
                ref = LocatedRef(evidence_version_id=record["id"], part_index=candidate["part_index"],
                                 start=start, end=start + len(quote), quote=quote)
                break
        if ref is None:
            continue
        # Materials may describe customers, subsidiaries or industry benchmarks.
        # No explicit subject -> an unassigned document subject, never silently
        # attributed to the monitored company or aggregated to its parent.
        subject_name = str(metric.get("subject") or "").strip()
        explicit = subject_name in names and subject_name in ref.quote
        subject = objects.ensure_subject(project_id) if explicit else objects.unassigned_document_subject(
            project_id, metric["source_id"])
        objects.save_fact(project_id, FactInput(subject_id=subject["id"], predicate=metric.get("std_name") or metric["name"],
            text=quote, value=str(metric["value"]), unit=metric.get("unit") or "", period=metric.get("period") or "",
            currency=metric.get("currency") or "", basis=metric.get("basis") or "unknown",
            consolidation_scope=metric.get("scope") or "unknown", subject_attribution="explicit" if explicit else "unknown",
            refs=[ref]))
        inserted += 1
    return inserted
