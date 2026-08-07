"""Deterministic parser from prose synthesizer output to a gate-ready turn.

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

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from regwatch.common.citations import iter_psg_citations
from regwatch.common.sentences import split_sentences
from regwatch.generate.turn_gate import materiality_trigger
from regwatch.retrieve.retriever import RetrievedPassage

# The exact single-sentence completion the v6 prose prompt instructs the model
# to emit when the passages do not answer the question. Defined HERE as the
# single source of truth; the prompt and the echo provider must import it.
PROSE_NO_EVIDENCE_SENTINEL = "NO_EVIDENCE."

# Frame openers that mark an uncited sentence as declared REASONING rather than
# conversation. Matched whitespace/case-normalized, prefix-only: a frame buried
# mid-sentence is not a declaration. Deliberately short -- an opener earns its
# place here, it is not guessed -- because the materiality guard below, not this
# list, is what stops a frame from laundering a factual claim (finding F3).
REASONING_FRAME_PREFIXES: tuple[str, ...] = (
    "the guidance does not state this directly",
    "reading the guidance together",
    "my reading is",
    "beyond the guidance,",
)

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


def gate_payload(parsed: ParsedProseTurn, passages: list[RetrievedPassage]) -> str:
    """Serialize a parsed prose turn into the gate's claims-JSON contract.

    One admission loop for both formats: the parser resolved [n] markers into
    passage positions; this bridge rewrites them as the (short_name, page)
    pairs the gate validates against THIS turn's passages. A numeric marker the
    parser carried as declared-but-unresolvable becomes a deliberately unknown
    cite (UNRESOLVED_<n>) so the gate still sees an ASSERTED source fact on its
    drop-or-correct path -- never uncited conversation. Parser-classified
    reasoning/conversation sentences bridge with zero cites: under v6's
    unchanged refuse-or-cite policy they land on DROP_NO_CITES exactly as a v5
    zero-cite claim would. Still serialization, not admission: the gate remains
    the only judge of what renders.
    """
    claims: list[dict[str, object]] = []
    for claim in parsed.claims:
        cites: list[dict[str, object]] = [
            {"short_name": passages[i].short_name, "page": passages[i].page}
            for i in claim.cite_indices
        ]
        unresolved_seen: set[str] = set()
        for token in claim.raw_markers:
            if not token.isdigit() or token in unresolved_seen:
                continue
            if 1 <= int(token) <= len(passages):
                continue
            unresolved_seen.add(token)
            cites.append({"short_name": f"UNRESOLVED_{token}", "page": 1})
        claims.append({"text": claim.text, "cites": cites})
    return json.dumps({"turn_type": parsed.turn_type, "claims": claims, "unsupported": []})


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


def parse(raw_text: str, *, passages: list[RetrievedPassage]) -> ParsedProseTurn:
    """Parse one prose completion against the passages sent this turn.

    ``passages`` is the ORDERED list the model was shown; marker [n] is 1-based
    into it. The parser never invents, repairs, or reorders a citation: it
    resolves what the model declared, and everything it cannot resolve is
    either carried as declared-but-unresolvable (numeric) or dropped with its
    sentence (everything else).
    """
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
            claims.append(ProseClaim(text=flat, kind=_classify_uncited(flat)))
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
