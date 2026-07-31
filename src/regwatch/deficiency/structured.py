"""Defense-in-depth structured output over regwatch's LLMProvider seam.

Adapted from DefPredict's llm/structured.py. The upstream stack used the
OpenAI SDK directly with Databricks strict json_schema response_format; that
would bypass regwatch's provider layer and with it the D1 residency guard, so
this version routes every call through ``regwatch.generate.llm.get_llm_provider``
using json_object mode plus the target schema embedded in the system prompt.

Layers (upstream numbering kept):
  L1  provider.complete(response_format="json") with schema-in-prompt
  L2  truncation detector -- providers raise on finish_reason=length; retry
      once with 2x max_tokens
  L3  json-repair -- deterministic in-process salvage
  L4  Pydantic validation
  L5  one-shot schema-repair rescue call (same provider)
  L6  typed ParseFailed sentinel -- raw LLM text never reaches the frontend

D1ResidencyError is re-raised, never converted to ParseFailed: a residency
violation must fail the run loudly, not degrade into a needs-human-review card.
"""

from __future__ import annotations

import json
from typing import Any, TypeVar

from json_repair import repair_json
from pydantic import BaseModel, ValidationError

from regwatch.common.logging import get_logger
from regwatch.deficiency.schemas.llm import ParseFailed
from regwatch.generate.llm import D1ResidencyError, LLMMessage, LLMProvider, get_llm_provider

log = get_logger(__name__)

T = TypeVar("T", bound=BaseModel)

# Upstream defaults (config.py max_tokens_ceiling / structured_output_max_repair_calls).
_MAX_TOKENS_CEILING = 8000
_MAX_REPAIR_CALLS = 1


def schema_for_prompt(model_cls: type[BaseModel]) -> dict[str, Any]:
    """Pydantic JSON schema sanitized the way the upstream stack shipped it.

    additionalProperties: false, no string patterns, anyOf[X, null] flattened.
    Kept even though the schema now travels in the prompt rather than as a
    strict response_format: smaller schemas prompt better and the sanitizer is
    load-bearing for optional-field shapes.
    """
    return _sanitize(model_cls.model_json_schema())


def _sanitize(node: Any) -> Any:
    if isinstance(node, dict):
        node = dict(node)
        if "anyOf" in node:
            variants = node["anyOf"]
            non_null = [
                v for v in variants if not (isinstance(v, dict) and v.get("type") == "null")
            ]
            if len(non_null) == 1:
                inherited = _sanitize(non_null[0])
                for k, v in inherited.items():
                    if k not in node:
                        node[k] = v
                node.pop("anyOf", None)
            else:
                node["anyOf"] = [_sanitize(v) for v in variants]
        node.pop("pattern", None)
        if node.get("type") == "object":
            node["additionalProperties"] = False
        for k, v in list(node.items()):
            node[k] = _sanitize(v)
        return node
    if isinstance(node, list):
        return [_sanitize(v) for v in node]
    return node


def _extract_json_blob(text: str) -> str:
    """Strip common wrappers: markdown fences, leading prose."""
    if not text:
        return text
    stripped = text.strip()
    if "```" in stripped:
        first = stripped.find("```")
        after_fence = stripped[first + 3 :]
        newline = after_fence.find("\n")
        if 0 < newline < 20:
            after_fence = after_fence[newline + 1 :]
        end = after_fence.rfind("```")
        if end > 0:
            return after_fence[:end].strip()
    for open_c, close_c in [("{", "}"), ("[", "]")]:
        start = stripped.find(open_c)
        end = stripped.rfind(close_c)
        if start >= 0 and end > start:
            return stripped[start : end + 1]
    return stripped


def parse_structured(raw: str, model_cls: type[T]) -> tuple[T | None, str | None]:
    """L3 + L4: extract, repair, validate.

    Returns (instance, None) on success or (None, error_message) on failure.
    """
    extracted = _extract_json_blob(raw)
    if not extracted:
        return None, "empty response after extraction"
    try:
        obj = json.loads(extracted)
    except json.JSONDecodeError:
        try:
            repaired = repair_json(extracted)
            obj = json.loads(repaired) if isinstance(repaired, str) else repaired
            log.info("json_repair_salvage", model=model_cls.__name__)
        except Exception as exc:
            return None, f"json_repair failed: {exc}"
    try:
        return model_cls.model_validate(obj), None
    except ValidationError as exc:
        return None, exc.json(indent=None)


def _to_llm_messages(
    messages: list[dict[str, str]], model_cls: type[BaseModel]
) -> list[LLMMessage]:
    """Wire messages plus the schema instruction json_object mode relies on."""
    schema_str = json.dumps(schema_for_prompt(model_cls), separators=(",", ":"))
    out = [LLMMessage(role=m["role"], content=m["content"]) for m in messages]
    out.append(
        LLMMessage(
            role="system",
            content=(
                "Return ONLY a JSON object that validates against this JSON Schema. "
                "No prose, no markdown fences.\n" + schema_str
            ),
        )
    )
    return out


