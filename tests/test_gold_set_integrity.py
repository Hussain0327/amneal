"""Integrity gate for the production QA gold set (`gold_set.jsonl`).

The QA gold set is the INV-1 ("refuse-or-cite") regression corpus, but the live
eval that consumes it (`regwatch.eval.run_eval`) only runs in CI behind
`if: env.OPENAI_API_KEY != ''`, and the no-key path is the CI default, so the
asset is otherwise NEVER loaded or validated in standard CI. A malformed row (the
loader bare-subscripts `row["question"]`, so a missing key is a KeyError) or an
INV-1-violating row (a refusal that cites) would ship green.

This test is network-free (no OpenAI, no DB) and mirrors the file-backed
whitepaper-gold gate (tests/test_eval_gate.py::test_eval_gate_whitepaper_cells,
which asserts the gold loads and has a non-empty floor). It runs in the default
`uv run pytest` step.
"""

from __future__ import annotations

import re
from pathlib import Path

from regwatch.eval import run_eval

# Resolve the asset from the module, not an absolute path, so it moves with the
# package and the test stays valid under any checkout root.
_GOLD_PATH = Path(run_eval.__file__).parent / "gold_set.jsonl"

# short_name in expected_sources is the PSG short id: "PSG_" + the FDA appl_no
# digits (e.g. PSG_021730). Anchored so a stray prefix/suffix is rejected.
_PSG_RE = re.compile(r"^PSG_\d+$")


def _load() -> list:
    # _load_gold raises on a malformed row (KeyError on a missing "question")
    # or a JSON parse error, so calling it at all is itself an assertion that
    # every line is well-formed.
    return run_eval._load_gold(_GOLD_PATH)


def test_gold_set_loads_without_error() -> None:
    items = _load()
    # Floor mirrors the whitepaper gate (len >= 10). The asset currently holds 12
    # data rows; a floor of 10 trips on truncation/accidental deletion while still
    # permitting modest churn. (Bump this floor when the corpus is deliberately
    # grown; a too-low floor is the silent-pass we are guarding against.)
    assert len(items) >= 10, f"gold set looks truncated: only {len(items)} item(s)"


def test_every_question_is_nonempty() -> None:
    for item in _load():
        assert item.question and item.question.strip(), f"empty question: {item!r}"


def test_must_refuse_items_have_no_citations() -> None:
    # INV-1: a refusal must NOT cite sources. If a refusal row carries expected
    # sources, the corpus itself would assert a cited refusal, the exact thing
    # the invariant forbids.
    for item in _load():
        if item.must_refuse:
            assert (
                item.expected_sources == []
            ), f"must_refuse item cites sources (INV-1 violation): {item.question!r}"


def test_expected_sources_are_well_formed() -> None:
    for item in _load():
        for src in item.expected_sources:
            short = src["short_name"]
            assert _PSG_RE.match(short), f"bad short_name {short!r} in {item.question!r}"
            page = src["page"]
            # bool is an int subclass; exclude it so True/False can't pose as a page.
            assert (
                isinstance(page, int) and not isinstance(page, bool) and page > 0
            ), f"non-positive/invalid page {page!r} in {item.question!r}"


def test_answerable_items_actually_test_something() -> None:
    # A non-refuse, non-clarify item must assert at least one expected source or
    # fact; otherwise it grades nothing and is a no-op that can't catch a
    # regression. must_clarify items are deliberately excluded: they test the
    # clarify DECISION (the system must ask, not guess) and legitimately carry no
    # sources/facts. The real estradiol clarify row in the asset is exactly this.
    for item in _load():
        if item.must_refuse or item.must_clarify:
            continue
        assert (
            item.expected_sources or item.expected_facts
        ), f"answerable item asserts nothing: {item.question!r}"
