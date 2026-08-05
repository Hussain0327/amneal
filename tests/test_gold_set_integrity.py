"""Integrity gate for the production QA gold set (`gold_set.jsonl`).

The QA gold set is the INV-1 ("refuse-or-cite") regression corpus, but the live
eval that consumes it (`regwatch.eval.run_eval`) runs in CI only when provider
credentials are configured -- the Databricks arm when the Qwen endpoint and
serving-runtime variable are set, the legacy arm on OPENAI_API_KEY, and neither
otherwise. With no credentials the asset would never be loaded or validated in
standard CI at all. A malformed row (the loader bare-subscripts
`row["question"]`, so a missing key is a KeyError), an INV-1-violating row (a
refusal that cites), or an uncategorized row would ship green.

This file owns gold-set POLICY (categories, stratification floors); the loader
owns SHAPE. Keeping them apart means a malformed asset fails on being unreadable
rather than on a policy rule, which is the more useful error.

This test is network-free (no OpenAI, no DB) and mirrors the file-backed
whitepaper-gold gate (tests/test_eval_gate.py::test_eval_gate_whitepaper_cells,
which asserts the gold loads and has a non-empty floor). It runs in the default
`uv run pytest` step.
"""

from __future__ import annotations

import re
from pathlib import Path

from regwatch.eval import run_eval
from regwatch.eval.metrics import GOLD_CATEGORIES

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


# Per-category floors. The asset holds 62 rows; these sit a little below the
# current counts so ordinary churn is allowed but a category cannot be quietly
# gutted. A stratified set whose strata are not enforced is just a big set.
_CATEGORY_FLOORS = {
    "current_version": 10,
    "exact_identifier": 8,
    "exception": 5,
    "table": 3,
    "duplicate_boilerplate": 4,
    "refusal": 12,
    "clarification": 2,
}

# Ingredients present in the CI seed corpus. A refusal row naming one of these is
# a SCORED negative: the resolver succeeds, the query reaches vector search, and a
# cosine score exists to calibrate against.
_SEEDED_INGREDIENTS = (
    "albuterol",
    "beclomethasone",
    "levalbuterol",
    "budesonide",
    "donepezil",
    "memantine",
)


def test_gold_set_loads_without_error() -> None:
    items = _load()
    # The asset holds 62 data rows. A floor of 55 trips on truncation or an
    # accidental deletion while still permitting churn. (Raise it when the corpus
    # is deliberately grown; a too-low floor is the silent pass we guard against.)
    assert len(items) >= 55, f"gold set looks truncated: only {len(items)} item(s)"


def test_every_category_meets_its_floor() -> None:
    counts: dict[str, int] = {}
    for item in _load():
        counts[item.category] = counts.get(item.category, 0) + 1
    for category, floor in _CATEGORY_FLOORS.items():
        assert (
            counts.get(category, 0) >= floor
        ), f"category {category!r} has {counts.get(category, 0)} row(s), floor is {floor}"


def test_refusals_include_scored_hard_negatives() -> None:
    # The whole reason this set was rebuilt. Before it, EVERY refusal row used a
    # fictional product, so all of them stopped at the resolver and none produced a
    # cosine score -- which is why REFUSAL_SCORE_THRESHOLD=0.30 was never calibrated
    # (docs/EVAL_STATUS.md). A floor on refusal COUNT alone would be satisfied by 16
    # more fictional drugs, so pin the property that actually matters: refusals that
    # name a real seeded product, and therefore reach vector retrieval.
    scored = [
        item
        for item in _load()
        if item.must_refuse and any(ing in item.question.lower() for ing in _SEEDED_INGREDIENTS)
    ]
    assert len(scored) >= 10, (
        f"only {len(scored)} refusal row(s) name a seeded product; without scored "
        "negatives the refusal threshold cannot be calibrated"
    )


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


def test_every_expected_source_carries_a_quote() -> None:
    # The structural half of the anti-mis-pinning defence. The semantic half (does
    # the quote REALLY appear on that page) needs the corpus and lives in
    # regwatch.eval.verify_gold, which run_eval enforces before scoring. This test
    # is network-free, so it still catches the common case -- a row added without a
    # quote -- on every pytest run, including when the live eval is skipped.
    for item in _load():
        for src in item.expected_sources:
            quote = str(src.get("quote") or "").strip()
            assert quote, f"expected source without a quote in {item.question!r}: {src}"
            # A one-word quote matches almost anything and verifies nothing.
            assert (
                len(quote) >= 12
            ), f"quote too short to be evidence in {item.question!r}: {quote!r}"


def test_every_item_declares_a_known_category() -> None:
    # An uncategorized row is invisible to the per-category breakdown, which is
    # the only thing that says WHICH kind of question a regression broke. A typo'd
    # category is worse than none: it silently creates a bucket of one.
    for item in _load():
        assert item.category, f"item declares no category: {item.question!r}"
        assert (
            item.category in GOLD_CATEGORIES
        ), f"unknown category {item.category!r} in {item.question!r}"


def test_refusal_and_clarification_categories_match_their_flags() -> None:
    # The category and the scored expectation must agree, or the breakdown
    # attributes a decision failure to the wrong kind of question.
    for item in _load():
        if item.category == "refusal":
            assert item.must_refuse, f"refusal category without must_refuse: {item.question!r}"
        if item.category == "clarification":
            assert (
                item.must_clarify
            ), f"clarification category without must_clarify: {item.question!r}"


def test_answerable_items_actually_test_something() -> None:
    # A non-refuse, non-clarify item must assert at least one expected source or
    # fact; otherwise it grades nothing and is a no-op that can't catch a
    # regression. must_clarify items are deliberately excluded: they test the
    # clarify DECISION (the system must ask, not guess) and legitimately carry no
    # sources/facts. The form-silent albuterol clarify row is exactly this.
    for item in _load():
        if item.must_refuse or item.must_clarify:
            continue
        assert (
            item.expected_sources or item.expected_facts
        ), f"answerable item asserts nothing: {item.question!r}"
