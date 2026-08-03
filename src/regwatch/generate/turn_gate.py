"""Claim-level admission gate for the structured synthesizer turn.

This module is the reliability boundary: it is the ONLY place model-authored
bytes can become user-visible text, and it admits them one claim at a time.

Pure. No DB, no settings, no provider. Input: the raw completion text, the
passages that were actually sent this turn, and the question. Output: an
``AdmittedTurn`` (what the caller may render) or a ``GateFailure`` (the payload
did not parse). The caller decides which decline branch a verdict maps to.

WHY THIS REPLACES THE PROSE SEGMENT SPLITTER
The old gate split the model's prose on sentence/newline boundaries and refused
the WHOLE TURN if any segment lacked a citation marker. A model that answers
correctly but places its citations as a trailing bibliography therefore had
every content sentence read as uncited -- a citation PLACEMENT bug that refused
three of three interactive production queries. Here the model never writes a
marker at all: it declares (short_name, page) per claim, and the renderer writes
canonical markers from validated passages.

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
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from regwatch.common.citations import strip_all_citations
from regwatch.common.logging import get_logger
from regwatch.common.structured_json import extract_json_blob
from regwatch.generate.rag_contract import Citation
from regwatch.generate.turn_schema import GroundedTurn
from regwatch.retrieve.retriever import RetrievedPassage

log = get_logger(__name__)

# Bumped whenever the rendered answer's SHAPE changes, so a stored answer can be
# read back against the renderer that produced it.
RENDERER_VERSION = 1

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


def materiality_trigger(claim_text: str) -> str | None:
    """The word that makes dropping ``claim_text`` MATERIAL, or None.

    The single materiality predicate: truthiness is the decision, and the
    returned word is what gets logged. Pure and case-insensitive, matching on
    word boundaries only.
    """
    match = _MATERIALITY_RE.search(claim_text or "")
    return match.group(0).lower() if match is not None else None


# ---------------------------------------------------------------------------
# Verdicts -- what the caller must do with an admitted turn.
# ---------------------------------------------------------------------------
VERDICT_ANSWER = "answer"  # every emitted claim was admitted
VERDICT_PARTIAL = "partial"  # some dropped, immaterial -> render + disclose
VERDICT_MATERIAL_DROP = "material_drop"  # some dropped, material -> reject whole answer
VERDICT_NO_VALID_CITATIONS = "no_valid_citations"  # nothing admitted
VERDICT_NO_EVIDENCE = "no_evidence"  # the model declined

# Drop reasons. These are OPERATOR strings (the route_json ledger and logs); no
# drop reason ever reaches a user.
DROP_EMPTY = "empty_text"
DROP_MARKUP = "markup_in_text"
DROP_MULTI_SENTENCE = "multi_sentence"
DROP_NO_CITES = "no_cites"
DROP_UNKNOWN_CITATION = "unknown_citation"

# ---------------------------------------------------------------------------
# OD-5: the two user-visible disclosure strings. Plain language, no
# implementation detail -- a user must never read "claim 3 failed citation
# validation". Operator detail lives in the ledger.
# ---------------------------------------------------------------------------
PARTIAL_DROP_DISCLOSURE = (
    "Some statements were omitted because their supporting citations " "could not be verified."
)
MATERIAL_DROP_TEXT = "I could not produce a fully supported answer from the available evidence."

# Retrieval-sufficiency disclosure. Unchanged wording: the eval set, the prompt
# eval and tests/test_grounded_qa_citations.py all pin this exact prefix.
PARTIAL_EVIDENCE_PREFIX = "Evidence not found in the supplied passages for:"
_MAX_UNSUPPORTED_LABELS_LEN = 160
_UNSUPPORTED_LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 /,()'&-]*")

# The SAME sentence split eval/metrics.py uses for faithfulness. Sharing it is
# what makes "one claim = one rendered sentence" and "faithfulness == 1.0 for an
# all-admitted turn" the same statement.
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")

# Structural markdown a claim slot may never carry: a heading, a link, or a bare
# URL. The answer is rendered through a markdown component with GFM autolinking,
# so any of these would render as chrome or as a clickable off-corpus pointer
# sitting beside real citation stamps.
_MARKUP_RE = re.compile(r"(?:^\s*#)|(?:\]\()|(?:\bhttps?://)|(?:\bwww\.)", re.IGNORECASE)
_LEADING_BULLET_RE = re.compile(r"^[-*+]\s+")
_WORD_RE = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class AdmittedClaim:
    index: int  # the model's claim index, so the ledger lines up with the draft
    text: str  # sanitized: one line, one sentence, no model-authored markers
    pairs: tuple[tuple[str, int], ...]  # canonical (SHORT_NAME, page), declaration order
    citations: tuple[Citation, ...]
    overlap: float  # claim/passage token overlap -- logged, NOT enforced


@dataclass(frozen=True)
class DroppedClaim:
    index: int
    text: str
    cites: tuple[tuple[str, int], ...]
    bad_cites: tuple[tuple[str, int], ...]
    reason: str
    material_word: str | None


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
    )


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
    return len([s for s in _SENT_SPLIT.split(text) if s.strip()])


def _tokens(text: str) -> set[str]:
    return {t for t in _WORD_RE.findall(text.lower()) if len(t) > 2}


def _overlap(text: str, passages: list[RetrievedPassage]) -> float:
    """Claim/passage token overlap, for calibration only.

    Citation validation is MECHANICAL: a claim can cite a real, retrieved
    passage that does not support it. That was true of the old gate too. Logging
    the overlap is what lets a floor be calibrated from real traffic later
    instead of guessed now.
    """
    claim_tokens = _tokens(text)
    if not claim_tokens:
        return 0.0
    passage_tokens: set[str] = set()
    for p in passages:
        passage_tokens |= _tokens(p.text)
    return round(len(claim_tokens & passage_tokens) / len(claim_tokens), 3)


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
) -> AdmittedTurn | GateFailure:
    """Parse one structured completion and admit its claims.

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

    if turn.turn_type == "NO_EVIDENCE":
        # Claims and unsupported labels are discarded WHOLESALE: a model that
        # declines has, by its own account, nothing to cite, so anything it put
        # in a claim slot is unvetted by definition.
        if turn.claims or turn.unsupported:
            log.warning(
                "qa_claims_on_no_evidence",
                claims=len(turn.claims),
                unsupported=len(turn.unsupported),
            )
        return AdmittedTurn(
            turn_type="NO_EVIDENCE",
            verdict=VERDICT_NO_EVIDENCE,
            admitted=(),
            dropped=(),
            unsupported=(),
            dropped_unsupported=(),
            emitted=len(turn.claims),
            material_word=None,
        )

    allowed = allowed_passage_map(passages)
    admitted: list[AdmittedClaim] = []
    dropped: list[DroppedClaim] = []

    for index, claim in enumerate(turn.claims):
        declared = tuple((c.short_name, c.page) for c in claim.cites)
        text = _sanitize_claim_text(claim.text)
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
            reason = DROP_NO_CITES
        else:
            bad = tuple((s, p) for (s, p) in declared if (s.upper(), p) not in allowed)
            if bad:
                reason = DROP_UNKNOWN_CITATION

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
            )
        )

    kept_labels, rejected_labels = _keep_unsupported(list(turn.unsupported), question)

    if not admitted:
        # Zero admitted is NOT a no-evidence turn even when the model emitted
        # claims: telling the user the corpus does not cover the question when
        # the truth is that every citation failed validation is a false
        # statement, and it hides a model-quality regression from the /metrics
        # rollup (which groups on mode+refused, not on status).
        verdict = VERDICT_NO_VALID_CITATIONS
        material_word = None
        kept_labels = []
    elif not dropped:
        verdict = VERDICT_ANSWER
        material_word = None
    else:
        material_word = next((d.material_word for d in dropped if d.material_word), None)
        verdict = VERDICT_MATERIAL_DROP if material_word else VERDICT_PARTIAL

    return AdmittedTurn(
        turn_type="ANSWER",
        verdict=verdict,
        admitted=tuple(admitted),
        dropped=tuple(dropped),
        unsupported=tuple(kept_labels),
        dropped_unsupported=tuple(rejected_labels),
        emitted=len(turn.claims),
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
    """
    sentences: list[str] = []
    for claim in turn.admitted:
        body = claim.text
        terminator = "."
        if body and body[-1] in ".!?":
            terminator = body[-1]
            body = body[:-1].rstrip()
        sentences.append(f"{body} {_marker(claim.pairs)}{terminator}")
    if not sentences:
        return ""

    parts = [" ".join(sentences)]
    if turn.unsupported:
        parts.append(f"\n{PARTIAL_EVIDENCE_PREFIX} {', '.join(turn.unsupported)}.")
    if turn.verdict == VERDICT_PARTIAL:
        # OD-5: the user is told that something was removed, in plain language
        # and with no implementation detail. Silence would hand back a
        # confident, fully-cited answer with an exception deleted from it.
        parts.append(f"\n{PARTIAL_DROP_DISCLOSURE}")

    trailer = "\n".join(f"- {_marker(((c.short_name, c.page),))}" for c in citations(turn))
    return "".join(parts) + "\n\nSources:\n" + trailer


def ledger(turn: AdmittedTurn, *, model: str, prompt_version: str) -> dict[str, Any]:
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
    """
    return {
        "renderer_version": RENDERER_VERSION,
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
                "text_prefix": claim.text[:200],
                "cites": [f"{s},p.{p}" for s, p in claim.pairs],
                "bad_cites": [],
                "material_word": None,
                "passage_overlap": claim.overlap,
            }
            for claim in turn.admitted
        ]
        + [
            {
                "index": claim.index,
                "admitted": False,
                "drop_reason": claim.reason,
                "text_prefix": claim.text[:200],
                "cites": [f"{s},p.{p}" for s, p in claim.cites],
                "bad_cites": [f"{s},p.{p}" for s, p in claim.bad_cites],
                "material_word": claim.material_word,
                "passage_overlap": None,
            }
            for claim in turn.dropped
        ],
    }
