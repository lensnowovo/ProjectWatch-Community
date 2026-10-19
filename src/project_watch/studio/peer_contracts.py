"""Versioned public-source company discovery; candidates are not assessments."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Candidate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=2, max_length=200)
    source: str = Field(min_length=1, max_length=10)
    quote: str = Field(min_length=6, max_length=6000)


class Discovery(BaseModel):
    model_config = ConfigDict(extra='forbid')
    schema_version: Literal[1] = 1
    candidates: list[Candidate] = Field(max_length=12)

    @field_validator('schema_version', mode='before')
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            raise ValueError('schema_version must be an integer')
        return value
