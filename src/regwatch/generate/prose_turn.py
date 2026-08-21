"""Deterministic parser from prose synthesizer output to a gate-ready turn.

Last updated: 2026-08-11. ``selective=True`` is the v7 path and it is what prod
runs; ``selective=False`` is the older v6 path, kept reachable by the flag.

TRUST MODEL. Every byte this module reads is untrusted model output: a [n]
marker is a CLAIM about evidence, not evidence. This module only parses, and it
parses deterministically -- no provider, no DB, no settings, no randomness. The
gate (``turn_gate``) remains the reliability boundary that validates what the
parser declared; nothing here admits, renders, or corrects anything.

Because the parser cannot verify, every ambiguity resolves in the SAFE
direction: a bracket that is not unambiguously a sentence-trailing citation is
never consumed as one, and the sentence carrying it is dropped rather than
rendered with a marker of uncertain meaning (finding F4: an in-range numeric
bracket quoted from source or user text must not resolve as a valid-but-wrong
citation).

The output mirrors the ``GroundedTurn``/``Claim`` shapes (``turn_schema``): a
turn type plus per-sentence claims, with ``cite_indices`` standing in for the
resolved ``(short_name, page)`` pairs -- position ``i`` names ``passages[i]``
of the ordered list the model was shown, so a later caller can build
``ClaimCite`` pairs from validated passages, never from model text.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from regwatch.common.blocks import (
    LINE_TERMINATED_KINDS,
    PARAGRAPH,
    Block,
    Unit,
    is_label,
    segment,
    split_units,
)
from regwatch.common.citations import iter_psg_citations
from regwatch.common.sentences import split_sentences

# Re-exported: parse's public vocabulary. Aliased to itself so mypy's strict
# no-implicit-reexport treats this as an intentional re-export, not an unused
# import -- every existing `prose_turn.REASONING_FRAME_PREFIXES` reference
# must keep resolving after the B.10.3.2 move to turn_gate.
from regwatch.generate.turn_gate import REASONING_FRAME_PREFIXES as REASONING_FRAME_PREFIXES
from regwatch.generate.turn_gate import (
    ParsedClaim,
    frame_split,
    materiality_trigger,
    source_assertion_trigger,
)
from regwatch.retrieve.retriever import RetrievedPassage

# V6 ONLY. The exact single-sentence completion the v6 prose prompt tells the
# model to emit when the passages do not answer the question. Defined HERE as
# the single source of truth; the v6 prompt and the echo provider must match it.
#
# v7 has NO sentinel and no code word: the model says "I do not have that" in
# ordinary words and the gate re-scans that text before it is served. This
# constant stays because v6 is still reachable with the flag off, and because
# the streaming path holds deltas back while the text is still a prefix of it.
PROSE_NO_EVIDENCE_SENTINEL = "NO_EVIDENCE."

# One numeric marker bracket body: "1", "1, 2". Digits only, so the pair
# grammar ([PSG_..., p.N]) and prose brackets ("[see appendix]") never match.
_NUMERIC_BODY = re.compile(r"^\s*\d+\s*(?:,\s*\d+\s*)*$")

_BRACKET = re.compile(r"\[[^\[\]]*\]")
_BRACKET_BODY = re.compile(r"\[([^\[\]]*)\]")

# POSITION RULE (finding F4): a citation is a run of brackets immediately
# before the sentence's terminal punctuation, and nothing else. ``body`` is
# lazy so the group claims the longest bracket run that still touches the
# terminator; any bracket left in ``body`` afterwards is by construction
# mid-sentence and falls to the leftover-bracket kill.
_TRAILING_GROUP = re.compile(
    r"^(?P<body>.*?)(?P<group>(?:\s*\[[^\[\]]*\])+)\s*(?P<punct>[.!?])$", re.DOTALL
)
# 2026-08-21: a heading, a bullet or a table cell is terminated by its LINE (or
# cell) end, not by a period -- "| Yes [1] |" and "- SAC (B/M/E) [1]" have no
# terminator to put the marker before. Same position rule (the run of brackets
# must touch the end of the unit), optional punctuation. Used ONLY for units
# whose block kind is in LINE_TERMINATED_KINDS; a paragraph sentence keeps the
# strict grammar, so F4 is unchanged there.
_TRAILING_GROUP_LINE = re.compile(
    r"^(?P<body>.*?)(?P<group>(?:\s*\[[^\[\]]*\])+)\s*(?P<punct>[.!?]?)$", re.DOTALL
)

# Sentence-initial marker reattachment: "study is required. [1] Next" puts the
# marker AFTER the terminator, so the sentence split would orphan it onto the
# following sentence. Moving the group before the terminator pre-split keeps
# the marker bound to the sentence it actually cites. Numeric markers only: the
# pair grammar has no documented post-terminator placement to repair.
# Horizontal whitespace only between the terminator and the group, and the
# trailing run is CAPTURED rather than swallowed: since 2026-08-21 this runs
# on the whole raw text before block segmentation, and a newline after the
# marker is the boundary to the next heading/bullet/table line. Eating it
# (the old ``\s*``) re-welded "required. [1]\n## Heading" into one paragraph
# -- the exact defect the block layer exists to remove (review finding).
_MARKER_AFTER_PUNCT = re.compile(
    r"(?P<punct>[.!?])[^\S\n]*"
    r"(?P<group>(?:\[\s*\d+\s*(?:,\s*\d+\s*)*\][^\S\n]*)+)"
    r"(?P<tail>\s*)"
)

ClaimKind = Literal["source_fact", "reasoning", "conversation"]


class ProseClaim(BaseModel):
    """One parsed sentence: the prose analogue of ``turn_schema.Claim``.

    ``cite_indices`` holds the RESOLVED 0-based positions into the passage list
    (marker ``n`` resolves to ``passages[n - 1]``), deduplicated in declaration
    order. ``raw_markers`` holds every marker token as the model wrote it
    ("1", "7", "PSG_020503, p.3"), so a marker that resolved nowhere is still
    visible: ``raw_markers`` without a matching index is a declared-but-
    unresolvable cite the gate can drop or correct.
    """

    model_config = ConfigDict(extra="forbid")

    text: str
    kind: ClaimKind
    cite_indices: list[int] = Field(default_factory=list)
    raw_markers: list[str] = Field(default_factory=list)
    # Which markdown container the sentence came from (common.blocks). The
    # default is the legacy "one flat paragraph", so every v6 consumer and
    # every persisted shape that predates the field reads unchanged.
    block: Block = Field(default=PARAGRAPH)


class ParsedProseTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    turn_type: Literal["ANSWER", "NO_EVIDENCE"]
    claims: list[ProseClaim] = Field(default_factory=list)
    # True when a dropped unterminated tail tripped the materiality lexicon:
    # the caller must treat the parse like a material drop, because the
    # truncation may have severed a qualifier from the answer that survives.
    truncated_material: bool = False
    # The ORIGINAL text of every bracket-killed sentence (mid-sentence or
    # non-citation bracket, fabricated pair echo). Full sentences, not bracket
    # fragments, so the caller can run the materiality check on what was
    # removed instead of trusting that the kill was benign.
    leftover_brackets: list[str] = Field(default_factory=list)


# Pathological-output bounds (issue #183).
#
# Since #183 the prose arms carry NO length bound: turn_gate.admit_claims never
# inspects length, and the v5 claims-JSON caps (text 400 / cites 4 / claims 20)
# are unreachable from this path by design -- re-imposing them is what killed
# valid long sentences in production. These two bounds exist ONLY to stop a
# degenerate completion (a repetition loop, a sentence that never terminates)
# from rendering unbounded text to an analyst. They are not a style rule.
#
# Sized against measurement, not taste. The 62-row v7 gold run (2026-08-11)
# produced a longest sentence of 488 chars and a longest answer of 1,823, so
# both bounds sit roughly 4x above real output and neither fired on any row.
# The total ceiling also lands near 20 sentences x 400 chars, which matches the
# 21-claim production answer recorded in #183 -- two independent routes to the
# same number.
PROSE_MAX_SENTENCE_CHARS = 2000
PROSE_MAX_ANSWER_CHARS = 8000


def bounds_exceeded(text: str) -> str | None:
    """Which pathological-output bound this completion breached, if any.

    Args:
        text: The raw synthesizer completion.

    Returns:
        ``"sentence_too_long"``, ``"answer_too_long"``, or None when the
        completion is within both bounds. The sentence fault is reported first:
        a repair instruction has to name ONE concrete fault, and "this sentence
        ran too long" is actionable in a way that "your answer was long" is not.

    Pure: no I/O, no settings. Splitting uses the same splitter ``parse`` uses,
    so a sentence measured here is the sentence the parser will emit.
    """
    body = text or ""
    # Block-aware units, not bare sentences: a table has no terminal
    # punctuation, so the sentence splitter read a whole 6x4 matrix as ONE
    # sentence and a legitimate answer could breach the per-sentence bound.
    if any(len(s) > PROSE_MAX_SENTENCE_CHARS for s in split_units(body)):
        return "sentence_too_long"
    if len(body) > PROSE_MAX_ANSWER_CHARS:
        return "answer_too_long"
    return None


def to_claims(parsed: ParsedProseTurn, passages: list[RetrievedPassage]) -> tuple[ParsedClaim, ...]:
    """Bridge a parsed prose turn into the gate's admission input.

    One admission loop for both formats: the parser resolved [n] markers into
    passage positions; this bridge rewrites them as the (short_name, page)
    pairs the gate validates against THIS turn's passages. A numeric marker the
    parser carried as declared-but-unresolvable becomes a deliberately unknown
    cite (UNRESOLVED_<n>) so the gate still sees an ASSERTED source fact on its
    drop-or-correct path, never uncited conversation. Parser-classified
    reasoning/conversation sentences bridge with zero cites, and what happens to
    them next is the gate's call, not this function's: under v6 they land on
    DROP_NO_CITES exactly as a v5 zero-cite claim would, while under v7
    (``selective=True``) the gate admits them uncited. Still translation, not
    admission.

    ``passages`` MUST be the same list handed to ``turn_gate.admit_claims`` --
    marker n is resolved positionally against it, so a different list here
    silently sources sentences to the wrong document (guarded by
    tests/test_prose_synthesis.py::test_marker_resolves_to_the_passage_shown_under_that_number).

    Replaced ``gate_payload``, which serialized this same result back into the
    v5 claims-JSON contract only for ``admit_turn`` to re-parse it. That round
    trip re-imposed the v5 schema's caps (text 400 chars, cites 4, claims 20)
    on our own sentence splitter's output, failing good answers as
    ``malformed_structure`` -- issue #183.
    """
    claims: list[ParsedClaim] = []
    for claim in parsed.claims:
        cites: list[tuple[str, int]] = [
            (passages[i].short_name, passages[i].page) for i in claim.cite_indices
        ]
        unresolved_seen: set[str] = set()
        for token in claim.raw_markers:
            if not token.isdigit() or token in unresolved_seen:
                continue
            if 1 <= int(token) <= len(passages):
                continue
            unresolved_seen.add(token)
            cites.append((f"UNRESOLVED_{token}", 1))
        claims.append(ParsedClaim(text=claim.text, cites=tuple(cites), block=claim.block))
    return tuple(claims)


def _reattach_markers(text: str) -> str:
    """Move a post-terminator numeric marker group before its terminator."""

    def repl(match: re.Match[str]) -> str:
        brackets = "".join(_BRACKET.findall(match.group("group")))
        tail = match.group("tail")
        # Keep a line break that followed the group; collapse anything else
        # to the single space the sentence splitter expects.
        return f" {brackets}{match.group('punct')}" + (tail if "\n" in tail else " ")

    return _MARKER_AFTER_PUNCT.sub(repl, text)


def _resolve_group(
    group: str,
    passages: list[RetrievedPassage],
    pair_index: dict[tuple[str, int], int],
) -> tuple[list[int], list[str]] | None:
    """Resolve one trailing bracket group, or None when it kills the sentence.

    Numeric markers resolve 1-based into the passage list; an out-of-range n
    stays declared (raw marker) but unresolved, so the gate sees an unknown
    citation rather than a silently repaired one. A pair-shaped echo must match
    a passage header sent this turn -- pair grammar carries a stated source
    identity, so a miss is a fabrication and kills the sentence, while a
    non-citation bracket simply is not a marker and kills it too.
    """
    cite_indices: list[int] = []
    raw_markers: list[str] = []
    for body in _BRACKET_BODY.findall(group):
        if _NUMERIC_BODY.match(body):
            for token in body.split(","):
                raw_markers.append(token.strip())
                n = int(token)
                if 1 <= n <= len(passages):
                    cite_indices.append(n - 1)
            continue
        pairs = list(iter_psg_citations(f"[{body}]"))
        if not pairs:
            return None
        for short_name, page in pairs:
            raw_markers.append(f"{short_name}, p.{page}")
            index = pair_index.get((short_name.upper(), page))
            if index is None:
                return None
            cite_indices.append(index)
    deduped: list[int] = []
    for index in cite_indices:
        if index not in deduped:
            deduped.append(index)
    return deduped, raw_markers


def _classify_uncited_legacy(text: str, block: Block = PARAGRAPH) -> ClaimKind:
    """v6 epistemic kind for a sentence with no trailing marker.

    ``block`` is accepted for signature parity with the selective classifier
    and deliberately ignored: v6 forbids markdown and its bytes are pinned.

    MATERIALITY GUARD (finding F3): the lexicon runs on reasoning AND
    conversation sentences, because a MODEL-authored frame must not launder a
    material FDA claim into an uncited channel. A hit reclassifies the sentence
    as an unsupported source fact -- zero cites -- putting it on the gate's
    correct-or-drop path. Frames the GATE itself authors later are its own
    concern; this parser exempts nothing.
    """
    if materiality_trigger(text) is not None:
        return "source_fact"
    if text.lower().startswith(REASONING_FRAME_PREFIXES):
        return "reasoning"
    return "conversation"


# 2026-08-20. "There is no dissolution requirement" ASSERTS something about the
# world; "I don't see a fed requirement in the PSG we pulled" OBSERVES something
# about the evidence. The lexicons cannot tell them apart -- both contain "no"
# and "requirement" -- so the whole difference is grammatical person. Without
# this, ordinary colleague hedging is silently dropped as an uncited source
# fact, which is the single most common way this gate damages the product.
_FIRST_PERSON_OBSERVATION_RE = re.compile(
    r"(?:\bi\s+don'?t\s+see|i\s+do\s+not\s+see|i'?m\s+not\s+seeing"
    r"|i\s+have\s+no|i'?d\b|i\s+would\b|i\s+wouldn'?t\b"
    r"|my\s+read(?:ing)?\b|\bi'?d\s+need\b|\bi\s+need\b)",
    re.IGNORECASE,
)

# ...but a first-person opener must never become a smuggling channel. If the
# sentence still attributes something to a named source, the exemption is void.
_ATTRIBUTED_ASSERTION_RE = re.compile(
    r"\b(?:fda|the\s+agency|the\s+guidance|the\s+psg|cfr)\b[^.]{0,24}?\b"
    r"(?:requires?|recommends?|permits?|prohibits?|states?|says?|specifies?"
    r"|mandates?|allows?|advises?|expects?|instructs?)\b",
    re.IGNORECASE,
)


# A hedge may soften a CLAIM but it does not make it an observation. "I'd say a
# fed study is required" asserts a requirement; "I don't see a fed requirement"
# reports what the evidence shows. The difference is a deontic PREDICATE -- a
# copula plus a regulatory participle, or a bare modal -- so the exemption is
# void whenever one is present, named source or not.
_DEONTIC_PREDICATE_RE = re.compile(
    r"\b(?:must|shall)\b"
    r"|\b(?:is|are|was|were|be|been|being)\s+(?:not\s+)?"
    r"(?:required|permitted|prohibited|approved|exempt|exempted|waived"
    r"|acceptable|expected|recommended|allowed|mandated|needed)\b",
    re.IGNORECASE,
)


def _is_first_person_observation(text: str) -> bool:
    """True when ``text`` reports the author's read, not a source's content."""
    body = _despan(text)
    if _ATTRIBUTED_ASSERTION_RE.search(body):
        return False
    if _DEONTIC_PREDICATE_RE.search(body):
        return False
    return _FIRST_PERSON_OBSERVATION_RE.search(body) is not None


# "The provided passage does not state any waiver conditions" reports what THIS
# turn's evidence contains -- the single most useful thing a colleague can say
# about a gap. "The guidance does not require a fed study" is a proposition
# about FDA and stays a source fact. The whole difference is evidence deixis, so
# match only explicit references to the material in front of us.
_EVIDENCE_DEIXIS_RE = re.compile(
    r"\b(?:the\s+)?(?:provided|supplied|retrieved|given|above|these|this)\s+"
    r"(?:passages?|excerpts?|documents?|texts?|sections?|material)\b"
    r"|\bwhat\s+(?:i\s+have|was\s+retrieved|we\s+pulled|you\s+gave\s+me)\b"
    r"|\bpassages?\s+(?:i|we)\s+(?:have|pulled|retrieved|got)\b",
    re.IGNORECASE,
)
_ABSENCE_REPORT_RE = re.compile(
    r"\b(?:does\s+not|do\s+not|doesn'?t|don'?t|did\s+not|didn'?t)\s+"
    r"(?:state|say|mention|cover|address|include|contain|specify|list)\b"
    r"|\bcontains?\s+no\b|\blacks?\b|\bis\s+silent\b|\bsays?\s+nothing\b"
    r"|\bmakes?\s+no\s+mention\b|\bno\s+mention\s+of\b",
    re.IGNORECASE,
)


# Presentation is a feature now, so the model legitimately writes "does **not**
# state". Every lexicon and epistemic regex below matches on WORDS, and markdown
# emphasis splices punctuation into the middle of them -- "does **not** state"
# silently failed the absence-report match and the sentence was dropped as an
# uncited source fact. Normalise emphasis away for CLASSIFICATION ONLY; the
# rendered text the user sees is untouched.
_EMPHASIS_RE = re.compile(r"(?:\*{1,3}|_{1,3}|`)")


def _despan(text: str) -> str:
    """Strip markdown emphasis so word-boundary matching sees whole words."""
    return _EMPHASIS_RE.sub("", text or "")


def _is_evidence_observation(text: str) -> bool:
    """True when ``text`` reports what THIS turn's passages do or do not contain."""
    body = _despan(text)
    if _ATTRIBUTED_ASSERTION_RE.search(body) or _DEONTIC_PREDICATE_RE.search(body):
        return False
    return bool(_EVIDENCE_DEIXIS_RE.search(body) and _ABSENCE_REPORT_RE.search(body))


