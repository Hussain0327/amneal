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

# Sentence-initial marker reattachment: "study is required. [1] Next" puts the
# marker AFTER the terminator, so the sentence split would orphan it onto the
# following sentence. Moving the group before the terminator pre-split keeps
# the marker bound to the sentence it actually cites. Numeric markers only: the
# pair grammar has no documented post-terminator placement to repair.
_MARKER_AFTER_PUNCT = re.compile(
    r"(?P<punct>[.!?])\s*(?P<group>(?:\[\s*\d+\s*(?:,\s*\d+\s*)*\]\s*)+)"
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
    if any(len(s) > PROSE_MAX_SENTENCE_CHARS for s in split_sentences(body)):
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
        claims.append(ParsedClaim(text=claim.text, cites=tuple(cites)))
    return tuple(claims)


def _reattach_markers(text: str) -> str:
    """Move a post-terminator numeric marker group before its terminator."""

    def repl(match: re.Match[str]) -> str:
        brackets = "".join(_BRACKET.findall(match.group("group")))
        return f" {brackets}{match.group('punct')} "

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


def _classify_uncited(text: str) -> ClaimKind:
    """Epistemic kind for a sentence with no trailing marker.

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


def _classify_uncited_selective(text: str) -> ClaimKind:
    """v7 epistemic kind for a sentence with no trailing marker (B.10.3.3).

    Reachable only when the caller passes ``selective=True`` (v6 stays on
    ``_classify_uncited``, byte-identical). Two lexicons, not one: a bald
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
    """
    frame, body = frame_split(text)
    scan = body if frame else text
    if materiality_trigger(scan) is not None or source_assertion_trigger(scan) is not None:
        return "source_fact"
    return "reasoning" if frame else "conversation"


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
    classify_uncited = _classify_uncited_selective if selective else _classify_uncited
    text = raw_text or ""
    if " ".join(text.split()) == PROSE_NO_EVIDENCE_SENTINEL:
        return ParsedProseTurn(turn_type="NO_EVIDENCE")

    sentences = split_sentences(_reattach_markers(text))

    # Truncation rule: an unterminated final sentence is a cut-off draft, not a
    # claim. It is dropped BEFORE marker extraction, and a material tail is
    # surfaced so the caller treats the parse like a material drop.
    truncated_material = False
    if sentences and sentences[-1].strip()[-1:] not in (".", "!", "?"):
        tail = sentences.pop()
        truncated_material = materiality_trigger(tail) is not None

    # First index wins for a duplicated (name, page) header, mirroring
    # allowed_passage_map's setdefault: the top-ranked chunk is the one the
    # model most plausibly echoed.
    pair_index: dict[tuple[str, int], int] = {}
    for position, passage in enumerate(passages):
        pair_index.setdefault((passage.short_name.upper(), passage.page), position)

    claims: list[ProseClaim] = []
    leftover: list[str] = []
    for sentence in sentences:
        flat = " ".join(sentence.split())
        match = _TRAILING_GROUP.match(flat)
        if match is None:
            if "[" in flat or "]" in flat:
                leftover.append(flat)
                continue
            claims.append(ProseClaim(text=flat, kind=classify_uncited(flat)))
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
            )
        )

    return ParsedProseTurn(
        turn_type="ANSWER",
        claims=claims,
        truncated_material=truncated_material,
        leftover_brackets=leftover,
    )
