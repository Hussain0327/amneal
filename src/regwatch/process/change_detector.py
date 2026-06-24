"""Version diffing for PSGs.

When the content_hash of a fetched PSG differs from the most recent
psg_version for the same psg_document, we create a new psg_version row and
generate a *cited* diff summary.

Cited means: every sentence in the summary carries a [p.<n>] page citation
that points to the CURRENT version. We use the LLM for prose but we validate
that any [p.N] tokens point to real pages.
"""

from __future__ import annotations

import re

from regwatch.common.logging import get_logger
from regwatch.generate.llm import LLMMessage, get_llm_provider
from regwatch.generate.prompts import CHANGE_SUMMARY_SYSTEM, CHANGE_SUMMARY_USER

log = get_logger(__name__)

_PROMPT_BUDGET = 8000  # chars per side
_CITE_RE = re.compile(r"\[p\.(\d+)\]")
# Sentence splitter: each segment is run-of-non-terminators plus its trailing
# terminator, so a dropped sentence takes its punctuation with it. NOTE: this is
# applied to text whose [p.N] cite periods have been masked first (see
# _strip_bad_cite_sentences), so the period inside a citation never splits.
_SENTENCE_RE = re.compile(r"[^.!?]*(?:[.!?]+|$)")
# Placeholder for the period inside a [p.N] token while we sentence-split.
_CITE_DOT = "\x00"


def summarize_change(
    previous_text: str | None,
    current_text: str,
    *,
    current_page_count: int,
) -> str:
    """Return a short, cited summary of what changed between two PSG versions."""
    if previous_text is None:
        # No prior version — emit a marker, not an LLM call.
        first = " ".join(current_text.split())[:240]
        return f"Initial version ingested. Begins: “{first}…”"

    prev_trim = previous_text[:_PROMPT_BUDGET]
    curr_trim = current_text[:_PROMPT_BUDGET]
    provider = get_llm_provider()
    resp = provider.complete(
        [
            LLMMessage(role="system", content=CHANGE_SUMMARY_SYSTEM),
            LLMMessage(
                role="user",
                content=CHANGE_SUMMARY_USER.format(previous=prev_trim, current=curr_trim),
            ),
        ],
        temperature=0.0,
        max_tokens=400,
    )
    summary = resp.text.strip()

    # Validate any [p.N] tokens point to real pages in the CURRENT version.
    bad = [m.group(1) for m in _CITE_RE.finditer(summary) if int(m.group(1)) > current_page_count]
    if bad:
        # INV-1: a [p.N] beyond the document's page count is an ungrounded
        # provenance claim. Don't merely log it — drop the whole sentence that
        # carries it so the unsupported claim never reaches a user surface
        # (dossier "Latest change:", QueryCitation.diff_summary, watch digest).
        # Sentences whose cites are all in range are preserved verbatim.
        log.warning("change_summary_invalid_page_cites", bad_pages=bad)
        summary = _strip_bad_cite_sentences(summary, current_page_count)
    return summary[:1000]


def _strip_bad_cite_sentences(summary: str, current_page_count: int) -> str:
    """Drop sentences containing a [p.N] that exceeds current_page_count.

    Splits on sentence terminators (. ! ?), removes any sentence carrying an
    out-of-range page citation, and rejoins. A sentence with only in-range
    cites (or no cites) is kept exactly as written.
    """
    # Mask the period inside every [p.N] so it isn't mistaken for a sentence end.
    masked = _CITE_RE.sub(lambda m: f"[p{_CITE_DOT}{m.group(1)}]", summary)
    kept: list[str] = []
    for match in _SENTENCE_RE.finditer(masked):
        sentence = match.group(0).replace(_CITE_DOT, ".")
        if not sentence.strip():
            continue
        has_bad = any(int(m.group(1)) > current_page_count for m in _CITE_RE.finditer(sentence))
        if not has_bad:
            kept.append(sentence.strip())
    return " ".join(kept)
