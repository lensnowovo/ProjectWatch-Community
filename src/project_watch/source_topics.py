"""Source-located topic routing; no judgment generation or investment policy."""

from __future__ import annotations

from project_watch.domain import RESEARCH_TOPICS
from project_watch.native_intake import NativeCandidate


def review_topics(
    primary_topic: str | None, candidates: tuple[NativeCandidate, ...]
) -> tuple[str, ...]:
    """Use only the indexed topic and source-located candidate topics."""
    if primary_topic not in RESEARCH_TOPICS:
        return ()
    return tuple(
        dict.fromkeys(
            (
                primary_topic,
                *(item.subject for item in candidates if item.subject in RESEARCH_TOPICS),
            )
        )
    )


def impact_topic(observation_subject: str, matched_topic: str) -> str:
    """Keep the prompt grouped with the old judgment's domain when possible."""
    return observation_subject if observation_subject in RESEARCH_TOPICS else matched_topic


__all__ = ["impact_topic", "review_topics"]
