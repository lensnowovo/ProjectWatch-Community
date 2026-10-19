"""Portable, bounded Agent output contract; no model implementation or provider I/O."""

import ipaddress
from datetime import date
from typing import Annotated, Literal
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_serializer, model_validator


class Record(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ResultSource(Record):
    id: str = Field(min_length=1, max_length=60)
    kind: Literal['public', 'internal']
    title: str = Field(min_length=1, max_length=300)
    url: str = Field(default='', max_length=2000)
    source_id: str = Field(default='', max_length=100)
    locator: str = Field(default='', max_length=500)
    text: str = Field(default='', max_length=20000)
    read_scope: Literal['summary', 'partial', 'full'] = 'summary'
    published_at: date | None = None

    @model_validator(mode='after')
    def source_boundary(self):
        if self.kind == 'internal':
            if not self.source_id or not self.locator or self.url or self.text:
                raise ValueError('internal sources require stored source_id/locator, not supplied text or URL')
        else:
            parts = urlsplit(self.url)
            host = (parts.hostname or '').lower()
            if parts.scheme not in ('https', 'http') or not host or parts.username or parts.password:
                raise ValueError('public sources require a safe public http(s) URL')
            try:
                private = not ipaddress.ip_address(host).is_global
            except ValueError:
                private = host == 'localhost' or host.endswith(('.localhost', '.local', '.internal'))
            if private or any(k.lower() in ('api_key', 'apikey', 'token', 'access_token', 'key') for k in parse_qs(parts.query)):
                raise ValueError('private hosts or credential-bearing URLs are not public evidence')
            if self.source_id or self.locator:
                raise ValueError('public sources cannot claim private source locators')
        return self


class Claim(Record):
    text: str = Field(min_length=1, max_length=600)
    refs: list[str] = Field(min_length=1, max_length=8)


class CompanySection(Record):
    title: str = Field(min_length=1, max_length=60)
    points: list[Claim] = Field(min_length=1, max_length=12)


class CompanyResult(Record):
    one_liner: Claim
    summary: Claim
    sections: list[CompanySection] = Field(default_factory=list, max_length=12)
    highlights: list[Claim] = Field(default_factory=list, max_length=4)
    questions: list[str] = Field(default_factory=list, max_length=8)


class BriefResult(Record):
    headline: str = Field(min_length=1, max_length=160)
    tone: Literal['positive', 'neutral', 'watch', 'negative'] = 'neutral'
    summary: Claim
    watch: list[Claim] = Field(default_factory=list, max_length=8)


class NewsResult(Record):
    key: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=300)
    summary: str = Field(min_length=1, max_length=600)
    impact: str = Field(default='', max_length=600)
    category: Literal['company', 'competitor', 'industry', 'policy', 'paper', 'trial', 'patent']
    importance: Literal['major', 'notable', 'routine'] = 'notable'
    event_at: date | None = None
    refs: list[str] = Field(min_length=1, max_length=8)


class PositionResult(Record):
    verdict: Literal['leading', 'ahead', 'on_par', 'behind', 'lagging', 'unclear'] = 'unclear'
    confidence: Literal['high', 'medium', 'low'] = 'low'
    summary: str = Field(default='', max_length=300)
    supporting: list[Claim] = Field(default_factory=list, max_length=6)
    against: list[Claim] = Field(default_factory=list, max_length=6)
    peers: list[str] = Field(default_factory=list, max_length=8)
    change_if: list[str] = Field(default_factory=list, max_length=4)


class TechnologyResult(Record):
    route: str = Field(default='', max_length=100)
    maturity: Literal['frontier', 'advanced', 'mainstream', 'mature', 'dated', 'unclear'] = 'unclear'
    consensus: Literal['consensus', 'forming', 'contrarian', 'unclear'] = 'unclear'
    confidence: Literal['high', 'medium', 'low'] = 'low'
    summary: str = Field(default='', max_length=300)
    academic: list[Claim] = Field(default_factory=list, max_length=6)
    industry: list[Claim] = Field(default_factory=list, max_length=6)
    risks: list[Claim] = Field(default_factory=list, max_length=6)


class IndustryResult(Record):
    verdict: Literal['expanding', 'growing', 'stable', 'consolidating', 'cooling', 'unclear'] = 'unclear'
    confidence: Literal['high', 'medium', 'low'] = 'low'
    summary: str = Field(default='', max_length=300)
    drivers: list[Claim] = Field(default_factory=list, max_length=6)
    headwinds: list[Claim] = Field(default_factory=list, max_length=6)


class RingResult(Record):
    confidence: Literal['high', 'medium', 'low'] = 'low'
    summary: str = Field(default='', max_length=300)
    supporting: list[Claim] = Field(default_factory=list, max_length=6)
    against: list[Claim] = Field(default_factory=list, max_length=6)
    change_if: list[str] = Field(default_factory=list, max_length=4)


class OperationsResult(RingResult):
    verdict: Literal['improving', 'steady', 'pressured', 'unclear'] = 'unclear'


class ChainResult(RingResult):
    verdict: Literal['favorable', 'steady', 'pressured', 'unclear'] = 'unclear'


class DeepResult(Record):
    headline: str = Field(min_length=1, max_length=160)
    position: PositionResult = Field(default_factory=PositionResult)
    technology: TechnologyResult = Field(default_factory=TechnologyResult)
    industry: IndustryResult = Field(default_factory=IndustryResult)
    gaps: list[str] = Field(default_factory=list, max_length=8)
    # Omit absent extensions to preserve old payload hashes/idempotency receipts.
    operations: OperationsResult | None = None
    chain: ChainResult | None = None

    @model_serializer(mode='wrap')
    def omit_absent_rings(self, handler):
        # Supported across our declared Pydantic >=2.9 range (exclude_if is newer).
        result = handler(self)
        for key in ('operations', 'chain'):
            if result.get(key) is None:
                result.pop(key, None)
        return result


class MonitoringResults(Record):
    expected_version: Annotated[StrictInt, Field(ge=1)]
    idempotency_key: str = Field(min_length=1, max_length=100)
    producer: str = Field(min_length=1, max_length=60)
    sources: list[ResultSource] = Field(min_length=1, max_length=40)
    news: list[NewsResult] = Field(default_factory=list, max_length=40)
    company: CompanyResult | None = None
    brief: BriefResult | None = None
    deep: DeepResult | None = None

    @model_validator(mode='after')
    def known_references(self):
        ids = {s.id for s in self.sources}
        if len(ids) != len(self.sources) or len({n.key for n in self.news}) != len(self.news):
            raise ValueError('source IDs and news keys must be unique')
        def walk(value):
            if isinstance(value, dict):
                if 'refs' in value and any(ref not in ids for ref in value['refs']):
                    raise ValueError('unknown source reference')
                for item in value.values():
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)
        walk(self.model_dump(exclude={'sources'}))
        public = {s.id for s in self.sources if s.kind == 'public'}
        if any(not set(n.refs) <= public for n in self.news):
            raise ValueError('news can cite only public sources; internal analysis stays private')
        if not (self.news or self.company or self.brief or self.deep):
            raise ValueError('a result bundle must include an output')
        if len(self.model_dump_json().encode()) > 512000:
            raise ValueError('result bundle exceeds 512 KiB')
        return self
