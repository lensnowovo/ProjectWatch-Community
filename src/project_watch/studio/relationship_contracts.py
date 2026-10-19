"""Scoped user corrections; manual relationships are not verified research."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator


class RelationshipScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    subject_id: str = Field(min_length=1, max_length=100)
    peer_id: str = Field(min_length=1, max_length=100)
    subject_product_id: str | None = Field(default=None, min_length=1, max_length=100)
    peer_product_id: str | None = Field(default=None, min_length=1, max_length=100)


class RelationshipDecision(RelationshipScope):
    decision: Literal["include", "exclude", "reset"]
    relation_type: Literal["direct", "substitute", "adjacent", "unknown", "unrelated"] = "unknown"
    reason: str = Field(min_length=1, max_length=500)


class RelationshipCorrection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    expected_version: Annotated[StrictInt, Field(ge=1)]
    idempotency_key: str = Field(min_length=1, max_length=100)
    changes: tuple[RelationshipDecision, ...] = Field(min_length=1, max_length=20)


class PeerSelection(BaseModel):
    """Owner-facing name selection; no product research or search prerequisites."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1, max_length=200)
    peer_id: str | None = Field(default=None, min_length=1, max_length=100)
    decision: Literal["include", "exclude", "reset"]
    expected_version: Annotated[StrictInt, Field(ge=1)]
    idempotency_key: str = Field(min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def trimmed_name(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("company name is required")
        return value
