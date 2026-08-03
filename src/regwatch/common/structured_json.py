"""Provider-agnostic helpers for schema-in-prompt structured JSON output.

Extracted from ``deficiency/structured.py`` so the generate/ package can reuse
them: ``deficiency.structured`` already imports FROM ``generate.llm``, so a
``generate -> deficiency`` edge would close an import cycle.

Deliberately NOT moved: ``parse_structured``. It carries ``repair_json``, and
the synthesis path refuses repair on purpose (repair closes a string truncated
mid-sentence, which can invert a regulatory statement while leaving its real
citation attached). Keeping the repairing parser in deficiency/ makes that
divergence structural rather than a comment nobody reads.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


def schema_for_prompt(model_cls: type[BaseModel]) -> dict[str, Any]:
    """Pydantic JSON schema sanitized the way the upstream stack shipped it.

    additionalProperties: false, no string patterns, anyOf[X, null] flattened.
    Kept even though the schema travels in the prompt rather than as a strict
    response_format: smaller schemas prompt better and the sanitizer is
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


def extract_json_blob(text: str) -> str:
    """Strip common wrappers: markdown fences, leading prose.

    Pure slicing -- it can never mint a token that the model did not emit,
    which is what makes it safe on the synthesis path where ``repair_json``
    is not.
    """
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
