"""Bounded external Agent inputs; no arbitrary method or SQL payloads."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from project_watch.studio.monitoring_contracts import ObjectInput


class GrantInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    permission: Literal["read", "write"]
    label: str = Field(min_length=1, max_length=100)
    hours: Annotated[StrictInt, Field(ge=1, le=720)] = 24
    allow_internal: StrictBool = False


class StatusInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["active", "paused"]
    expected_version: Annotated[StrictInt, Field(ge=1)]
    idempotency_key: str = Field(min_length=1, max_length=100)


class ObjectMutation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    object: ObjectInput
    object_id: str | None = Field(default=None, min_length=1, max_length=100)
    expected_object_version: Annotated[StrictInt, Field(ge=1)] | None = None
    expected_project_version: Annotated[StrictInt, Field(ge=1)]
    idempotency_key: str = Field(min_length=1, max_length=100)


class SourceAuthorization(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: Annotated[StrictInt, Field(ge=1)]
    idempotency_key: str = Field(min_length=1, max_length=100)


class TextSubmission(SourceAuthorization):
    filename: str = Field(min_length=1, max_length=255)
    text: str = Field(min_length=1, max_length=100000)


class ScopeFields(BaseModel):
    model_config = ConfigDict(extra='forbid')
    text: str = Field(min_length=2, max_length=200)
    queries: list[str] = Field(default_factory=list, max_length=4)
    axis: str = Field(default='asset', max_length=40)
    reason: str = Field(default='', max_length=600)
    criteria: str = Field(default='', max_length=600)
    exit_rule: str = Field(default='', max_length=600)
    tier: Literal['core', 'topic', 'background'] = 'core'
    status: Literal['active', 'trial', 'reduced', 'retired'] = 'active'


class ScopeWrite(SourceAuthorization):
    expected_scope_version: str = Field(min_length=64, max_length=64)
    watch_id: str | None = Field(default=None, min_length=1, max_length=100)
    item: ScopeFields
