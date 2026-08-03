"""The structured synthesizer turn contract.

The synthesizer no longer writes prose. It emits ONE JSON object: a turn type
plus a list of claims, where a claim is one factual sentence and the citations
it declares. The gate (``turn_gate``) admits claims one at a time; the renderer
writes every citation marker itself, from validated passages.

WHY THE MODEL-SELECTABLE ENUM IS TWO VALUES, NOT FOUR
The four user-visible turn types are a DOMAIN vocabulary; this enum is the
subset the model may DECIDE after seeing passages.
  * CAPABILITY is deterministic: the meta gate in grounded_qa is a hard veto
    that only fires pre-retrieval, so a model-selected CAPABILITY turn would
    always be one the veto already rejected.
  * CLARIFY is deterministic: grounded_qa already turns a model decline into a
    clarify when the product resolved by name, using application-authored
    interpretation/option text. A model-authored ``ask`` field would be a NEW
    uncited output channel, and a shape-only gate on it (length, ends with '?')
    stops nothing semantic.
The governing rule: the model may SELECT a turn and may AUTHOR text only inside
a claim slot. Every other user-visible byte is deterministic.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from regwatch.common.structured_json import schema_for_prompt
from regwatch.generate.llm import LLMMessage


class ClaimCite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    short_name: str
    page: int = Field(ge=1)


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(max_length=400)
    # Cardinality is DELIBERATELY permissive: a schema that rejected a
    # zero-cite claim would fail model_validate and void the WHOLE turn --
    # exactly the bug this design removes. The gate enforces cardinality
    # per-claim, so a failure costs one claim, never the turn.
    cites: list[ClaimCite] = Field(default_factory=list, max_length=4)


class GroundedTurn(BaseModel):
    # extra="forbid" is load-bearing, not hygiene: without it an adversarial
    # model can smuggle a second text channel (an unknown key) that the
    # route_json ledger would then persist and a debug view would surface.
    model_config = ConfigDict(extra="forbid")

    turn_type: Literal["ANSWER", "NO_EVIDENCE"]
    claims: list[Claim] = Field(default_factory=list, max_length=10)
    unsupported: list[str] = Field(default_factory=list, max_length=2)


# Rendered once at import and appended as a TRAILING system message rather than
# embedded in GROUNDED_QA_SYSTEM: keeping the schema out of the template means
# the template stays reviewable prose and the schema stays generated from the
# pydantic models, so the two can never drift.
#
# The literal word "json" appears here on purpose: OpenAI's json_object mode
# 400s unless the messages contain it, and this message is the only one every
# structured caller is guaranteed to send.
TURN_SCHEMA_MESSAGE = LLMMessage(
    role="system",
    content=(
        "Return ONLY a JSON object that validates against this JSON Schema. "
        "No prose, no markdown fences.\n"
        + json.dumps(schema_for_prompt(GroundedTurn), separators=(",", ":"))
    ),
)
