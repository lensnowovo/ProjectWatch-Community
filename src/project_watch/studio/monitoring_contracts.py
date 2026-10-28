"""Versioned monitoring data, not model-authored summaries masquerading as quotes."""

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from project_watch.studio.detail_contracts import FactAttributes


class EvidencePart(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    locator_kind: Literal["fragment", "pdf_page", "slide", "cell_range", "stored_text"]
    locator: str = Field(min_length=1, max_length=500)
    text: str = Field(min_length=1, max_length=20000)


class EvidenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    project_id: str = Field(min_length=1, max_length=100)
    source_key: str = Field(min_length=1, max_length=500)
    extraction_version: str = Field(min_length=1, max_length=100)
    source_type: Literal["document", "public_page", "search_summary", "structured_record"] = "document"
    visibility: Literal["internal", "public"] = "internal"
    origin: Literal["company_self", "regulator", "partner", "media", "unknown"] = "unknown"
    read_scope: Literal["full", "partial", "fragmented", "summary"]
    internal_source_id: str | None = Field(default=None, max_length=100)
    raw_content_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    propagation_family: str = Field(default="", max_length=100)
    family_basis: Literal["exact_text", "source_only", "unknown"] = "unknown"
    parts: tuple[EvidencePart, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def authentic_scope(self):
        if self.source_type == "search_summary" and (
            self.read_scope != "summary" or any(p.locator_kind != "stored_text" for p in self.parts)
        ):
            raise ValueError("search summaries only have stored-text locators and summary scope")
        if self.source_type == "public_page" and any(p.locator_kind != "stored_text" for p in self.parts):
            raise ValueError("unstructured public pages cannot claim PDF/slide/cell coordinates")
        if self.source_type == "structured_record" and (
            self.visibility != "public" or self.read_scope != "partial"
            or any(p.locator_kind != "stored_text" for p in self.parts)
        ):
            raise ValueError("structured records retain public source fields, not full page text")
        return self


class LocatedRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    evidence_version_id: str = Field(min_length=1, max_length=100)
    part_index: Annotated[StrictInt, Field(ge=0)]
    start: Annotated[StrictInt, Field(ge=0)]
    end: Annotated[StrictInt, Field(gt=0)]
    quote: str = Field(min_length=1, max_length=20000)

    @model_validator(mode="after")
    def offsets(self):
        if self.end <= self.start:
            raise ValueError("quote end must follow start")
        return self


class Alias(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1, max_length=200)
    visibility: Literal["internal", "public"] = "internal"
    valid_from: date | None = None
    valid_to: date | None = None

    @model_validator(mode="after")
    def interval(self):
        if self.valid_from and self.valid_to and self.valid_to < self.valid_from:
            raise ValueError("alias validity interval is reversed")
        return self


class ObjectInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["company", "product", "person", "regulatory_code"]
    name: str = Field(min_length=1, max_length=200)
    purpose: Literal["subject", "related", "unknown"] = "unknown"
    visibility: Literal["internal", "public"] = "internal"
    website: str = Field(default="", max_length=500)
    jurisdiction: str = Field(default="", max_length=100)
    registry_id: str = Field(default="", max_length=100)
    registration_code: str = Field(default="", max_length=100)
    parent_id: str | None = Field(default=None, max_length=100)
    aliases: tuple[Alias, ...] = Field(default=(), max_length=30)
    refs: tuple[LocatedRef, ...] = Field(default=(), max_length=20)


class FactInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    subject_id: str = Field(min_length=1, max_length=100)
    predicate: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=2000)
    value: str | None = Field(default=None, max_length=100)
    unit: str = Field(default="", max_length=50)
    period: str = Field(default="", max_length=50)
    currency: str = Field(default="", max_length=10)
    consolidation_scope: str = Field(default="unknown", max_length=100)
    subject_attribution: Literal["explicit", "unknown"] = "unknown"
    basis: Literal["actual", "forecast", "budget", "unknown"] = "unknown"
    visibility: Literal["internal", "public"] = "internal"
    occurred_at: date | None = None
    disclosed_at: date | None = None
    valid_from: date | None = None
    valid_to: date | None = None
    refs: tuple[LocatedRef, ...] = Field(min_length=1, max_length=20)
    attributes: FactAttributes | None = None

    @model_validator(mode='after')
    def attribute_predicate(self):
        if self.attributes is not None and self.predicate != self.attributes.kind:
            raise ValueError('typed attributes must match their fact predicate')
        return self
