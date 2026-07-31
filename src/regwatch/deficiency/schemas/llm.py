from __future__ import annotations

from pydantic import BaseModel


class ParseFailed(BaseModel):
    """Typed sentinel -- the frontend renders this as a needs-human-review card
    instead of receiving a raw LLM dump. Emitted by the defense-in-depth structured
    output path (llm.structured) when strict decoding + repair all fail."""

    layer: str
    reason: str
    raw_output: str
    validation_error: str = ""
    requires_human_review: bool = True
