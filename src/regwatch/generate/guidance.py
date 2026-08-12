"""Constrained AI planning for non-answer Ask turns.

The model never writes display prose here. It selects one server-allowlisted next
step and may prioritize IDs for options the application already constructed. The
orchestrator keeps ownership of status, copy, filters, citations, and regulatory
policy, so sending a clarification through AI does not create a second uncited
factual output channel.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from regwatch.common.structured_json import extract_json_blob, schema_for_prompt
from regwatch.generate.llm import LLMMessage
from regwatch.generate.prompt_identity import identify_prompt
from regwatch.generate.prompts import QUERY_GUIDANCE_SYSTEM, QUERY_GUIDANCE_USER
from regwatch.generate.rag_contract import ClarifyOption

NextStep = Literal[
    "choose_product",
    "choose_dosage_form",
    "narrow_source_topic",
    "name_product",
    "ask_evidence_question",
    "view_capabilities",
]


class GuidancePlan(BaseModel):
    """The only model-selectable values on a non-answer turn."""

    model_config = ConfigDict(extra="forbid")

    next_step: NextStep
    option_ids: list[str] = Field(default_factory=list, max_length=3)


GUIDANCE_SCHEMA_MESSAGE = LLMMessage(
    role="system",
    content=(
        "Return ONLY a JSON object that validates against this JSON Schema. "
        "No prose, no markdown fences.\n"
        + json.dumps(schema_for_prompt(GuidancePlan), separators=(",", ":"))
    ),
)
QUERY_GUIDANCE_PROMPT = identify_prompt(
    "regwatch.query_guidance",
    "1",
    QUERY_GUIDANCE_SYSTEM,
    QUERY_GUIDANCE_USER,
    GUIDANCE_SCHEMA_MESSAGE.content,
)


@dataclass(frozen=True)
class GuidanceRequest:
    messages: list[LLMMessage]
    allowed_next_steps: tuple[NextStep, ...]
    option_ids: frozenset[str]
    available_options: tuple[dict[str, str], ...]


def _allowed_steps(reason: str, status: str) -> tuple[NextStep, ...]:
    if status == "scope_warning":
        return ("ask_evidence_question",)
    if status == "meta":
        return ("view_capabilities",)
    if reason in {"ambiguous_product", "did_you_mean", "brand_lookup", "mixed_products"}:
        return ("choose_product",)
    if reason == "multi_form":
        return ("choose_dosage_form",)
    if reason == "vague_input":
        return ("narrow_source_topic",)
    # need_product and product_not_covered replace the old no_product refusal;
    # no_product itself stays mapped for turns replayed from history.
    if reason in {"no_product", "need_product", "product_not_covered"}:
        return ("name_product",)
    if reason in {"low_top_score", "model_refusal", "no_valid_citations", "material_drop"}:
        return ("narrow_source_topic", "choose_dosage_form")
    # This function is used only for deliberate non-answer domain outcomes.
    # Keeping a narrow fallback is safer than letting a new reason silently gain
    # an unconstrained action vocabulary.
    return ("narrow_source_topic",)


def _options(clarify: list[ClarifyOption], related: list[ClarifyOption]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for channel, options in (("clarify", clarify), ("related", related)):
        for index, option in enumerate(options):
            rows.append(
                {
                    "id": f"{channel}:{index}",
                    "label": option.label,
                    "channel": channel,
                }
            )
    return rows


def build_guidance_request(
    *,
    question: str,
    status: str,
    reason: str,
    product: str | None,
    clarify: list[ClarifyOption],
    related: list[ClarifyOption],
) -> GuidanceRequest:
    """Build the bounded planner request from application-owned route state."""
    allowed = _allowed_steps(reason, status)
    options = _options(clarify, related)
    context = {
        "untrusted_question": question,
        "route": {"status": status, "reason": reason},
        "trusted_product_context": product,
        "allowed_next_steps": list(allowed),
        "available_options": options,
    }
    return GuidanceRequest(
        messages=[
            LLMMessage(role="system", content=QUERY_GUIDANCE_SYSTEM),
            LLMMessage(
                role="user",
                content=QUERY_GUIDANCE_USER.format(
                    context_json=json.dumps(context, separators=(",", ":"))
                ),
            ),
            GUIDANCE_SCHEMA_MESSAGE,
        ],
        allowed_next_steps=allowed,
        option_ids=frozenset(row["id"] for row in options),
        available_options=tuple(options),
    )


def parse_guidance_plan(raw: str, request: GuidanceRequest) -> GuidancePlan:
    """Parse and validate model selections against this turn's allowlists."""
    try:
        payload = json.loads(extract_json_blob(raw))
        plan = GuidancePlan.model_validate(payload)
    except (json.JSONDecodeError, TypeError, ValidationError) as exc:
        raise ValueError("invalid_guidance_structure") from exc
    if plan.next_step not in request.allowed_next_steps:
        raise ValueError("disallowed_guidance_step")
    if any(option_id not in request.option_ids for option_id in plan.option_ids):
        raise ValueError("unknown_guidance_option")
    if len(set(plan.option_ids)) != len(plan.option_ids):
        raise ValueError("duplicate_guidance_option")
    return plan


def render_guidance_message(
    plan: GuidancePlan,
    *,
    reason: str,
    product: str | None,
    fallback: str,
) -> str:
    """Render trusted copy selected by the plan; never render model prose."""
    product_label = product.title() if product else "the product"
    if reason == "no_product":
        return (
            "I couldn't identify the product confidently enough to search the right FDA "
            "guidance. What generic ingredient should I use?"
        )
    # need_product / product_not_covered own their copy at the call site (the
    # clarify interpretation), so the planner only orders their options.
    if reason in {"need_product", "product_not_covered"}:
        return fallback
    if reason in {"low_top_score", "model_refusal", "no_valid_citations", "material_drop"}:
        if plan.next_step == "choose_dosage_form":
            return (
                f"I found {product_label}, but I couldn't verify an answer from the retrieved "
                "FDA passages. Which dosage form or route should I narrow the search to?"
            )
        return (
            f"I found {product_label}, but I couldn't verify that answer from the current FDA "
            "passages. Can you narrow the question to study design, strengths, dissolution, "
            "or dosage form?"
        )
    return fallback


def selected_option_records(plan: GuidancePlan, request: GuidanceRequest) -> list[dict[str, str]]:
    """Resolve selected positional IDs before the display list is reordered."""
    by_id = {row["id"]: row for row in request.available_options}
    return [dict(by_id[option_id]) for option_id in plan.option_ids]


def prioritize_options(
    options: list[ClarifyOption], *, channel: str, plan: GuidancePlan
) -> list[ClarifyOption]:
    """Move model-selected existing IDs first without hiding or creating options."""
    selected = [
        int(option_id.split(":", 1)[1])
        for option_id in plan.option_ids
        if option_id.startswith(f"{channel}:")
    ]
    selected_set = set(selected)
    return [options[index] for index in selected] + [
        option for index, option in enumerate(options) if index not in selected_set
    ]
