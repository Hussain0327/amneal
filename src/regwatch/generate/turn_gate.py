"""Claim-level admission gate for the synthesizer turn.

Last updated: 2026-08-11.

This module is the reliability boundary: it is the ONLY place model-authored
bytes can become user-visible text, and it admits them one claim at a time.

WHAT THE POLICY IS NOW (v7, live in prod)
"Cite the facts, talk like a person." A sentence that states what FDA guidance
says must carry its passage number(s); reasoning and conversation sentences may
carry none. INV-1 is unchanged underneath that: an UNCITED SOURCE FACT is still
dropped here, exactly as it always was. What changed is that the gate can now
admit an uncited sentence when the parser classified it as reasoning or
conversation, instead of refusing the whole turn.

Amended 2026-08-10: the flag-gated live-draft SSE channel (REGWATCH_LIVE_DRAFT,
see grounded_qa._stream_structured) may emit un-gated PROVISIONAL bytes; the
gate remains the only source of VALIDATED user-visible text.

Pure. No DB, no settings, no provider. Input: the raw completion text, the
passages that were actually sent this turn, and the question. Output: an
``AdmittedTurn`` (what the caller may render) or a ``GateFailure`` (the payload
did not parse). The caller decides which decline branch a verdict maps to.

WHY THIS REPLACED THE PROSE SEGMENT SPLITTER
The old gate split the model's prose on sentence/newline boundaries and refused
the WHOLE TURN if any segment lacked a citation marker. A model that answers
correctly but puts its citations in a trailing bibliography therefore had every
content sentence read as uncited: a citation PLACEMENT bug that refused three
of three interactive production queries. This gate takes a claims payload
instead. Under v6/v7 the model does write [n] markers again, but prose_turn
resolves them to passage POSITIONS before this module sees them, so the marker
text itself is never trusted and the renderer still writes every canonical
marker from a validated passage.

FIVE PROPERTIES THE OLD GATE DID NOT HAVE
1. A markdown header cannot occupy a claim slot (claim text is collapsed to one
   line and structural markup is rejected outright).
2. Citation markers are written ONLY by the renderer. Model-authored markers are
   stripped; a claim still holding '[' or ']' afterwards is dropped, because an
   UNBALANCED fabricated marker survives strip_all_citations (the bracket regex
   requires a matched pair) and would otherwise render beside a real stamp.
3. A claim whose declared cites do not ALL resolve to a passage sent this turn
   is dropped WHOLE, never partially rewritten (OD-4). Stricter than the old
   filter_citations, which kept a bracket's valid pairs and let the sentence
   stand -- re-stamping model text whose real source was never retrieved onto an
   unrelated real passage.
4. Dropping a claim can silently invert an answer ("a fed study is NOT required"
   is exactly the kind of qualifier a model mis-cites), so after dropping we ask
   whether the drop was MATERIAL and reject the whole answer if it was.
5. The gate's input and its decision are both persisted (see ``ledger``), so the
   drop rate is measurable from real traffic instead of inferred from a counter.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from pydantic import ValidationError

from regwatch.common.citations import strip_all_citations
from regwatch.common.logging import get_logger
from regwatch.common.sentences import sentence_count
from regwatch.common.structured_json import extract_json_blob
from regwatch.generate.rag_contract import Citation, ClaimTag
from regwatch.generate.turn_schema import GroundedTurn
from regwatch.retrieve.retriever import RetrievedPassage

log = get_logger(__name__)

# Bumped whenever the rendered answer's SHAPE changes, so a stored answer can be
# read back against the renderer that produced it.
RENDERER_VERSION = 1
# v7 selective citation. NOT a flat bump of RENDERER_VERSION -- that would
# mis-stamp every v5/v6 row for the whole dark window, when those rows are
# still produced by the v1 renderer. ledger() takes it as a parameter, passed
# by the ONE caller only when selective_mode is true (B.10.1.4).
RENDERER_VERSION_SELECTIVE = 2

# ---------------------------------------------------------------------------
# OD-4: materiality.
#
# Dropping an unsupported claim is safe only when what remains still means the
# same thing. These words mark a claim as carrying obligation, permission,
# prohibition or exception -- remove one of those and the surviving answer can
# read as its own opposite ("A fasting study is required" surviving alone after
# "A fed study is NOT required for the 45 mcg strength" was dropped).
#
# The list is deliberately broad and deliberately NOT narrowed by guesswork.
# Every decision is logged with the claim text and the triggering word so the
# real firing rate can be measured and the list narrowed from data.
# ---------------------------------------------------------------------------
MATERIALITY_WORDS: tuple[str, ...] = (
    "not",
    "required",
    "prohibited",
    "approved",
    "except",
    "only",
    "unless",
    "may",
    "must",
)

# Word boundaries, never substrings: "may" must not fire on "Mayo" and "not"
# must not fire on "notice".
_MATERIALITY_RE = re.compile(r"\b(?:" + "|".join(MATERIALITY_WORDS) + r")\b", re.IGNORECASE)


# "the May 2026 draft PSG" is a DATE. Matching it as the modal "may" made the
# gold set refuse whole answers (verdict=material_drop) because a month name
# appeared in a citation-worthy sentence. Mask month-shaped uses before the
# deontic scan; every real modal use ("may be waived") is unaffected.
_MONTH_MAY_RE = re.compile(r"\bMay\b(?=\s+\d)")


def materiality_trigger(claim_text: str) -> str | None:
    """The word that makes dropping ``claim_text`` MATERIAL, or None.

    The single materiality predicate: truthiness is the decision, and the
    returned word is what gets logged. Pure and case-insensitive, matching on
    word boundaries only.
    """
    match = _MATERIALITY_RE.search(_MONTH_MAY_RE.sub("", claim_text or ""))
    return match.group(0).lower() if match is not None else None


# ---------------------------------------------------------------------------
# v7 selective citation (B.10.3.1): the AIS (attributed-information-source)
# lexicon. A sentence carrying none of MATERIALITY_WORDS can still be a report
# of what a source SAYS ("FDA recommends a fasting study.") -- v6 never needed
# this because it drops every uncited sentence regardless of kind, but v7
# admits uncited REASONING/CONVERSATION, so this is the second guard that
# reclassifies such a sentence back to source_fact before it can render
# unsupported. Verb-anchored, not noun-bearing (B.10.3.1's overturn of the
# parked B.3 list): a noun list fires on ordinary connective prose ("guidance")
# and turns "the talking version" back into "the canned version" -- see the
# module docstring's design note for the measured evidence. Composes with
# MATERIALITY_WORDS rather than duplicating it (those words already imply an
# assertion).
# ---------------------------------------------------------------------------
#
# Expanded post-launch-review (adversarial INV-1 lens, finding P0): the
# original 18-word list missed ordinary obligation/attribution phrasing
# ("should", "no", "waived", "says", ...) that a real model plausibly writes,
# letting an uncited FDA assertion classify as conversation/reasoning and
# render. The expansion is verified against B.10.3.1's own acceptance test:
# the two lexicons (materiality + source-assertion), frame-stripped, produce
# ZERO hits on every uncited sentence of the three v7 assistant exemplars
# (tests/test_v7_selective.py::test_v7_exemplars_survive_their_own_gate).
# "describe(s)"/"cover(s)" are deliberately EXCLUDED -- they fire on the
# exemplars' own uncited sentences ("The passages I received cover the
# dissolution method...", "the two passages describe one dosage form...").
# MATERIALITY_WORDS is NOT touched here: it is shared with the LIVE v6 path,
# and any addition there would change prod material-drop behavior.
SOURCE_ASSERTION_WORDS: tuple[str, ...] = (
    "acceptable",
    "according",
    "advise",
    "advises",
    "allows",
    "calls",
    "establishes",
    "exempt",
    "exemption",
    "exempts",
    "expected",
    "expects",
    "indicates",
    "instructs",
    "mandates",
    "no",
    "notes",
    "permits",
    "prohibition",
    "prohibits",
    "recommend",
    "recommendation",
    "recommendations",
    "recommended",
    "recommending",
    "recommends",
    "require",
    "requirement",
    "requirements",
    "requires",
    "requiring",
    "say",
    "says",
    "sets",
    "shall",
    "should",
    "specified",
    "specifies",
    "specify",
    "stated",
    "states",
    "suggests",
    "waive",
    "waived",
    "permitted",
    "exempted",
    "waives",
)

_SOURCE_ASSERTION_RE = re.compile(
    r"\b(?:" + "|".join(SOURCE_ASSERTION_WORDS) + r")\b", re.IGNORECASE
)


def source_assertion_trigger(text: str) -> str | None:
    """The word that makes ``text`` a report of what a source SAYS, or None."""
    match = _SOURCE_ASSERTION_RE.search(text or "")
    return match.group(0).lower() if match is not None else None


# ---------------------------------------------------------------------------
# Verdicts -- what the caller must do with an admitted turn.
# ---------------------------------------------------------------------------
VERDICT_ANSWER = "answer"  # every emitted claim was admitted
VERDICT_PARTIAL = "partial"  # some dropped, immaterial -> render + disclose
VERDICT_MATERIAL_DROP = "material_drop"  # some dropped, material -> reject whole answer
VERDICT_NO_VALID_CITATIONS = "no_valid_citations"  # nothing admitted
VERDICT_NO_EVIDENCE = "no_evidence"  # the model declined
VERDICT_CONVERSATIONAL_DECLINE = "conversational_decline"  # v7: admitted, but zero source facts

# Drop reasons. These are OPERATOR strings (the route_json ledger and logs); no
# drop reason ever reaches a user.
DROP_EMPTY = "empty_text"
DROP_MARKUP = "markup_in_text"
DROP_MULTI_SENTENCE = "multi_sentence"
DROP_NO_CITES = "no_cites"
DROP_UNKNOWN_CITATION = "unknown_citation"

# ---------------------------------------------------------------------------
# Epistemic claim kinds and the citation corrector. Reached only when a caller
# passes admit_turn(correct=True): the v6 and v7 prose callers do, the v5
# caller does not, so under v5 these stay at their defaults and only show up as
# ledger fields.
# ---------------------------------------------------------------------------
CLAIM_KIND_SOURCE_FACT = "source_fact"
CLAIM_KIND_REASONING = "reasoning"

# correction_method values the ledger can carry. material_exempt marks a claim
# the materiality guard EXCLUDED from correction/downgrade, so the exemption
# rate is measurable from real traffic before any threshold is revisited.
CORRECTION_LEXICAL = "lexical_overlap"
CORRECTION_MATERIAL_EXEMPT = "material_exempt"

# GATE-authored, deterministic downgrade frame -- the same trust model as the
# renderer-authored citation markers: these words are written by the gate, never
# accepted from the model, so a reader can trust the hedge was applied by code.
REASONING_FRAME = "The guidance does not state this directly; my reading is: "

# Frame openers that mark an uncited sentence as declared REASONING rather than
# conversation (v7 prompt rule 2; B.10.2). Matched whitespace/case-normalized,
# prefix-only: a frame buried mid-sentence is not a declaration. Lives HERE
# (not prose_turn, which imports FROM this module) because both the selective
# classifier (prose_turn) AND render_decline's guard (this module) need
# frame-stripping, and a turn_gate -> prose_turn import would be a cycle
# (B.10.3.2). prose_turn re-exports this tuple so prose_turn.REASONING_FRAME_PREFIXES
# stays a valid attribute for every existing reference.
REASONING_FRAME_PREFIXES: tuple[str, ...] = (
    "the guidance does not state this directly",
    "reading the guidance together",
    "my reading is",
    "beyond the guidance,",
)


def frame_split(text: str) -> tuple[str, str]:
    """(recognized frame prefix, remaining body). ('', text) when unframed.

    Scanning a framed sentence WHOLE is what makes the recommended frame
    unusable: "The guidance does not state this directly" carries a materiality
    word ("not") while asserting nothing. The frame is an allowlisted,
    content-free hedge, so the lexicons run on the BODY. Applies to
    gate-authored and model-authored frames alike -- the text is identical
    either way.
    """
    original = text or ""
    collapsed = " ".join(original.split())
    lowered = collapsed.lower()
    for prefix in REASONING_FRAME_PREFIXES:
        if lowered.startswith(prefix):
            body = collapsed[len(prefix) :].lstrip(" ")
            if body[:1] in (";", ",", ":"):
                body = body[1:]
            return prefix, body.strip()
    return "", original


# Correction thresholds. Both are required: the FLOOR says the claim must be
# substantially contained in the winning passage at all, and the MARGIN says the
# winner must be unambiguous among the passages sent this turn -- a near-tie
# means the evidence cannot say WHICH passage the model meant, and a guessed
# re-stamp is exactly the OD-4 failure this gate exists to prevent. Values are
# provisional; every correction is ledgered so they can be calibrated from data.
CORRECTION_OVERLAP_FLOOR = 0.6
CORRECTION_OVERLAP_MARGIN = 0.2

# ---------------------------------------------------------------------------
# OD-5: the two user-visible disclosure strings. Plain language, no
# implementation detail -- a user must never read "claim 3 failed citation
# validation". Operator detail lives in the ledger.
# ---------------------------------------------------------------------------
PARTIAL_DROP_DISCLOSURE = (
    "Some statements were omitted because their supporting citations " "could not be verified."
)
MATERIAL_DROP_TEXT = (
    "This guidance states what is required, not why. I can tell you what it "
    "requires for this product."
)

# Said to the user when a completion breached the pathological-output bounds
# and the one repair attempt did not recover it (issue #183). Deliberately
# ordinary speech: the 2,000-char rule is plumbing, and a reason code, a
# character count or the word "validation" would leak mechanism into a
# regulatory conversation. It invites the next turn rather than closing the
# thread, because nothing about the QUESTION was wrong -- only our answer to
# it. The machine-readable reason travels separately, on the audit row.
OVERSIZE_RECOVERY_TEXT = "I got too wordy there. Ask me again and I will keep it tighter."

# Retrieval-sufficiency disclosure. Unchanged wording: the eval set, the prompt
# eval and tests/test_grounded_qa_citations.py all pin this exact prefix.
PARTIAL_EVIDENCE_PREFIX = "Evidence not found in the supplied passages for:"
_MAX_UNSUPPORTED_LABELS_LEN = 160
_UNSUPPORTED_LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 /,()'&-]*")

# The SAME sentence split eval/metrics.py uses for faithfulness. Sharing it is
# what makes "one claim = one rendered sentence" and "faithfulness == 1.0 for an
# all-admitted turn" the same statement -- now shared by IMPORT rather than by
# two identical literals kept in step by hand (see common/sentences.py).

# Structural markdown a claim slot may never carry: a heading, a link, or a bare
# URL. The answer is rendered through a markdown component with GFM autolinking,
# so any of these would render as chrome or as a clickable off-corpus pointer
# sitting beside real citation stamps.
_MARKUP_RE = re.compile(r"(?:^\s*#)|(?:\]\()|(?:\bhttps?://)|(?:\bwww\.)", re.IGNORECASE)
# v7 sanitize-keep (B.10.3.4), selective mode only: a leading markdown heading
# is stripped BEFORE the _MARKUP_RE check rather than dropping the whole claim,
# because a heading-shaped sentence is common conversational structure in
# free-form prose and costs nothing to keep once the "#" itself is gone. Link/
# URL markup and any residual bracket still DROP_MARKUP unchanged -- only the
# heading marker is sanitize-kept. Emphasis (**bold**/*italic*/_underscore_) is
# NOT touched: it already passes _MARKUP_RE today, so stripping it would be a
# v7-only text change with no safety value.
_HEADING_PREFIX_RE = re.compile(r"^#{1,6}\s+")
# Numbered forms ("1. ", "2) ") are stripped alongside the dash/star/plus bullets
# because a surviving "1." reads as a sentence terminator to _SENT_SPLIT: the
# claim then counts as two sentences and is dropped for DROP_MULTI_SENTENCE, even
# though the model wrote exactly one. List STRUCTURE is a renderer decision built
# from the admitted claims, so no ordinal a model authored is ever load-bearing.
_LEADING_BULLET_RE = re.compile(r"^(?:[-*+]|\d+[.)])\s+")
_WORD_RE = re.compile(r"[a-z0-9]+")

# Matches Claim.text's schema cap (turn_schema.py), so the ledger records the
# WHOLE claim rather than a window of it. At 200 a material_drop row could name
# the materiality word while truncating away the clause that contained it --
# recording that something material was dropped but not what, which is the one
# question the ledger exists to answer (INV-6). The schema bounds the field, so
# this cannot grow unboundedly.
_LEDGER_TEXT_CHARS = 400


@dataclass(frozen=True)
class ParsedClaim:
    """One already-parsed sentence on its way into the gate.

    The admission core's input, deliberately NOT ``turn_schema.Claim``: that
    model's caps (text 400 chars, cites 4, claims 20) exist to bound arbitrary
    MODEL-authored JSON, and re-imposing them on prose that our own sentence
    splitter produced is issue #183 -- a good answer failing as
    ``malformed_structure``. Uncapped by construction, and free of pydantic so
    the core does not depend on the v5 schema module at all.
    """

    text: str
    cites: tuple[tuple[str, int], ...] = ()  # declared (short_name, page), declaration order


@dataclass(frozen=True)
class AdmittedClaim:
    index: int  # the model's claim index, so the ledger lines up with the draft
    text: str  # sanitized: one line, one sentence, no model-authored markers
    pairs: tuple[tuple[str, int], ...]  # canonical (SHORT_NAME, page), declaration order
    citations: tuple[Citation, ...]
    overlap: float  # claim/passage token overlap -- logged, NOT enforced
    # Epistemic ledger fields (additive; defaults preserve every v5 consumer).
    kind: str = CLAIM_KIND_SOURCE_FACT
    correction_method: str | None = None
    original_cites: tuple[tuple[str, int], ...] | None = None  # pre-correction, as declared
    downgraded: bool = False


@dataclass(frozen=True)
class DroppedClaim:
    index: int
    text: str
    cites: tuple[tuple[str, int], ...]
    bad_cites: tuple[tuple[str, int], ...]
    reason: str
    material_word: str | None
    # material_exempt when the materiality guard blocked correction/downgrade.
    correction_method: str | None = None


@dataclass(frozen=True)
class AdmittedTurn:
    turn_type: str
    verdict: str
    admitted: tuple[AdmittedClaim, ...]
    dropped: tuple[DroppedClaim, ...]
    unsupported: tuple[str, ...]  # labels kept (rendered)
    dropped_unsupported: tuple[str, ...]  # labels rejected by the shape guard
    emitted: int  # claims the model emitted, before admission
    material_word: str | None  # the word that made a drop material, if any


@dataclass(frozen=True)
class GateFailure:
    """The completion did not parse as a GroundedTurn.

    A MACHINE fault, never a corpus statement -- the caller must serve the
    service-unavailable copy, not the refusal string, or the audit row records
    an assertion about the corpus that was never tested.
    """

    reason: str
    detail: str


def allowed_passage_map(
    passages: list[RetrievedPassage],
) -> dict[tuple[str, int], RetrievedPassage]:
    """(SHORT_NAME, page) -> the passage a citation may resolve to.

    Key case-insensitively: a model may declare a short_name in any casing while
    the passage short_name is canonical uppercase (PSG_NNNNNN). A case-sensitive
    miss would drop a valid claim and could flip a genuinely-grounded answer to
    a false refusal.

    Passages arrive best-first and a page often spans several chunks, so
    setdefault keeps the TOP-ranked chunk per (doc, page) -- the one the model
    most plausibly used -- as the citation's chunk_id/snippet/score. A plain
    assignment would bind the evidence to the weakest chunk.
    """
    allowed: dict[tuple[str, int], RetrievedPassage] = {}
    for p in passages:
        allowed.setdefault((p.short_name.upper(), p.page), p)
    return allowed


def _citation_for(passage: RetrievedPassage) -> Citation:
    snippet = passage.text.strip().replace("\n", " ")[:200]
    return Citation(
        # Canonical casing comes from the PASSAGE, never from the model's echo:
        # the renderer writes markers, so there is no as-emitted casing to honor.
        short_name=passage.short_name,
        page=passage.page,
        chunk_id=passage.chunk_id,
        doc_id=passage.doc_id,
        version_id=passage.version_id,
        source_url=passage.source_url,
        snippet=snippet,
        # Confidence: the matched passage's retriever score, carried on the
        # citation it grounds. INV-1 unaffected -- this is the same passage that
        # validated the citation.
        score=passage.score,
        # Human-identifying provenance, all already in hand: "PSG_020911" names
        # an FDA application number and nothing a reader can act on. No new
        # query -- normalized_name is a named field and the rest ride the chunk
        # row's denormalized metadata (pgvector_store._TEXT_METADATA_COLUMNS).
        product_name=passage.normalized_name or None,
        dosage_form=_meta_text(passage, "dosage_form"),
        route=_meta_text(passage, "route"),
        psg_type=_meta_text(passage, "psg_type"),
    )


def _meta_text(passage: RetrievedPassage, key: str) -> str | None:
    """One passage metadata string, or None when absent or blank.

    Ingest writes "" rather than NULL for an unknown dosage_form/route, so an
    empty string has to collapse to None here: the UI distinguishes "not
    recorded" from "not loaded", and "" would render as a stray separator.
    """
    value = passage.metadata.get(key)
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _sanitize_claim_text(raw: str) -> str:
    """Collapse to one line, drop model-authored markers, tidy the residue."""
    text = " ".join((raw or "").split())
    text = _LEADING_BULLET_RE.sub("", text)
    # Model-authored citation markers are NEVER trusted: the renderer writes
    # every marker from a validated passage. Stripping first also means a claim
    # cannot smuggle a marker for a page it did not declare in `cites`.
    text = strip_all_citations(text)
    text = " ".join(text.split())
    return re.sub(r"\s+([.,;:])", r"\1", text).strip()


def _sentence_count(text: str) -> int:
    return sentence_count(text)


def _tokens(text: str) -> set[str]:
    return {t for t in _WORD_RE.findall(text.lower()) if len(t) > 2}


def _overlap_score(claim_tokens: set[str], passage_tokens: set[str]) -> float:
    """The fraction of claim tokens found in the passage tokens."""
    if not claim_tokens:
        return 0.0
    return round(len(claim_tokens & passage_tokens) / len(claim_tokens), 3)


def _overlap(text: str, passages: list[RetrievedPassage]) -> float:
    """Claim/passage token overlap, for calibration only.

    Citation validation is MECHANICAL: a claim can cite a real, retrieved
    passage that does not support it. That was true of the old gate too. Logging
    the overlap is what lets a floor be calibrated from real traffic later
    instead of guessed now.
    """
    passage_tokens: set[str] = set()
    for p in passages:
        passage_tokens |= _tokens(p.text)
    return _overlap_score(_tokens(text), passage_tokens)


def correct_unknown_citation(
    claim_text: str,
    declared_cites: tuple[tuple[str, int], ...],
    evidence_passages: list[RetrievedPassage],
) -> RetrievedPassage | None:
    """Best-match re-stamp for a claim whose declared cite resolved to nothing.

    The declared cite names a passage that was never sent this turn. When ONE of
    the passages that WAS sent is an unambiguously strong lexical match, the
    claim can be re-stamped onto it instead of dropped: the winner must clear an
    absolute overlap floor AND lead the runner-up by a margin. Returns the
    winning passage rather than its (name, page) pair so the citation binds the
    chunk whose text actually matched, not the top-ranked chunk of that page.
    ``declared_cites`` is unused here; the caller ledgers it as original_cites.

    Never for material claims: token overlap is negation-blind -- "a fed study
    is NOT required" scores highly against the passage saying it IS required --
    so the materiality check is repeated here in addition to the call site, and
    no future caller can reach the argmax without it.
    """
    if materiality_trigger(claim_text) is not None:
        return None
    claim_tokens = _tokens(claim_text)
    if not claim_tokens:
        return None
    scored = sorted(
        ((_overlap_score(claim_tokens, _tokens(p.text)), p) for p in evidence_passages),
        key=lambda pair: pair[0],
        reverse=True,
    )
    if not scored:
        return None
    best_score, best = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    if best_score < CORRECTION_OVERLAP_FLOOR:
        return None
    if best_score - runner_up < CORRECTION_OVERLAP_MARGIN:
        return None
    return best


def _metadata_uniform(passages: list[RetrievedPassage]) -> bool:
    """True when every passage carries truthy AND identical product metadata.

    Correction assumes all evidence describes one product in one dosage form,
    but that premise is metadata-conditional, not guaranteed: the upstream
    mixed-product/form clarify guards SKIP passages with empty metadata (ingest
    writes "" when the FDA listing lacks the field), so mixed evidence can reach
    the gate whenever metadata is missing. Empty or non-uniform metadata
    therefore disables correction outright (F6).
    """
    combos: set[tuple[str, str, str]] = set()
    for p in passages:
        form = str(p.metadata.get("dosage_form") or "")
        route = str(p.metadata.get("route") or "")
        if not (p.normalized_name and form and route):
            return False
        combos.add((p.normalized_name, form, route))
    return len(combos) == 1


def downgrade_to_reasoning(claim: AdmittedClaim) -> AdmittedClaim:
    """Reframe an uncited-but-benign claim as the gate's own hedged reading.

    The frame is GATE-authored and deterministic -- the model cannot emit these
    words into a claim slot and have them trusted, exactly as it cannot author a
    citation marker. Cites are cleared because a reasoning sentence must never
    wear a stamp.
    """
    if materiality_trigger(claim.text) is not None:
        # F1 (P0): a material sentence served uncited-but-hedged can still
        # invert the guidance, so material claims are exempt from every
        # softening path. Reaching here is a caller bug, not a data condition.
        raise ValueError("material claim must never be downgraded to reasoning")
    return replace(
        claim,
        text=f"{REASONING_FRAME}{claim.text}",
        pairs=(),
        citations=(),
        kind=CLAIM_KIND_REASONING,
        downgraded=True,
    )


def _keep_unsupported(labels: list[str], question: str) -> tuple[list[str], list[str]]:
    """Split model-emitted unsupported labels into (kept, rejected).

    ``unsupported`` names parts of the QUESTION that the passages did not
    answer. It is not a second prose channel, so a label must be short, must
    match a restrictive charset, and must be ANCHORED in the question -- share a
    substantive word with it. Without the anchor a model could state a
    regulatory fact ("fed study waiver granted") in an uncited final line.
    """
    question_tokens = _tokens(question)
    kept: list[str] = []
    rejected: list[str] = []
    used = 0
    for raw in labels:
        label = " ".join((raw or "").split())
        anchored = any(t in question_tokens for t in _tokens(label) if len(t) >= 4)
        # +2 for the ", " separator this label would add to the rendered tail.
        projected = used + len(label) + (2 if kept else 0)
        if (
            label
            and len(label) <= 80
            and _UNSUPPORTED_LABEL_RE.fullmatch(label) is not None
            and anchored
            and projected <= _MAX_UNSUPPORTED_LABELS_LEN
        ):
            kept.append(label)
            used = projected
        else:
            rejected.append(label[:80])
    return kept, rejected


def admit_turn(
    raw_text: str,
    *,
    passages: list[RetrievedPassage],
    question: str,
    correct: bool = False,
    downgrade_uncited: bool | None = None,
    kinds: Sequence[str] | None = None,
    selective: bool = False,
) -> AdmittedTurn | GateFailure:
    """Parse one structured completion and admit its claims.

    ``correct=False`` (every v5 caller) keeps today's behavior exactly; the new
    epistemic fields appear in the output at their defaults. ``correct=True``
    lets an unknown-cite claim attempt a lexical re-stamp and an uncited benign
    claim downgrade to gate-framed reasoning, both refused for material claims.

    ``downgrade_uncited`` splits the two corrector behaviors along the phase
    boundary: None (default) follows ``correct``; False keeps re-stamp
    correction while an uncited benign claim stays on the DROP_NO_CITES path.
    The v6 prose caller passes False because v6's policy is cite or refuse, so
    it must never serve a gate-framed UNCITED sentence. v7 is where uncited
    sentences became servable, and it gets there through ``selective`` below,
    not through this flag.

    ``kinds``/``selective`` (v7, B.10.3.4): ``kinds`` is the parser's per-claim
    epistemic reading, positional against ``claims`` (the bridge emits
    claims in parse order, so the correspondence is exact). Reachable ONLY with
    ``selective=True`` -- a v5/v6 caller that happened to pass ``kinds`` would
    still get today's behavior. A length mismatch against ``claims``
    ignores ``kinds`` entirely (logged, never silent) and treats every claim as
    ``source_fact``: the strict direction, more citation enforcement, never
    less. A claim carrying declared cites is ALWAYS ``source_fact`` regardless
    of what ``kinds`` says for it -- a sentence wearing a marker is an
    assertion. An uncited claim whose kind is ``reasoning``/``conversation`` is
    admitted with no pairs/citations rather than dropped; an uncited
    ``source_fact`` is unaffected (still ``DROP_NO_CITES``, never downgraded --
    INV-1).

    NO json_repair, deliberately, and this divergence from the shipped
    deficiency ladder must not be "harmonized" later. The usual argument
    (repair can invent a citation) is the weaker one. The real one: repair
    CLOSES A STRING TRUNCATED MID-SENTENCE. Given
    ``"text":"A biowaiver is not granted for the 45 mcg strength unless in vitro data``
    repair closes the string and the object, pydantic validates, the cite
    resolves against a REAL retrieved passage, and the renderer emits an
    inverted, truncated regulatory statement wearing a real clickable stamp.
    Citation re-validation does not protect claim TEXT.
    """
    extracted = extract_json_blob(raw_text)
    if not extracted:
        return GateFailure("malformed_structure", "empty response after extraction")
    try:
        payload = json.loads(extracted)
    except json.JSONDecodeError as exc:
        return GateFailure("malformed_structure", f"json decode failed: {exc}")
    try:
        turn = GroundedTurn.model_validate(payload)
    except ValidationError as exc:
        return GateFailure("malformed_structure", exc.json(indent=None)[:1000])

    return admit_claims(
        turn.turn_type,
        tuple(
            ParsedClaim(text=c.text, cites=tuple((cc.short_name, cc.page) for cc in c.cites))
            for c in turn.claims
        ),
        tuple(turn.unsupported),
        passages=passages,
        question=question,
        correct=correct,
        downgrade_uncited=downgrade_uncited,
        kinds=kinds,
        selective=selective,
    )


def admit_claims(
    turn_type: str,
    claims: Sequence[ParsedClaim],
    unsupported: Sequence[str] = (),
    *,
    passages: list[RetrievedPassage],
    question: str,
    correct: bool = False,
    downgrade_uncited: bool | None = None,
    kinds: Sequence[str] | None = None,
    selective: bool = False,
) -> AdmittedTurn:
    """Admit already-parsed claims. The gate's whole judgment lives here.

    Split out of ``admit_turn`` for issue #183. ``admit_turn`` is now only the
    JSON front door -- extract, decode, schema-validate -- and every caller that
    already HAS parsed claims (the prose path) reaches this directly, so the v5
    schema's caps never see prose. The admission logic below is unchanged: same
    order, same drops, same verdicts, same ledger.

    Returns ``AdmittedTurn`` and never ``GateFailure``: a parse failure is by
    definition a front-door outcome, and there is nothing left to fail to parse.
    """
    if turn_type == "NO_EVIDENCE":
        # Claims and unsupported labels are discarded WHOLESALE: a model that
        # declines has, by its own account, nothing to cite, so anything it put
        # in a claim slot is unvetted by definition.
        if claims or unsupported:
            log.warning(
                "qa_claims_on_no_evidence",
                claims=len(claims),
                unsupported=len(unsupported),
            )
        return AdmittedTurn(
            turn_type="NO_EVIDENCE",
            verdict=VERDICT_NO_EVIDENCE,
            admitted=(),
            dropped=(),
            unsupported=(),
            dropped_unsupported=(),
            emitted=len(claims),
            material_word=None,
        )

    allowed = allowed_passage_map(passages)
    admitted: list[AdmittedClaim] = []
    dropped: list[DroppedClaim] = []
    # F6: computed ONCE per turn. Correction is only safe when the evidence is
    # provably one product/form, and that premise is metadata-conditional.
    metadata_uniform = correct and _metadata_uniform(passages)
    allow_downgrade = correct if downgrade_uncited is None else (correct and downgrade_uncited)

    # v7 (B.10.3.4): the parser's per-claim kinds, positional against
    # claims. An arity mismatch is the strict-direction fallback (every
    # claim reads as source_fact) rather than a guess at which index means
    # what.
    claim_kinds: list[str] | None = None
    if kinds is not None:
        if len(kinds) == len(claims):
            claim_kinds = list(kinds)
        else:
            log.warning("gate_kind_arity_mismatch", declared=len(kinds), claims=len(claims))

    for index, claim in enumerate(claims):
        declared = claim.cites
        text = _sanitize_claim_text(claim.text)
        layout_stripped = False
        if selective:
            # 2026-08-20: presentation is a PRODUCT FEATURE now. The v7 prompt
            # tells the model to use headings and bullets when they help, and
            # the gold set then refused 18 otherwise-correct answers purely for
            # DROP_MARKUP on a leading "##". A heading marker carries no
            # provenance meaning, so strip it and keep the claim -- for CITED
            # claims too, which the previous narrowing excluded.
            #
            # BUT the old narrowing was also load-bearing for a second reason:
            # DROP_MARKUP fired BEFORE the lexical corrector, so a heading-
            # prefixed cited claim whose declared cite does not exist could not
            # be re-stamped onto an unrelated passage by token overlap. Removing
            # the drop without replacing that guard re-opened exactly that hole
            # (verified: a claim citing PSG_999999,p.7 was served stamped
            # PSG_020503,p.3). So: strip the layout, and make a claim that
            # needed stripping ineligible for correction. Presentation gets
            # free; the re-stamp surface is unchanged from before v7.
            stripped = _HEADING_PREFIX_RE.sub("", text, count=1)
            stripped = _LEADING_BULLET_RE.sub("", stripped).strip()
            layout_stripped = stripped != text
            text = stripped
        # A claim wearing a marker is always an assertion, regardless of what
        # the parser's kind said for it (B.10.3.4). Absent that, the parser's
        # reading governs ONLY in selective mode; every other caller keeps
        # every claim as source_fact, matching AdmittedClaim's own default.
        if declared or not selective:
            claim_kind = CLAIM_KIND_SOURCE_FACT
        else:
            claim_kind = claim_kinds[index] if claim_kinds is not None else CLAIM_KIND_SOURCE_FACT
        reason: str | None = None
        bad: tuple[tuple[str, int], ...] = ()

        if not text:
            reason = DROP_EMPTY
        elif _MARKUP_RE.search(text) or "[" in text or "]" in text:
            # A leftover bracket means an UNBALANCED fabricated marker survived
            # stripping (the bracket grammar requires a matched pair), which
            # would render as literal text beside a real stamp.
            reason = DROP_MARKUP
        elif _sentence_count(text) > 1:
            # Non-negotiable: without it, one text slot can hold a cited fact
            # AND an uncited fabrication behind valid cites, making this design
            # strictly WEAKER than the segment splitter it replaces.
            reason = DROP_MULTI_SENTENCE
        elif not declared:
            # v7 uncited admit (B.10.3.4): reasoning/conversation, no cites ->
            # admitted below with pairs=()/citations=(), never dropped. An
            # uncited source_fact is UNAFFECTED -- it still falls to
            # DROP_NO_CITES exactly as before (INV-1: never downgraded).
            reason = None if claim_kind != CLAIM_KIND_SOURCE_FACT else DROP_NO_CITES
        else:
            bad = tuple((s, p) for (s, p) in declared if (s.upper(), p) not in allowed)
            if bad:
                reason = DROP_UNKNOWN_CITATION

        correction_method: str | None = None
        corrected: RetrievedPassage | None = None
        downgrade = False
        correctable = correct and not (layout_stripped and declared)
        if correctable and reason in (DROP_NO_CITES, DROP_UNKNOWN_CITATION):
            if materiality_trigger(text) is not None:
                # F1 (P0): token overlap is negation-blind -- "a fed study is
                # NOT required" matches the passage saying it IS required -- so
                # a material claim is never re-stamped and never reframed. It
                # stays on the drop path and the material-drop verdict applies
                # unchanged; the exemption is ledgered so its rate is
                # measurable.
                correction_method = CORRECTION_MATERIAL_EXEMPT
            elif reason == DROP_NO_CITES:
                if allow_downgrade:
                    downgrade = True
                    reason = None
            elif metadata_uniform:
                corrected = correct_unknown_citation(text, declared, passages)
                if corrected is not None:
                    correction_method = CORRECTION_LEXICAL
                    reason = None
                    log.info(
                        "qa_claim_cite_corrected",
                        claim_index=index,
                        original_cites=[f"{s},p.{p}" for s, p in declared],
                        corrected_cite=f"{corrected.short_name},p.{corrected.page}",
                        claim_text=text[:400],
                    )
            # else: F6 -- metadata empty or non-uniform, so the single
            # product/form premise behind a lexical re-stamp does not hold;
            # unknown cites keep today's drop behavior.

        if reason is not None:
            trigger = materiality_trigger(text)
            # Every materiality decision is logged with the claim text and the
            # triggering word, so the firing rate is measurable from real
            # traffic and MATERIALITY_WORDS can be narrowed from data.
            log.info(
                "qa_claim_dropped",
                claim_index=index,
                drop_reason=reason,
                material_word=trigger,
                bad_cites=[f"{s},p.{p}" for s, p in bad],
                claim_text=text[:400],
            )
            dropped.append(
                DroppedClaim(
                    index=index,
                    text=text,
                    cites=declared,
                    bad_cites=bad,
                    reason=reason,
                    material_word=trigger,
                    correction_method=correction_method,
                )
            )
            continue

        if corrected is not None:
            admitted.append(
                AdmittedClaim(
                    index=index,
                    text=text,
                    pairs=((corrected.short_name, corrected.page),),
                    citations=(_citation_for(corrected),),
                    overlap=_overlap(text, [corrected]),
                    correction_method=CORRECTION_LEXICAL,
                    original_cites=declared,
                )
            )
            continue
        if downgrade:
            admitted.append(
                downgrade_to_reasoning(
                    AdmittedClaim(index=index, text=text, pairs=(), citations=(), overlap=0.0)
                )
            )
            continue

        seen: set[tuple[str, int]] = set()
        pairs: list[tuple[str, int]] = []
        citations: list[Citation] = []
        cited_passages: list[RetrievedPassage] = []
        for short_name, page in declared:
            fold = (short_name.upper(), page)
            if fold in seen:
                continue
            seen.add(fold)
            passage = allowed[fold]
            pairs.append((passage.short_name, passage.page))
            citations.append(_citation_for(passage))
            cited_passages.append(passage)
        admitted.append(
            AdmittedClaim(
                index=index,
                text=text,
                pairs=tuple(pairs),
                citations=tuple(citations),
                overlap=_overlap(text, cited_passages),
                kind=claim_kind,
            )
        )

    kept_labels, rejected_labels = _keep_unsupported(list(unsupported), question)

    if not admitted:
        # Zero admitted is NOT a no-evidence turn even when the model emitted
        # claims: telling the user the corpus does not cover the question when
        # the truth is that every citation failed validation is a false
        # statement, and it hides a model-quality regression from the /metrics
        # rollup (which groups on mode+refused, not on status).
        verdict = VERDICT_NO_VALID_CITATIONS
        material_word = None
        kept_labels = []
    else:
        # Identical to today for every non-selective turn: with no dropped
        # claims this generator is empty and material_word is None, which is
        # what the old `elif not dropped` branch hardcoded.
        material_word = next((d.material_word for d in dropped if d.material_word), None)
        no_source_fact_admitted = selective and not any(
            c.kind == CLAIM_KIND_SOURCE_FACT for c in admitted
        )
        if material_word:
            # MATERIAL_DROP outranks the v7 decline (B.10.1.1): a turn that
            # dropped an obligation-bearing sentence must say so specifically,
            # not launder it into a chatty "I could not find that".
            verdict = VERDICT_MATERIAL_DROP
        elif no_source_fact_admitted and not dropped:
            # v7 found-nothing: every admitted claim is uncited
            # reasoning/conversation, NOTHING was dropped either, so nothing
            # was asserted about the corpus at all. selective=False makes
            # this branch unreachable, so v5/v6 verdicts are byte-identical.
            verdict = VERDICT_CONVERSATIONAL_DECLINE
        elif no_source_fact_admitted:
            # Post-launch-review fix (P1): the decline must not outrank a
            # dropped claim. Here the model DID assert a source fact, the
            # gate dropped it (unsupported/unknown cite), and only harmless
            # filler survived alongside it -- e.g. "FDA recommends a fed
            # study... Let me know if you want the dissolution details as
            # well." with the first sentence dropped for no_cites. Serving
            # the filler alone as a conversational "Evidence gap" would
            # mislabel a citation failure as reason=model_refusal and orphan
            # a dangling referent ("That is the same design..." with its
            # antecedent deleted). v6 reaches the SAME refusal for the
            # identical completion: there the filler would drop too (v6
            # never admits anything uncited), landing on `not admitted` ->
            # VERDICT_NO_VALID_CITATIONS below. Treat v7 the same way rather
            # than rendering the orphaned filler.
            verdict = VERDICT_NO_VALID_CITATIONS
            kept_labels = []
        elif dropped:
            verdict = VERDICT_PARTIAL
        else:
            verdict = VERDICT_ANSWER

    return AdmittedTurn(
        turn_type="ANSWER",
        verdict=verdict,
        admitted=tuple(admitted),
        dropped=tuple(dropped),
        unsupported=tuple(kept_labels),
        dropped_unsupported=tuple(rejected_labels),
        emitted=len(claims),
        material_word=material_word,
    )


def citations(turn: AdmittedTurn) -> list[Citation]:
    """Validated citations in first-appearance order, deduped by (name, page)."""
    seen: set[tuple[str, int]] = set()
    out: list[Citation] = []
    for claim in turn.admitted:
        for citation in claim.citations:
            key = (citation.short_name.upper(), citation.page)
            if key in seen:
                continue
            seen.add(key)
            out.append(citation)
    return out


def claim_tags(turn: AdmittedTurn) -> tuple[ClaimTag, ...]:
    """One ClaimTag per admitted claim, in render order.

    Render order == ``turn.admitted`` order == ``render_answer``'s sentence
    order, so a caller can zip this against the rendered sentences. Pure
    accessor: eval/metrics.faithfulness is the only consumer.
    """
    return tuple(ClaimTag(kind=c.kind, cited=bool(c.pairs)) for c in turn.admitted)


def _marker(pairs: tuple[tuple[str, int], ...]) -> str:
    """The canonical compound-bracket form, identical to filter_citations'."""
    return "[" + "; ".join(f"{s}, p.{p}" for s, p in pairs) + "]"


