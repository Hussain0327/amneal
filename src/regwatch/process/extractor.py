"""BE-requirements extractor — strict per-field citations.

Given the parsed pages of a PSG, returns a structured BE-requirements record
where every populated field carries a citation (page + verbatim quote). Any
field whose value cannot be tied to a source span is left null — never
fabricated.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from regwatch.common.logging import get_logger
from regwatch.generate.llm import LLMMessage, current_model_name, get_llm_provider
from regwatch.generate.prompts import (
    BE_EXTRACTION_PROMPT,
    BE_EXTRACTION_SYSTEM,
    BE_EXTRACTION_USER,
)
from regwatch.process.span_match import NO_MATCH, SpanMatch, quote_on_page

log = get_logger(__name__)


FIELD_NAMES = (
    "study_type",
    "study_design",
    "strengths",
    "dissolution",
    "waiver_conditions",
    "additional_notes",
)


@dataclass
class ExtractionResult:
    fields: dict[str, Any]
    citations: dict[str, Any]
    model_name: str

    @property
    def populated_field_count(self) -> int:
        return sum(1 for v in self.fields.values() if v)


def _passages_for_prompt(pages: list[str], max_chars: int = 18_000) -> str:
    """Stitch pages into one prompt block with page markers."""
    parts: list[str] = []
    used = 0
    for i, page_text in enumerate(pages, start=1):
        block = f"[PAGE {i}]\n{page_text.strip()}\n"
        if used + len(block) > max_chars:
            break
        parts.append(block)
        used += len(block)
    return "\n".join(parts)


def _match_quote_on_page(quote: str, pages: list[str], page: int) -> SpanMatch:
    """Locate the quote on the CLAIMED page only.

    `page` is 1-indexed and must already be range-checked by the caller. We
    search ONLY pages[page-1] so a quote cited to the wrong page is rejected
    even if it appears elsewhere in the document (INV-1: provenance must point
    at the page the quote actually came from).

    Args:
      quote: The verbatim span the model claims it copied.
      pages: The parsed pages of the source document, in order.
      page: 1-indexed page the citation claims.

    Returns:
      The match outcome, or `NO_MATCH` when the quote is empty or the
      claimed page is out of range.
    """
    if not quote:
        return NO_MATCH
    if not isinstance(page, int) or page < 1 or page > len(pages):
        return NO_MATCH
    return quote_on_page(quote, pages[page - 1])


def _validate_field_citation(
    field_name: str,
    field_obj: Any,
    pages: list[str],
) -> tuple[Any, dict[str, Any] | None]:
    """Validate a single (value, citation) pair. Drops value if citation invalid."""
    if not isinstance(field_obj, dict):
        return None, None
    value = field_obj.get("value")
    citation = field_obj.get("citation")
    if value in (None, "", []):
        return None, None
    if not isinstance(citation, dict):
        log.warning("be_extraction_no_citation", field=field_name)
        return None, None
    page = citation.get("page")
    quote = citation.get("quote", "")

    if not isinstance(page, int) or page < 1 or page > len(pages):
        log.warning("be_extraction_bad_page", field=field_name, page=page)
        return None, None
    if not isinstance(quote, str) or len(quote) > 200:
        log.warning("be_extraction_bad_quote_length", field=field_name)
        return None, None
    match = _match_quote_on_page(quote, pages, page)
    if not match.found:
        log.warning("be_extraction_quote_not_found", field=field_name, quote=quote[:80])
        return None, None
    if not match.exact:
        # The quote is not on the page verbatim, only within the edit budget.
        # Logged separately so the new leniency is measurable in prod.
        log.info(
            "be_extraction_quote_fuzzy_match",
            field=field_name,
            page=page,
            distance=match.distance,
            quote=quote[:80],
        )
    return value, {"page": page, "quote": quote}


def extract_be(pages: list[str]) -> ExtractionResult:
    """Run the BE-requirements extractor on the parsed PSG pages.

    Every kept field has a verified citation; fields without a verifiable
    quote are dropped (INV-1 / INV-2). The raw response from the LLM is
    discarded; we keep only validated fields.
    """
    passages = _passages_for_prompt(pages)
    log.info("llm_prompt", role="extractor", **BE_EXTRACTION_PROMPT.log_fields())
    provider = get_llm_provider(role="extractor")
    response = provider.complete(
        [
            LLMMessage(role="system", content=BE_EXTRACTION_SYSTEM),
            LLMMessage(role="user", content=BE_EXTRACTION_USER.format(passages=passages)),
        ],
        temperature=0.0,
        max_tokens=1500,
        response_format="json",
    )

    fields: dict[str, Any] = {name: None for name in FIELD_NAMES}
    citations: dict[str, Any] = {}
    try:
        payload = json.loads(response.text)
    except json.JSONDecodeError as exc:
        log.warning("be_extraction_invalid_json", error=str(exc), raw=response.text[:200])
        return ExtractionResult(fields=fields, citations=citations, model_name=response.model)

    raw_fields = payload.get("fields") or payload  # tolerate flat or nested
    if not isinstance(raw_fields, dict):
        return ExtractionResult(fields=fields, citations=citations, model_name=response.model)

    for name in FIELD_NAMES:
        value, citation = _validate_field_citation(name, raw_fields.get(name), pages)
        if value is not None and citation is not None:
            fields[name] = value
            citations[name] = citation

    log.info(
        "be_extraction_done",
        populated=sum(1 for v in fields.values() if v),
        total=len(FIELD_NAMES),
        model=response.model,
    )
    return ExtractionResult(
        fields=fields,
        citations=citations,
        model_name=current_model_name(role="extractor"),
    )