def _complete_with_truncation_retry(
    provider: LLMProvider,
    llm_messages: list[LLMMessage],
    *,
    temperature: float,
    max_tokens: int,
) -> str:
    """L1 + L2. Providers raise RuntimeError on finish_reason=length/incomplete;
    retry once with doubled max_tokens, then let the failure propagate."""
    capped = min(max_tokens, _MAX_TOKENS_CEILING)
    try:
        return provider.complete(
            llm_messages, temperature=temperature, max_tokens=capped, response_format="json"
        ).text
    except D1ResidencyError:
        raise
    except RuntimeError as exc:
        if capped >= _MAX_TOKENS_CEILING:
            raise
        log.warning("deficiency_structured_truncation_retry", old=capped, error=str(exc)[:200])
        return provider.complete(
            llm_messages,
            temperature=temperature,
            max_tokens=min(capped * 2, _MAX_TOKENS_CEILING),
            response_format="json",
        ).text


def _repair_with_rescue_call(
    provider: LLMProvider,
    raw: str,
    validation_error: str,
    model_cls: type[T],
    context: str = "",
) -> tuple[T | None, ParseFailed | None]:
    """L5: one-shot repair call (upstream used the 70B moderator; here it is
    the same regwatch provider -- there is exactly one sanctioned model)."""
    if _MAX_REPAIR_CALLS <= 0:
        return None, ParseFailed(
            layer="L5",
            reason="repair rescue disabled",
            raw_output=raw[:2000],
            validation_error=validation_error[:1000],
        )
    schema_str = json.dumps(schema_for_prompt(model_cls), indent=2)
    system = (
        "You are a schema-repair assistant. Return ONLY valid JSON matching the "
        "target schema. No prose, no markdown fences."
    )
    user = (
        (f"## Context\n{context}\n\n" if context else "")
        + f"## Target JSON Schema\n```json\n{schema_str}\n```\n\n"
        + f"## Malformed Output\n{raw[:4000]}\n\n"
        + f"## Validation Error\n{validation_error[:1500]}\n\n"
        + "Emit ONLY the corrected JSON object."
    )
    log.warning("deficiency_rescue_called", model=model_cls.__name__)
    try:
        text = _complete_with_truncation_retry(
            provider,
            _to_llm_messages(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                model_cls,
            ),
            temperature=0.0,
            max_tokens=_MAX_TOKENS_CEILING,
        )
    except D1ResidencyError:
        raise
    except Exception as exc:
        log.error("deficiency_rescue_exception", error=str(exc)[:200])
        return None, ParseFailed(
            layer="L5",
            reason="rescue call raised exception",
            raw_output=raw[:2000],
            validation_error=str(exc)[:1000],
        )
    instance, err = parse_structured(text, model_cls)
    if instance is not None:
        log.info("deficiency_rescue_success", model=model_cls.__name__)
        return instance, None
    return None, ParseFailed(
        layer="L5",
        reason="rescue call also failed to produce valid output",
        raw_output=text[:2000],
        validation_error=(err or "")[:1000],
    )


def chat_completion(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.0,
    max_tokens: int = 4096,
    provider: LLMProvider | None = None,
) -> str:
    """Plain (non-structured) completion over the same provider seam.

    Keeps the upstream ``llm.client.chat_completion`` name so vendored call
    sites (detection/selection.py) only change their import line, while the
    call inherits the provider layer's D1 residency guard.
    """
    llm = provider if provider is not None else get_llm_provider(role="default")
    return llm.complete(
        [LLMMessage(role=m["role"], content=m["content"]) for m in messages],
        temperature=temperature,
        max_tokens=max_tokens,
    ).text


def structured_call(
    messages: list[dict[str, str]],
    model_cls: type[T],
    *,
    temperature: float = 0.0,
    max_tokens: int = 4096,
    repair_context: str = "",
    provider: LLMProvider | None = None,
) -> tuple[T | None, ParseFailed | None]:
    """Top-level entry point: L1 -> L5. Returns validated instance XOR ParseFailed.

    ``provider`` is injectable for tests; production callers omit it and get
    the configured regwatch provider (Databricks in prod, so the D1 residency
    guard checks every served response).
    """
    llm = provider if provider is not None else get_llm_provider(role="default")
    llm_messages = _to_llm_messages(messages, model_cls)
    try:
        raw = _complete_with_truncation_retry(
            llm, llm_messages, temperature=temperature, max_tokens=max_tokens
        )
    except D1ResidencyError:
        raise
    except Exception as exc:
        log.error("deficiency_l1_call_exception", model=model_cls.__name__, error=str(exc)[:200])
        return None, ParseFailed(
            layer="L1",
            reason="LLM call raised exception before parsing",
            raw_output="",
            validation_error=str(exc)[:1000],
        )

    instance, err = parse_structured(raw, model_cls)
    if instance is not None:
        return instance, None

    first_failure = ParseFailed(
        layer="L1+L2+L3+L4",
        reason="structured output did not validate",
        raw_output=raw[:2000],
        validation_error=(err or "")[:1000],
    )
    repaired, repair_failure = _repair_with_rescue_call(
        llm, raw, err or "", model_cls, context=repair_context
    )
    if repaired is not None:
        return repaired, None
    return None, repair_failure or first_failure