def render_answer(turn: AdmittedTurn) -> str:
    """The deterministic answer string for a renderable verdict.

    Each claim renders as ONE sentence whose citation marker sits BEFORE the
    terminal punctuation, so the faithfulness scorer -- which splits on that
    punctuation -- sees a cited sentence rather than a bare sentence followed by
    a bare marker.

    Callers must check ``turn.verdict`` first: for a non-renderable verdict
    there are no admitted claims and this returns "".

    Post-launch-review fix (P0): an uncited admitted claim (reasoning/
    conversation) re-crosses the gate boundary HERE too, mirroring
    ``render_decline``'s guard -- this is the answer-path twin of that
    function's defense in depth, closing the asymmetry where a decline was
    re-scanned before serving but an answer was not, even though the answer
    path serves with ``refused=False`` under a ``Sources:`` header. A hit
    drops the sentence and folds to the same disclosure a gate-level drop
    gets (OD-5): silently dropping would hand back a confident answer with
    the qualifier deleted, same as any other drop.
    """
    sentences: list[str] = []
    render_time_drop = False
    for claim in turn.admitted:
        if not claim.pairs:
            # Only uncited claims are re-scanned: a claim carrying pairs is a
            # cite-validated source_fact already, not this guard's concern.
            scan = frame_split(claim.text)[1] or claim.text
            if materiality_trigger(scan) is not None or source_assertion_trigger(scan) is not None:
                render_time_drop = True
                continue
        body = claim.text
        terminator = "."
        if body and body[-1] in ".!?":
            terminator = body[-1]
            body = body[:-1].rstrip()
        if claim.pairs:
            sentences.append(f"{body} {_marker(claim.pairs)}{terminator}")
        else:
            # v7 uncited kinds (reasoning/conversation): no marker to write.
            # Unreachable under v5/v6 -- :531-532's DROP_NO_CITES drops every
            # zero-pair claim before it can be admitted, and no live v5/v6
            # caller enables the uncited-downgrade path either -- so flag-off
            # rendering is provably byte-identical without a flag check here
            # (the scan above is unreachable flag-off for the same reason:
            # `claim.pairs` is never empty for an admitted v5/v6 claim).
            sentences.append(f"{body}{terminator}")
    if not sentences:
        return ""

    parts = [" ".join(sentences)]
    if turn.unsupported:
        parts.append(f"\n{PARTIAL_EVIDENCE_PREFIX} {', '.join(turn.unsupported)}.")
    if turn.verdict == VERDICT_PARTIAL or render_time_drop:
        # OD-5: the user is told that something was removed, in plain language
        # and with no implementation detail. Silence would hand back a
        # confident, fully-cited answer with an exception deleted from it.
        parts.append(f"\n{PARTIAL_DROP_DISCLOSURE}")

    cited = citations(turn)
    if not cited:
        # v7: an all-uncited turn (every admitted claim is reasoning/
        # conversation) has nothing to list -- a dangling "Sources:" header
        # with an empty body would be worse than omitting the trailer.
        # Unreachable under v5/v6 by the same construction as the marker
        # branch above: an admitted turn there always carries >= 1 pair.
        return "".join(parts)
    trailer = "\n".join(f"- {_marker(((c.short_name, c.page),))}" for c in cited)
    return "".join(parts) + "\n\nSources:\n" + trailer


