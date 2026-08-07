"""The structured synthesizer turn contract.

The synthesizer no longer writes prose. It emits ONE JSON object: a turn type
plus a list of claims, where a claim is one factual sentence and the citations
it declares. The gate (``turn_gate``) admits claims one at a time; the renderer
writes every citation marker itself, from validated passages.

WHY THE SYNTHESIZER-SELECTABLE ENUM IS TWO VALUES, NOT FOUR
The four user-visible turn types are a DOMAIN vocabulary; this enum is the
subset the synthesizer may DECIDE after seeing passages.
  * CAPABILITY remains an application-owned outcome. A separate constrained
    guidance planner may select ``view_capabilities`` only when the server puts
    that action on the turn's allowlist.
  * CLARIFY remains an application-owned outcome with application-authored
    interpretation and option text. The guidance planner may select a safe next
    step and prioritize existing option IDs, but it cannot author an ``ask``
    field or create an uncited output channel.
The governing synthesis rule: the model may SELECT a turn and may AUTHOR text
only inside a claim slot. The separate planner contract in ``guidance.py``
selects actions, never user-visible prose. Every other user-visible byte is
deterministic.
"""

from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

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
    # 10 -> 20. This cap is NOT a trim: pydantic raises on the 21st claim, the
    # gate returns malformed_structure, and the user gets "service temporarily
    # unavailable" with ZERO claims. It is a whole-turn kill switch, so a cap
    # set below what the model may reasonably emit converts a long answer into
    # an outage.
    #
    # 20, not 30, because the EVIDENCE binds before the schema does: exactly 8
    # passages reach synthesis (effective_rerank_top_k) and overlapping chunks
    # collapse to as few as 4 distinct citable (short_name, page) keys. Claims
    # beyond what 8 passages can independently support are either redundant or
    # fail the gate's overlap check. 30 is also unaffordable: 30 x 400 chars x
    # 4 cites is 5,707 output tokens, above SYNTH_MAX_TOKENS_CEILING.
    #
    # Safe for INV-1: the gate validates each claim independently against this
    # turn's passages, and its admission loop is order-independent, so a longer
    # list cannot make any single claim likelier to survive incorrectly. The
    # real exposure of a larger n is AVAILABILITY -- the whole-turn materiality
    # guard trips on a single material drop -- which is why the depth targets
    # that would actually fill this cap are gated on a measured baseline.
    claims: list[Claim] = Field(default_factory=list, max_length=20)
    # The list was capped at 2; the ITEMS were unbounded. The prompt asks for
    # "SHORT LABELS" and nothing enforced it, so two 2,000-char strings could
    # add ~2,000 output tokens to a payload the budget sized at ~30.
    unsupported: list[Annotated[str, StringConstraints(max_length=80)]] = Field(
        default_factory=list, max_length=2
    )


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
