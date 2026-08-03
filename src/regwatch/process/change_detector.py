"""Page-aware, evidence-validated version diffing for PSGs.

When the content_hash of a fetched PSG differs from the most recent
psg_version for the same psg_document, we create a new psg_version row and
generate a *cited* diff summary.

Cited means: every sentence is built from a structured claim whose verbatim
evidence was found on its claimed page in a deterministic diff packet. The LLM
never supplies the rendered citation marker; validation does.
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass
from typing import Any

from regwatch.common.logging import get_logger
from regwatch.generate.llm import LLMMessage, get_llm_provider
from regwatch.generate.prompts import (
    CHANGE_SUMMARY_PROMPT,
    CHANGE_SUMMARY_SYSTEM,
    CHANGE_SUMMARY_USER,
)

log = get_logger(__name__)

_PAGE_SEP = "\n\f\n"
_PROMPT_BUDGET = 16_000
_MAX_EXCERPT_CHARS = 1_200
_MAX_CHANGE_SIDE_CHARS = 4_000
_MAX_QUOTE_CHARS = 300
_MODEL_CITE_RE = re.compile(r"\[(?:previous\s+)?p\.\d+\]", re.IGNORECASE)


@dataclass(frozen=True)
class _Unit:
    page: int
    text: str
    normalized: str


@dataclass(frozen=True)
class _DiffPacket:
    json_text: str
    previous_pages: list[str]
    current_pages: list[str]
    changed_previous_by_page: dict[int, str]
    changed_current_by_page: dict[int, str]


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _split_pages(text: str) -> list[str]:
    return text.split(_PAGE_SEP) if _PAGE_SEP in text else [text]


def _units(pages: list[str]) -> list[_Unit]:
    units: list[_Unit] = []
    for page_number, page in enumerate(pages, start=1):
        for raw in page.splitlines():
            cleaned = re.sub(r"\s+", " ", raw).strip()
            normalized = _normalize(cleaned)
            if normalized:
                units.append(_Unit(page_number, cleaned, normalized))
    return units


def _changed_by_page(units: list[_Unit]) -> dict[int, str]:
    by_page: dict[int, list[str]] = {}
    for unit in units:
        by_page.setdefault(unit.page, []).append(unit.text)
    return {page: _normalize(" ".join(texts)) for page, texts in by_page.items()}


def _render_units(units: list[_Unit]) -> list[dict[str, Any]]:
    rendered: list[dict[str, Any]] = []
    used = 0
    for unit in units:
        text = unit.text[:_MAX_EXCERPT_CHARS]
        if rendered and used + len(text) > _MAX_CHANGE_SIDE_CHARS:
            break
        rendered.append(
            {
                "page": unit.page,
                # Keep the excerpt verbatim. Truncation takes a prefix without an
                # invented ellipsis, so any quote copied from it still occurs on-page.
                "text": text,
            }
        )
        used += len(text)
    return rendered


def _build_diff_packet(previous_text: str, current_text: str) -> _DiffPacket | None:
    previous_pages = _split_pages(previous_text)
    current_pages = _split_pages(current_text)
    previous_units = _units(previous_pages)
    current_units = _units(current_pages)
    matcher = difflib.SequenceMatcher(
        a=[unit.normalized for unit in previous_units],
        b=[unit.normalized for unit in current_units],
        autojunk=False,
    )

    changes: list[dict[str, Any]] = []
    changed_previous: list[_Unit] = []
    changed_current: list[_Unit] = []
    used = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        old = previous_units[i1:i2]
        new = current_units[j1:j2]
        rendered_old = _render_units(old)
        rendered_new = _render_units(new)
        change = {"kind": tag, "previous": rendered_old, "current": rendered_new}
        encoded = json.dumps(change, ensure_ascii=False, separators=(",", ":"))
        if changes and used + len(encoded) > _PROMPT_BUDGET:
            break
        changes.append(change)
        changed_previous.extend(
            _Unit(int(item["page"]), str(item["text"]), _normalize(str(item["text"])))
            for item in rendered_old
        )
        changed_current.extend(
            _Unit(int(item["page"]), str(item["text"]), _normalize(str(item["text"])))
            for item in rendered_new
        )
        used += len(encoded)

    if not changes:
        return None
    return _DiffPacket(
        json_text=json.dumps({"changes": changes}, ensure_ascii=False, separators=(",", ":")),
        previous_pages=previous_pages,
        current_pages=current_pages,
        changed_previous_by_page=_changed_by_page(changed_previous),
        changed_current_by_page=_changed_by_page(changed_current),
    )


def _validated_evidence(
    value: Any,
    *,
    pages: list[str],
    changed_by_page: dict[int, str],
) -> tuple[int, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        return None
    page = value.get("page")
    quote = value.get("quote")
    if not isinstance(page, int) or not 1 <= page <= len(pages):
        return None
    if not isinstance(quote, str) or not quote.strip() or len(quote) > _MAX_QUOTE_CHARS:
        return None
    normalized = _normalize(quote)
    if normalized not in _normalize(pages[page - 1]):
        return None
    # A real page quote is not enough: it must come from a line the deterministic
    # diff marked changed, preventing unrelated cover-page evidence from passing.
    if normalized not in changed_by_page.get(page, ""):
        return None
    return page, quote.strip()


def _render_validated_claims(raw: str, packet: _DiffPacket) -> str:
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        log.warning("change_summary_invalid_json")
        return ""
    claims = payload.get("claims") if isinstance(payload, dict) else None
    if not isinstance(claims, list):
        return ""

    rendered: list[str] = []
    for claim in claims[:3]:
        if not isinstance(claim, dict):
            continue
        statement = claim.get("statement")
        if not isinstance(statement, str):
            continue
        statement = _MODEL_CITE_RE.sub("", " ".join(statement.split())).strip()
        if not statement or len(statement) > 400:
            continue
        previous = _validated_evidence(
            claim.get("previous"),
            pages=packet.previous_pages,
            changed_by_page=packet.changed_previous_by_page,
        )
        current = _validated_evidence(
            claim.get("current"),
            pages=packet.current_pages,
            changed_by_page=packet.changed_current_by_page,
        )
        if previous is None and current is None:
            continue
        markers: list[str] = []
        if previous is not None:
            markers.append(f"[previous p.{previous[0]}]")
        if current is not None:
            markers.append(f"[p.{current[0]}]")
        rendered.append(f"{statement.rstrip('.!?')} {' '.join(markers)}.")
    return " ".join(rendered)[:1000]


def summarize_change(
    previous_text: str | None,
    current_text: str,
    *,
    current_page_count: int,
) -> str:
    """Return a short summary backed by page-anchored, changed excerpts."""
    if previous_text is None:
        # No prior version — emit a marker, not an LLM call.
        first = " ".join(current_text.split())[:240]
        return f"Initial version ingested. Begins: “{first}…”"

    packet = _build_diff_packet(previous_text, current_text)
    if packet is None:
        return "Only formatting or whitespace changed."
    if current_page_count != len(packet.current_pages):
        log.warning(
            "change_summary_page_count_mismatch",
            supplied=current_page_count,
            parsed=len(packet.current_pages),
        )
        return ""

    log.info("llm_prompt", role="change_summary", **CHANGE_SUMMARY_PROMPT.log_fields())
    provider = get_llm_provider(role="extractor")
    resp = provider.complete(
        [
            LLMMessage(role="system", content=CHANGE_SUMMARY_SYSTEM),
            LLMMessage(
                role="user",
                content=CHANGE_SUMMARY_USER.format(evidence=packet.json_text),
            ),
        ],
        temperature=0.0,
        max_tokens=700,
        response_format="json",
    )
    return _render_validated_claims(resp.text, packet)