DECLINE_GUARD_MATERIAL = "material_in_decline"
DECLINE_GUARD_SOURCE_ASSERTION = "source_assertion_in_decline"


def render_decline(turn: AdmittedTurn) -> tuple[str | None, str | None]:
    """(conversational decline text, guard reason). Exactly one is None.

    The v7 decline is MODEL text, so it re-crosses the gate boundary here
    rather than being trusted from the parser: the parser scanned its own
    pre-sanitization bytes, and ``kinds`` is caller-supplied. A hit on either
    lexicon returns ``(None, reason)`` and the caller serves the fixed refusal
    copy -- an uncited sentence that asserts what a source says, or that
    carries obligation wording, must never be served, and a decline is the
    one shape where nothing else on the turn would disclose it.

    Callers must check ``turn.verdict == VERDICT_CONVERSATIONAL_DECLINE`` first;
    by that verdict's construction no admitted claim carries pairs, so this
    never has to decide what to do with a marker.
    """
    for claim in turn.admitted:
        scan = frame_split(claim.text)[1] or claim.text
        if materiality_trigger(scan) is not None:
            return None, DECLINE_GUARD_MATERIAL
        if source_assertion_trigger(scan) is not None:
            return None, DECLINE_GUARD_SOURCE_ASSERTION

    sentences: list[str] = []
    for claim in turn.admitted:
        body = claim.text
        terminator = "."
        if body and body[-1] in ".!?":
            terminator = body[-1]
            body = body[:-1].rstrip()
        sentences.append(f"{body}{terminator}")
    # No marker, no unsupported tail, no PARTIAL disclosure, and no Sources:
    # trailer -- a decline states nothing about the corpus, so none of
    # render_answer's evidence-disclosure machinery applies.
    return " ".join(sentences), None