def _classify_uncited_selective(text: str, block: Block = PARAGRAPH) -> ClaimKind:
    """v7 epistemic kind for a sentence with no trailing marker (B.10.3.3).

    Reachable only when the caller passes ``selective=True`` (v6 stays on
    ``_classify_uncited_legacy``, byte-identical). Two lexicons, not one: a bald
    obligation/permission word (MATERIALITY_WORDS) OR an attribution verb
    reporting what a source SAYS (SOURCE_ASSERTION_WORDS) reclassifies the
    sentence back to an unsupported source_fact, on the gate's drop-or-correct
    path -- v7 admits uncited reasoning/conversation, so this is what stops an
    uncited FDA assertion from reaching that channel (P1). The scan runs on
    the FRAME-STRIPPED body when the sentence is framed, because the frame
    itself is allowlisted, content-free hedge text that can carry a
    materiality word ("does NOT state this directly") without asserting
    anything (P0/F3) -- scanning it whole would fail the recommended frame
    100% of the time.

    2026-08-21: for a LABEL (heading, table header or row label -- see
    ``blocks.is_label``) the attribution scan masks ``turn_gate.
    LABEL_TOPIC_WORDS`` only ("Recommended BE design" is what every PSG
    heading is called); "Exempt", "waived", "FDA recommends ..." still fire
    on a label. The materiality lexicon is never masked.
    ``turn_gate.render_answer``'s render-time scan mirrors this exactly.
    """
    frame, body = frame_split(text)
    scan = _despan(body if frame else text)
    if _is_first_person_observation(text) or _is_evidence_observation(text):
        return "reasoning" if frame else "conversation"
    if materiality_trigger(scan) is not None:
        return "source_fact"
    if source_assertion_trigger(scan, label=is_label(block)) is not None:
        return "source_fact"
    return "reasoning" if frame else "conversation"


