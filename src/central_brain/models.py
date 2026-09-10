from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

MemoryType = Literal["preference", "fact", "decision", "project", "summary"]
Sensitivity = Literal["public", "internal", "confidential", "restricted"]


class MemoryCreate(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)
    memory_type: MemoryType
    subject_ref: str | None = Field(default=None, max_length=500)
    sensitivity: Sensitivity = "internal"
    confidence: float | None = Field(default=None, ge=0, le=1)
    source: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    dedupe_key: str | None = Field(default=None, max_length=200)
    expires_at: datetime | None = None


class Memory(BaseModel):
    id: UUID
    workspace_id: UUID
    content: str
    memory_type: MemoryType
    status: Literal["proposed", "active", "rejected", "superseded", "archived"]
    sensitivity: Sensitivity
    confidence: float | None
    source: dict[str, Any]
    metadata: dict[str, Any]
    created_at: datetime
    expires_at: datetime | None


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2_000)
    memory_types: list[MemoryType] = Field(default_factory=list, max_length=5)
    limit: int = Field(default=10, ge=1, le=100)


class SearchResult(BaseModel):
    memory: Memory
    score: float


class WriteReceipt(BaseModel):
    memory_id: UUID
    status: str
    created: bool
