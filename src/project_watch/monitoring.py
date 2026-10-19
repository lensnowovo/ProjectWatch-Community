"""Deterministic monitoring runner; adapters return data but never mutate records."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from project_watch.domain import SignalInput, SourceRun
from project_watch.repository import NotFoundError, ProjectRepository


@dataclass(frozen=True, slots=True)
class FeedItem:
    external_id: str
    title: str
    url: str
    summary: str
    published_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class AdapterResult:
    items: tuple[FeedItem, ...] = ()
    unavailable_error: str | None = None
    retry_after_seconds: int | None = None


class MonitoringAdapter(Protocol):
    def fetch(self, target: str) -> AdapterResult: ...


class ManualSnapshotAdapter:
    """Offline adapter for explicit, synthetic snapshots; never reads the network."""

    def __init__(self, snapshots: dict[str, tuple[FeedItem, ...]]) -> None:
        self.snapshots = dict(snapshots)

    def fetch(self, target: str) -> AdapterResult:
        items = self.snapshots.get(target)
        if items is None:
            return AdapterResult(unavailable_error="snapshot_missing")
        return AdapterResult(items=items)


def items_to_signals(items: tuple[FeedItem, ...]) -> tuple[SignalInput, ...]:
    """Classify freshness identically in the local and team runners."""
    now = datetime.now(UTC)
    return tuple(
        SignalInput(
            external_id=item.external_id,
            title=item.title,
            url=item.url,
            summary=item.summary,
            published_at=item.published_at,
            freshness=(
                "unknown"
                if item.published_at is None or item.published_at.astimezone(UTC) > now + timedelta(days=1)
                else "historical"
                if item.published_at.astimezone(UTC) < now - timedelta(days=30)
                else "current"
            ),
        )
        for item in items
    )


def select_project_items(items: tuple[FeedItem, ...], project_name: str) -> tuple[FeedItem, ...]:
    """Keep literal project-name mentions without asserting entity identity."""
    term = " ".join(unicodedata.normalize("NFKC", project_name).casefold().split())
    if len(term.replace(" ", "")) < 2:
        return ()
    left = r"(?<![a-z0-9])" if term[0].isascii() and term[0].isalnum() else ""
    right = r"(?![a-z0-9])" if term[-1].isascii() and term[-1].isalnum() else ""
    pattern = re.compile(left + re.escape(term) + right)

    def contains_name(value: str) -> bool:
        text = " ".join(unicodedata.normalize("NFKC", value).casefold().split())
        return pattern.search(text) is not None

    return tuple(item for item in items if contains_name(item.title) or contains_name(item.summary))


class MonitoringRunner:
    def __init__(
        self, repository: ProjectRepository, adapters: dict[str, MonitoringAdapter], *, source_router=None
    ) -> None:
        self.repository = repository
        self.adapters = adapters
        self.source_router = source_router

    def run(self, plan_id: str, *, idempotency_key: str) -> SourceRun:
        existing = self.repository.get_source_run_by_key(plan_id, idempotency_key)
        if existing is not None:
            return existing
        plan = self.repository.get_monitoring_plan(plan_id)
        if plan is None:
            raise NotFoundError("monitoring plan not found")
        from project_watch.external_scopes import CONSENT_VERSION, ExternalFocusStore, public_query

        focus = ExternalFocusStore(self.repository).get(plan.id)
        if focus and (
            not plan.enabled or focus['consent_version'] != CONSENT_VERSION
            or public_query(focus['category'], focus['term'], focus['provider'])['target'] != plan.target
        ):
            return self.repository.record_source_run(
                plan_id, status='not_run', error_code=None, idempotency_key=idempotency_key
            )
        adapter = self.adapters.get(plan.kind)
        if adapter is None:
            return self.repository.record_source_run(
                plan_id, status="not_run", error_code=None, idempotency_key=idempotency_key
            )
        try:
            result = (self.source_router.collect(plan, idempotency_key, adapter)
                      if self.source_router is not None else adapter.fetch(plan.target))
        except Exception:
            # Adapter errors are recorded without leaking URL or provider details to clients.
            result = AdapterResult(unavailable_error="adapter_error")
        if result.unavailable_error:
            return self.repository.record_source_run(
                plan_id,
                status="rate_limited"
                if result.unavailable_error in {"rate_limited", "quota_exhausted"}
                else "unreachable",
                error_code=result.unavailable_error,
                idempotency_key=idempotency_key,
            )
        if not result.items:
            return self.repository.record_source_run(
                plan_id, status="no_results", error_code=None, idempotency_key=idempotency_key
            )
        project = self.repository.get_project(plan.project_id)
        if project is None:
            raise NotFoundError("monitoring project not found")
        matched = select_project_items(result.items, focus['term'] if focus else project.name)
        return self.repository.record_source_run(
            plan_id,
            status="completed",
            error_code=None,
            idempotency_key=idempotency_key,
            signals=items_to_signals(matched),
            fetched_count=len(result.items),
        )
