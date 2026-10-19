"""Small shared rules for human-reviewed, source-bound follow-ups."""

from __future__ import annotations

from datetime import date
from typing import Any

from project_watch.domain import Observation, SourceVersion, WatchItem


def watch_item_input(
    title: str, question: str, owner: str | None, due_on: date | None
) -> tuple[str, str, str, str | None, str | None]:
    title = " ".join(title.split())
    question = question.strip()
    owner = owner.strip() if owner else None
    if not 1 <= len(title) <= 200:
        raise ValueError("watch title must contain 1-200 characters")
    if not 1 <= len(question) <= 1000:
        raise ValueError("watch question must contain 1-1000 characters")
    if owner is not None and not 1 <= len(owner) <= 128:
        raise ValueError("watch owner must contain 1-128 characters")
    return title, title.casefold(), question, owner, due_on.isoformat() if due_on else None


def watch_review_input(action: str, reviewer: str, note: str | None) -> tuple[str, str, str | None]:
    status = {"watch": "watching", "require_action": "action_required", "reject": "rejected"}.get(
        action
    )
    if status is None:
        raise ValueError("invalid watch review action")
    reviewer = reviewer.strip()
    note = note.strip() if note else None
    if not 1 <= len(reviewer) <= 200:
        raise ValueError("watch reviewer must contain 1-200 characters")
    if note is not None and not 1 <= len(note) <= 500:
        raise ValueError("watch review note must contain 1-500 characters")
    return status, reviewer, note


def watch_change_input(
    target_status: str,
    question: str,
    owner: str | None,
    due_on: date | None,
    reason: str,
) -> tuple[str, str, str | None, str | None, str]:
    if target_status not in {"watching", "action_required", "resolved"}:
        raise ValueError("invalid watch change target status")
    question = question.strip()
    owner = owner.strip() if owner else None
    reason = reason.strip()
    if not 1 <= len(question) <= 1000:
        raise ValueError("watch question must contain 1-1000 characters")
    if owner is not None and not 1 <= len(owner) <= 128:
        raise ValueError("watch owner must contain 1-128 characters")
    if not 5 <= len(reason) <= 500:
        raise ValueError("watch change reason must contain 5-500 characters")
    return target_status, question, owner, due_on.isoformat() if due_on else None, reason


def watch_change_review_input(
    action: str, reviewer: str, note: str | None
) -> tuple[str, str, str | None]:
    if action not in {"accept", "reject"}:
        raise ValueError("invalid watch change review action")
    reviewer = reviewer.strip()
    note = note.strip() if note else None
    if not 1 <= len(reviewer) <= 200:
        raise ValueError("watch reviewer must contain 1-200 characters")
    if note is not None and not 1 <= len(note) <= 500:
        raise ValueError("watch review note must contain 1-500 characters")
    return action, reviewer, note


def watch_item_snapshot(
    item: WatchItem, observations: dict[str, Observation], sources: dict[str, SourceVersion]
) -> dict[str, Any]:
    """Keep the reviewed task next to its independently reviewed source record."""
    observation = observations[item.current_observation_id]
    source = sources[observation.source_id]
    if (
        item.project_id != observation.project_id
        or source.project_id != item.project_id
        or observation.status != "confirmed"
        or not observation.source_locator
    ):
        raise ValueError("watch item evidence is unavailable or no longer confirmed")
    return {
        "id": item.id,
        "project_id": item.project_id,
        "observation_id": item.observation_id,
        "current_observation_id": item.current_observation_id,
        "observation_version": observation.version,
        "title": item.title,
        "question": item.question,
        "owner": item.owner,
        "due_on": item.due_on,
        "status": "resolved" if item.resolved_at else item.status,
        "version": item.version,
        "proposed_by": item.proposed_by,
        "reviewed_by": item.reviewed_by,
        "review_note": item.review_note,
        "initial_question": item.initial_question,
        "initial_owner": item.initial_owner,
        "initial_due_on": item.initial_due_on,
        "resolved_at": item.resolved_at,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "source_id": source.id,
        "source_reference": source.reference,
        "source_sha256": source.content_sha256,
        "source_locator": observation.source_locator,
    }
