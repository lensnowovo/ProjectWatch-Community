"""Strict source-assertion attributes; no investment scores or inferred cash gaps."""

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Term = Annotated[str, Field(min_length=1, max_length=100)]


class Attributes(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)


class BusinessRelation(Attributes):
    kind: Literal['business_relation']
    peer_id: str = Field(min_length=1, max_length=100)
    role: Literal['customer', 'supplier', 'partner', 'pilot_partner', 'investor', 'unknown']
    stage: Literal['trial', 'order', 'delivery', 'acceptance', 'revenue', 'payment', 'strategic', 'announced', 'unknown'] = 'unknown'


class Policy(Attributes):
    kind: Literal['policy']
    name: str = Field(min_length=1, max_length=200)
    status: Literal['draft', 'in_force', 'expired', 'unknown'] = 'unknown'
    jurisdictions: tuple[Term, ...] = Field(default=(), max_length=10)
    applicable_to: tuple[Term, ...] = Field(default=(), max_length=10)
    effective_from: date | None = None
    effective_to: date | None = None


class Event(Attributes):
    kind: Literal['event']
    name: str = Field(min_length=1, max_length=200)
    channel: Literal['funding', 'commercial', 'regulatory', 'development', 'other']
    role: Literal['recipient', 'actor', 'mentioned', 'unknown'] = 'unknown'
    state: Literal['intent', 'agreement', 'received', 'signed', 'trial', 'delivered', 'accepted',
                   'recognized', 'paid', 'started', 'completed', 'approved',
                   'cancelled', 'delayed', 'withdrawn', 'paused', 'resumed', 'unknown']
    state_text: str = Field(min_length=1, max_length=100)

    @model_validator(mode='after')
    def channel_state(self):
        if (self.state in ('intent', 'received') and self.channel != 'funding') or (
                self.state == 'agreement' and self.channel not in ('funding', 'commercial')) or (
                self.state in ('signed', 'trial', 'delivered', 'accepted', 'recognized', 'paid') and self.channel != 'commercial'):
            raise ValueError('event stage is not valid for its channel')
        return self


class Milestone(Attributes):
    kind: Literal['milestone']
    name: str = Field(min_length=1, max_length=200)
    phase: Literal['original', 'adjusted', 'actual', 'delayed', 'cancelled']
    phase_text: str = Field(min_length=1, max_length=100)
    target_date: date | None = None


class Funding(Attributes):
    kind: Literal['funding_fact']
    measure: Literal['funding', 'cash_balance', 'operating_cash_flow', 'net_profit', 'planned_spend']
    stage: Literal['plan', 'agreement', 'received', 'balance', 'flow', 'profit', 'spend']
    stage_text: str = Field(min_length=1, max_length=100)
    milestone_name: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode='after')
    def measure_stage(self):
        valid = {'funding': ('plan', 'agreement', 'received'), 'cash_balance': ('balance',),
                 'operating_cash_flow': ('flow',), 'net_profit': ('profit',), 'planned_spend': ('spend',)}
        if self.stage not in valid[self.measure]:
            raise ValueError('financial stage does not match its measure')
        return self


class Commitment(Attributes):
    kind: Literal['commitment'] = 'commitment'
    statement: str = Field(min_length=6, max_length=80)
    target_kind: Literal['量产', '交付', '获批', '上市（IPO）', '融资', '投产', '营收目标', '其他']
    target_text: str = Field(min_length=1, max_length=100)
    due_text: str = Field(min_length=1, max_length=60)
    due_start: date
    due_end: date
    said_at: date
    speaker: str = Field(min_length=1, max_length=100)

    @model_validator(mode='after')
    def interval(self):
        if self.due_end < self.due_start:
            raise ValueError('commitment interval reversed')
        return self


FactAttributes = Annotated[BusinessRelation | Policy | Event | Milestone | Funding | Commitment, Field(discriminator='kind')]