def ledger(
    turn: AdmittedTurn,
    *,
    model: str,
    prompt_version: str,
    renderer_version: int = RENDERER_VERSION,
    decline_guard: str | None = None,
) -> dict[str, Any]:
    """The operator-facing record of what the gate saw and what it decided.

    OD-5's operator half. Every identifier the owner named maps onto something
    this repo already has, rather than a new id:
      response_id            -> the query_log row this ledger is stored on
                                (route_json["turn"]); its id IS the audit_id.
      claim_id               -> the model's claim index.
      citation_id            -> the (short_name, page) pair that failed.
      validation_failure_rsn -> drop_reason.
      retrieval_run_id       -> retrieved_json on that same row.
      model / prompt_version -> carried here, mirroring route_json["prompt"].

    This is a strict forensic win over the branch it replaces: today an uncited
    answer is declined by writing the refusal text with citations=[], so the
    model's draft and its markers are recorded NOWHERE and only a count
    survives in the logs.

    ``renderer_version``/``decline_guard`` (v7) are CONDITIONAL keys, present
    only when they carry information: ``kind_counts`` only when
    ``renderer_version == RENDERER_VERSION_SELECTIVE`` (under v5/v6 every
    admitted claim is a cited source fact, so the counts are zero-information
    and emitting them would move every v5 row's persisted ledger bytes for
    nothing), and ``decline_guard`` only when not None. Both exist to make
    flag-off ledger bytes IDENTICAL to main's -- the golden byte-stability
    test pins exactly that.
    """
    payload: dict[str, Any] = {
        "renderer_version": renderer_version,
        "turn_type": turn.turn_type,
        "verdict": turn.verdict,
        "model": model,
        "prompt_version": prompt_version,
        "emitted": turn.emitted,
        "admitted": len(turn.admitted),
        "dropped": len(turn.dropped),
        "material_word": turn.material_word,
        "unsupported_kept": list(turn.unsupported),
        "unsupported_dropped": list(turn.dropped_unsupported),
        "claims": [
            {
                "index": claim.index,
                "admitted": True,
                "drop_reason": None,
                "text_prefix": claim.text[:_LEDGER_TEXT_CHARS],
                "cites": [f"{s},p.{p}" for s, p in claim.pairs],
                "bad_cites": [],
                "material_word": None,
                "passage_overlap": claim.overlap,
                "kind": claim.kind,
                "correction_method": claim.correction_method,
                "original_cites": (
                    None
                    if claim.original_cites is None
                    else [f"{s},p.{p}" for s, p in claim.original_cites]
                ),
                "downgraded": claim.downgraded,
            }
            for claim in turn.admitted
        ]
        + [
            {
                "index": claim.index,
                "admitted": False,
                "drop_reason": claim.reason,
                "text_prefix": claim.text[:_LEDGER_TEXT_CHARS],
                "cites": [f"{s},p.{p}" for s, p in claim.cites],
                "bad_cites": [f"{s},p.{p}" for s, p in claim.bad_cites],
                "material_word": claim.material_word,
                "passage_overlap": None,
                # A dropped claim was always an attempted source fact; only
                # correction_method varies (material_exempt when the guard
                # blocked correction/downgrade).
                "kind": CLAIM_KIND_SOURCE_FACT,
                "correction_method": claim.correction_method,
                "original_cites": None,
                "downgraded": False,
            }
            for claim in turn.dropped
        ],
    }
    if renderer_version == RENDERER_VERSION_SELECTIVE:
        counts: dict[str, int] = {}
        for claim in turn.admitted:
            counts[claim.kind] = counts.get(claim.kind, 0) + 1
        payload["kind_counts"] = counts
    if decline_guard is not None:
        payload["decline_guard"] = decline_guard
    return payload
