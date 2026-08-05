"""Verify the gold set against the corpus it will be scored on.

WHY THIS EXISTS: on 2026-08-05 an audit of the 12 hand-authored gold rows found
3 with wrong page attributions -- a 25% defect rate in the asset the CI gate
treats as ground truth:

  - levalbuterol cited pages 4 and 6; the design text is on 4 and 5.
  - albuterol/budesonide cited page 5 only; the design text is also on page 4.
  - beclomethasone cited pages 1 and 5 for "single actuation content"; that
    phrase appears only on page 2.

A wrong ``expected_sources`` page does not fail anything. It quietly depresses
recall_at_k and citation_precision for a CORRECT answer. With CI floors at 0.90
and 0.95 a defect rate like that makes the thresholds unreachable, so the
natural response is to lower the thresholds to fit the buggy asset -- after
which the gate is calibrated to its own errors, a real regression fits inside
the slack, and a real IMPROVEMENT that starts citing the right page reads as a
regression. The asset becomes a ratchet locking in wrong behavior, silently.

The fix is to make the page pin DERIVABLE rather than asserted: every expected
source carries a verbatim ``quote``, and this module proves that quote really
appears in a chunk at that exact (short_name, page). A mis-pinned row then fails
loudly, at the only moment it matters -- before the scorecard is produced.

Matching is deliberately tolerant of extraction noise (whitespace and case) but
NOT of content: PDF extraction shreds subscripts, so "FEV1" may arrive as
"FEV 1". Quotes should avoid those spans rather than the matcher pretending they
are equal.
"""

from __future__ import annotations

import re

from regwatch.eval.metrics import GoldItem

_WS_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Lowercase and collapse whitespace.

    Line breaks and column padding are artifacts of PDF extraction and of where
    the chunker happened to wrap, neither of which a gold author can see or
    should have to reproduce.
    """
    return _WS_RE.sub(" ", (text or "").lower()).strip()


def _as_page(value: object) -> int:
    """Coerce a page to int; 0 for anything unusable.

    Shape is the integrity gate's job (tests/test_gold_set_integrity.py rejects a
    non-positive page), so here a bad value just resolves to a page the corpus
    cannot have, and is reported as a missing chunk rather than crashing the run.
    """
    # bool is an int subclass; True must not pose as page 1.
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def verify_items(items: list[GoldItem]) -> list[str]:
    """Return one human-readable defect string per problem found; [] when clean.

    Returning strings rather than raising lets the caller report EVERY defect in
    one pass. Fixing gold rows one failure per run is how a 60-row asset takes a
    week to repair.
    """
    from regwatch.store.vector_store import chunk_texts_at

    defects: list[str] = []
    for item in items:
        for src in item.expected_sources:
            short = str(src.get("short_name") or "")
            page = _as_page(src.get("page"))
            quote = str(src.get("quote") or "").strip()
            where = f"{short} p.{page} in {item.question!r}"

            if not quote:
                defects.append(f"{where}: no quote, so the page pin is unverifiable")
                continue

            texts = chunk_texts_at(short, page)
            if not texts:
                # Distinct from a wrong page: the corpus has no such document/page
                # at all, so the row is unanswerable rather than mis-pinned.
                defects.append(f"{where}: no chunk exists at this (short_name, page)")
                continue

            needle = _normalize(quote)
            if not any(needle in _normalize(t) for t in texts):
                defects.append(f"{where}: quote not found on this page -- {quote[:80]!r}")

        # A fact the answer must contain, that appears nowhere in the cited
        # evidence, cannot be produced by a grounded answer. That is a broken
        # gold row, not a model failure.
        for fact in item.expected_facts:
            if not _fact_supported(item, fact):
                defects.append(
                    f"{item.question!r}: expected_fact {fact!r} appears in none of "
                    "its expected_sources"
                )
    return defects


def _fact_supported(item: GoldItem, fact: str) -> bool:
    """Uses the SCORER's normalizer, deliberately, not this module's.

    fact_recall is graded with metrics.normalize_for_fact (which also maps
    hyphens to spaces). Checking facts with a stricter rule here would reject
    rows that score perfectly -- exactly what happened to a fact of
    "non smoking" against evidence reading "non-smoking".
    """
    from regwatch.eval.metrics import normalize_for_fact
    from regwatch.store.vector_store import chunk_texts_at

    needle = normalize_for_fact(fact)
    if not needle:
        return True
    for src in item.expected_sources:
        for text in chunk_texts_at(str(src.get("short_name") or ""), _as_page(src.get("page"))):
            if needle in normalize_for_fact(text):
                return True
    return False
