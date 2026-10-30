"""Default-off input boundary for a separately approved patent registry adapter.

No provider is configured, no network or scheduler registration is supplied.
Activation requires a verified provider mapping, rights, jurisdictions and bounded
pagination. A web-search hit or Europe PMC record does not satisfy this contract.
"""
from datetime import date
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from project_watch.studio.http import HttpError

CONTRACT = 'patent-registry-record-v1'


class PatentRecord(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    contract: Literal['patent-registry-record-v1']
    registry: str = Field(min_length=1, max_length=120)
    publication_number: str = Field(min_length=4, max_length=80, pattern=r'^[A-Z]{2}[A-Z0-9.-]+$')
    title: str = Field(min_length=6, max_length=200)
    url: str = Field(min_length=10, max_length=2000)
    applicants: list[str] = Field(min_length=1, max_length=20)
    inventors: list[str] = Field(default_factory=list, max_length=40)
    status: Literal['application', 'publication', 'grant', 'unknown']
    event_date: str | None = None
    date_basis: Literal['application', 'publication', 'grant', 'unknown']
    status_quote: str = Field(min_length=6, max_length=1500)
    original_text: str = Field(min_length=6, max_length=20000)

    @field_validator('event_date')
    @classmethod
    def iso_date(cls, value):
        if value is not None and date.fromisoformat(value).isoformat() != value:
            raise ValueError('explicit ISO date required')
        return value

    @model_validator(mode='after')
    def exact_source(self):
        url = urlsplit(self.url)
        if url.scheme != 'https' or not url.hostname or url.username or url.password:
            raise ValueError('public registry URL required')
        for value in [self.publication_number, self.title, self.status_quote, *self.applicants, *self.inventors]:
            if not isinstance(value, str) or not value.strip() or len(value) > 1500 or value not in self.original_text:
                raise ValueError('registry field lacks verbatim evidence')
        if self.event_date and (self.date_basis != self.status or self.date_basis == 'unknown'
                                or self.event_date not in self.original_text):
            raise ValueError('patent date/status mismatch')
        return self


def normalize_page(data, *, enabled=False):
    """Offline adapter contract only. Default cannot dispatch or admit sources."""
    if enabled is not True:
        raise HttpError('patent_registry_not_configured')
    if not isinstance(data, list) or len(data) > 20:
        raise ValueError('bounded patent page required')
    records = [PatentRecord.model_validate(row) for row in data]
    return [dict(title=r.title, url=r.url, source=r.registry, snippet=r.original_text,
        published_at=r.event_date, extra=dict(
            source_identity='patent-registry:' + r.registry + ':' + r.publication_number,
            source_record=r.model_dump(), patent_number=r.publication_number, applicants=r.applicants,
            legal_status=r.status, date_basis=r.date_basis)) for r in records]