def _units(text: str, *, selective: bool) -> list[Unit]:
    """The claim-sized units of a completion, in document order.

    v7 (``selective=True``) reads the markdown shape first (``common.blocks``):
    a heading is its own unit instead of being welded onto the sentence below
    it, each list item and table cell is a unit, and the block tag rides along
    so the renderer can rebuild the structure.

    Marker reattachment runs on the WHOLE text before segmentation, exactly
    as v6 does: ``segment`` sentence-splits inside each paragraph/item, so a
    post-terminator "[1]" must already sit before its period by then or the
    split orphans it (review finding, 2026-08-21). The regex cannot cross a
    block boundary in practice -- a list marker, a pipe or a heading hash
    always intervenes between one block's terminator and the next block's
    bracket -- so the marker never migrates into another container.

    v6 (``selective=False``) keeps the flat sentence split byte-for-byte --
    every unit carries the default PARAGRAPH block -- because the v6 prompt
    forbids markdown and its ledger bytes are pinned.
    """
    reattached = _reattach_markers(text)
    if not selective:
        return [Unit(sentence, PARAGRAPH) for sentence in split_sentences(reattached)]
    return segment(reattached)


def parse(
    raw_text: str, *, passages: list[RetrievedPassage], selective: bool = False
) -> ParsedProseTurn:
    """Parse one prose completion against the passages sent this turn.

    ``passages`` is the ORDERED list the model was shown; marker [n] is 1-based
    into it. The parser never invents, repairs, or reorders a citation: it
    resolves what the model declared, and everything it cannot resolve is
    either carried as declared-but-unresolvable (numeric) or dropped with its
    sentence (everything else).

    ``selective=False`` (every v5/v6 caller) keeps today's classification
    exactly; ``selective=True`` (v7) is the only way to reach
    ``_classify_uncited_selective``.
    """
    classify_uncited = _classify_uncited_selective if selective else _classify_uncited_legacy
    text = raw_text or ""
    if " ".join(text.split()) == PROSE_NO_EVIDENCE_SENTINEL:
        return ParsedProseTurn(turn_type="NO_EVIDENCE")

    units = _units(text, selective=selective)

    # Truncation rule: an unterminated final sentence is a cut-off draft, not a
    # claim. It is dropped BEFORE marker extraction, and a material tail is
    # surfaced so the caller treats the parse like a material drop. A heading,
    # bullet or cell ends with its line, so only a PARAGRAPH tail can be
    # unterminated.
    truncated_material = False
    if (
        units
        and units[-1].block.kind not in LINE_TERMINATED_KINDS
        and units[-1].text.strip()[-1:] not in (".", "!", "?")
    ):
        tail = units.pop().text
        truncated_material = materiality_trigger(tail) is not None

    # First index wins for a duplicated (name, page) header, mirroring
    # allowed_passage_map's setdefault: the top-ranked chunk is the one the
    # model most plausibly echoed.
    pair_index: dict[tuple[str, int], int] = {}
    for position, passage in enumerate(passages):
        pair_index.setdefault((passage.short_name.upper(), passage.page), position)

    claims: list[ProseClaim] = []
    leftover: list[str] = []
    for unit in units:
        flat = " ".join(unit.text.split())
        block = unit.block
        grammar = _TRAILING_GROUP_LINE if block.kind in LINE_TERMINATED_KINDS else _TRAILING_GROUP
        match = grammar.match(flat)
        if match is None:
            if "[" in flat or "]" in flat:
                leftover.append(flat)
                continue
            claims.append(ProseClaim(text=flat, kind=classify_uncited(flat, block), block=block))
            continue

        resolved = _resolve_group(match.group("group"), passages, pair_index)
        body = match.group("body").strip()
        # MARKER SCOPE: the group binds only this sentence; a killed sentence
        # takes its markers down with it rather than donating them elsewhere.
        if resolved is None or not body or "[" in body or "]" in body:
            leftover.append(flat)
            continue
        cite_indices, raw_markers = resolved
        claims.append(
            ProseClaim(
                # A trailing marker group is a citation declaration whether or
                # not it resolved: an out-of-range [n] still means the model
                # asserted a source fact, and the gate must see it as one
                # (drop-or-correct), never as uncited conversation.
                text=f"{body}{match.group('punct')}",
                kind="source_fact",
                cite_indices=cite_indices,
                raw_markers=raw_markers,
                block=block,
            )
        )

    return ParsedProseTurn(
        turn_type="ANSWER",
        claims=claims,
        truncated_material=truncated_material,
        leftover_brackets=leftover,
    )
