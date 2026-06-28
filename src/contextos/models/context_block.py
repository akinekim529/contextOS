"""Context blocks — the ephemeral, replayable units the assembler packs into a prompt."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from .common import AccessScope, new_ulid


class BlockSource(str, Enum):
    SYSTEM = "system"
    MEMORY = "memory"
    DOCUMENT = "document"
    HISTORY = "history"
    USER = "user"


class ContextBlock(BaseModel):
    """One assembled fragment. ``score`` is the assembler's final blended relevance;
    ``position`` is the 0-based slot in the rendered prompt (edge-loading places the
    most relevant blocks at head and tail to fight lost-in-the-middle)."""

    id: str = Field(default_factory=new_ulid)
    source: BlockSource
    content: str
    score: float = Field(0.0, description="final blended relevance assigned by the assembler")
    token_count: int = Field(0, ge=0)
    access_scope: AccessScope
    position: int = Field(0, ge=0)
    provenance_ref: str | None = Field(None, description="memory_id / doc id this block derives from")
