import json
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

MemoryType = Literal["preference", "fact", "decision", "project", "summary"]
Sensitivity = Literal["public", "internal", "confidential", "restricted"]
MemoryStatus = Literal["proposed", "active", "rejected", "superseded", "archived"]


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kind: Literal["human", "conversation", "document"]
    reference: str = Field(min_length=1, max_length=1000)


class MemoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    content: str = Field(min_length=1, max_length=20000)
    memory_type: MemoryType
    subject_ref: str | None = Field(default=None, max_length=500)
    sensitivity: Sensitivity = "internal"
    visibility: Literal["private", "workspace"] = "workspace"
    confidence: float | None = Field(default=None, ge=0, le=1)
    source: Source
    metadata: dict[str, Any] = Field(default_factory=dict)
    dedupe_key: str | None = Field(default=None, min_length=1, max_length=200)
    expires_at: datetime | None = None

    @field_validator("metadata")
    @classmethod
    def bounded_metadata(cls, value):
        if len(json.dumps(value, allow_nan=False).encode()) > 4096:
            raise ValueError("metadata exceeds 4096 bytes")
        return value

    @field_validator("expires_at")
    @classmethod
    def aware_expiry(cls, value):
        if value is not None and value.utcoffset() is None:
            raise ValueError("expires_at must include a timezone")
        return value


class Memory(BaseModel):
    id: UUID
    workspace_id: UUID
    created_by: UUID | None = None
    content: str
    subject_ref: str | None = None
    memory_type: MemoryType
    status: MemoryStatus
    sensitivity: Sensitivity
    visibility: Literal["private", "workspace"] = "workspace"
    confidence: float | None
    source: dict[str, Any]
    metadata: dict[str, Any]
    created_at: datetime
    expires_at: datetime | None
    supersedes_id: UUID | None = None
    deleted_at: datetime | None = None


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    query: str = Field(min_length=1, max_length=2000)
    memory_types: list[MemoryType] = Field(default_factory=list, max_length=5)
    limit: int = Field(default=10, ge=1, le=100)


class SearchResult(BaseModel):
    memory: Memory
    score: float


class WriteReceipt(BaseModel):
    memory_id: UUID
    status: str
    created: bool


class Skill(BaseModel):
    name: str
    description: str
    version: str
    definition: str
    content_sha256: str
