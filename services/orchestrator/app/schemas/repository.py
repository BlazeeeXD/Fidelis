"""Contracts for repository learning and convention storage."""
from pydantic import BaseModel, ConfigDict, Field


class RepositoryRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule_key: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=2000)
    confidence: float = Field(default=0.5, ge=0, le=1)
    evidence_count: int = Field(default=1, ge=1)
    source_prs: list[int] = Field(default_factory=list, max_length=100)


class RepositoryRulesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rules: list[RepositoryRule] = Field(min_length=1, max_length=200)


class RepositoryRulesResponse(BaseModel):
    repository: str
    rules: list[RepositoryRule]
