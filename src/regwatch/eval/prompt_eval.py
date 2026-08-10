"""Synthetic, role-specific prompt evaluation for RegWatch generation tasks.

Dataset validation is offline and deterministic. ``run`` is explicitly
provider-backed and therefore opt-in; it never runs as part of ordinary tests.
Artifacts bind results to exact dataset digests and prompt fingerprints.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer
from config.settings import get_settings

from regwatch.common.citations import iter_psg_citations
from regwatch.generate import prose_turn
from regwatch.generate import turn_gate as tg
from regwatch.generate.guidance import GuidanceRequest, build_guidance_request, parse_guidance_plan
from regwatch.generate.llm import LLMMessage, current_model_name, get_llm_provider
from regwatch.generate.prompts import (
    BE_EXTRACTION_SYSTEM,
    BE_EXTRACTION_USER,
    GROUNDED_QA_EXEMPLARS_V6,
    GROUNDED_QA_EXEMPLARS_V7,
    GROUNDED_QA_SYSTEM,
    GROUNDED_QA_SYSTEM_V6,
    GROUNDED_QA_SYSTEM_V7,
    GROUNDED_QA_USER,
    GROUNDED_QA_USER_V6,
    GROUNDED_QA_USER_V7,
    generation_prompt_manifest,
)
from regwatch.generate.rag_contract import ClarifyOption
from regwatch.generate.route import (
    ROUTE_PROMPT,
    CorpusPolicyHint,
    RouteHistoryTurn,
    RouteRequest,
    ScopeHint,
    TurnMode,
    build_route_request,
    parse_route_decision,
)
from regwatch.generate.turn_schema import TURN_SCHEMA_MESSAGE
from regwatch.process.change_detector import summarize_change
from regwatch.process.extractor import FIELD_NAMES, _passages_for_prompt, _validate_field_citation
from regwatch.retrieve.retriever import RetrievedPassage

app = typer.Typer(add_completion=False, help="Validate or run synthetic prompt evaluations.")
_SET_DIR = Path(__file__).with_name("prompt_sets")
_SET_FILES = {
    "qa": _SET_DIR / "qa.jsonl",
    "guidance": _SET_DIR / "guidance.jsonl",
    "route": _SET_DIR / "route.jsonl",
    "extraction": _SET_DIR / "extraction.jsonl",
    "changes": _SET_DIR / "changes.jsonl",
}
_PARTIAL_PREFIX = "Evidence not found in the supplied passages for:"
_PAGE_SEP = "\n\f\n"


def _prompt_manifest() -> dict[str, dict[str, str]]:
    """Served prompts plus dark contracts exercised only by this eval harness."""
    manifest = generation_prompt_manifest()
    manifest[ROUTE_PROMPT.prompt_id] = ROUTE_PROMPT.as_dict()
    return manifest


@dataclass(frozen=True)
class LoadedSet:
    rows: list[dict[str, Any]]
    sha256: str


def _load_jsonl(path: Path) -> LoadedSet:
    raw = path.read_bytes()
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for number, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        row = json.loads(line)
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            raise ValueError(f"{path}:{number}: each row needs a string id")
        if row["id"] in seen:
            raise ValueError(f"{path}:{number}: duplicate id {row['id']!r}")
        seen.add(row["id"])
        rows.append(row)
    if not rows:
        raise ValueError(f"{path}: empty prompt set")
    return LoadedSet(rows=rows, sha256=hashlib.sha256(raw).hexdigest())


def validate_prompt_sets() -> dict[str, dict[str, Any]]:
    """Load every committed synthetic set and return its immutable identity."""
    loaded = {name: _load_jsonl(path) for name, path in _SET_FILES.items()}
    required = {
        "qa": {
            "question",
            "passages",
            "expected_facts",
            "expected_citations",
            "expected_turn_type",
        },
        "guidance": {
            "question",
            "status",
            "reason",
            "product",
            "clarify",
            "related",
            "expected_next_steps",
            "expected_first_option_ids",
        },
        "route": {
            "question",
            "recent_turns",
            "trusted_product_context",
            "expected_modes",
            "expected_scope_hints",
            "expected_corpus_policies",
            "expected_standalone_terms",
        },
        "extraction": {"pages", "expected", "expected_null"},
        "changes": {"previous_pages", "current_pages", "expected_terms", "expected_markers"},
    }
    for name, item in loaded.items():
        for row in item.rows:
            missing = required[name] - row.keys()
            if missing:
                raise ValueError(f"{name}:{row['id']}: missing {sorted(missing)}")
            if name == "guidance":
                request = _guidance_request(row)
                expected = row["expected_next_steps"]
                if (
                    not isinstance(expected, list)
                    or not expected
                    or not all(isinstance(step, str) and step for step in expected)
                ):
                    raise ValueError(
                        f"guidance:{row['id']}: expected_next_steps needs non-empty strings"
                    )
                if not set(expected).issubset(request.allowed_next_steps):
                    raise ValueError(
                        f"guidance:{row['id']}: expected_next_steps must be route-allowlisted"
                    )
                expected_options = row["expected_first_option_ids"]
                if not isinstance(expected_options, list) or not all(
                    isinstance(option_id, str) and option_id for option_id in expected_options
                ):
                    raise ValueError(
                        f"guidance:{row['id']}: expected_first_option_ids must be strings"
                    )
                if not set(expected_options).issubset(request.option_ids):
                    raise ValueError(
                        f"guidance:{row['id']}: expected option ids must be route-allowlisted"
                    )
            if name == "route":
                route_request = _route_request(row)
                expected_modes = row["expected_modes"]
                expected_scopes = row["expected_scope_hints"]
                expected_policies = row["expected_corpus_policies"]
                expected_terms = row["expected_standalone_terms"]
                if (
                    not isinstance(expected_modes, list)
                    or not expected_modes
                    or not set(expected_modes).issubset({mode.value for mode in TurnMode})
                ):
                    raise ValueError(f"route:{row['id']}: invalid expected_modes")
                if (
                    not isinstance(expected_scopes, list)
                    or not expected_scopes
                    or not set(expected_scopes).issubset({scope.value for scope in ScopeHint})
                ):
                    raise ValueError(f"route:{row['id']}: invalid expected_scope_hints")
                policy_values = {policy.value for policy in route_request.allowed_corpus_policies}
                if (
                    not isinstance(expected_policies, list)
                    or not expected_policies
                    or any(
                        policy is not None and policy not in policy_values
                        for policy in expected_policies
                    )
                ):
                    raise ValueError(f"route:{row['id']}: invalid expected_corpus_policies")
                if not isinstance(expected_terms, list) or not all(
                    isinstance(term, str) and term.strip() for term in expected_terms
                ):
                    raise ValueError(
                        f"route:{row['id']}: expected_standalone_terms must be strings"
                    )
    return {name: {"count": len(item.rows), "sha256": item.sha256} for name, item in loaded.items()}


def _contains_all(text: str, expected: list[str]) -> bool:
    lowered = " ".join(text.lower().replace("-", " ").split())
    return all(" ".join(term.lower().replace("-", " ").split()) in lowered for term in expected)


def _contains_none(text: str, forbidden: list[str]) -> bool:
    lowered = text.lower()
    return all(term.lower() not in lowered for term in forbidden)


def _qa_user(row: dict[str, Any]) -> str:
    blocks = []
    for passage in row["passages"]:
        section = f" ({passage['section']})" if passage.get("section") else ""
        blocks.append(
            f"[{passage['short_name']}, p.{passage['page']}]{section}\n{passage['text']}\n"
        )
    return GROUNDED_QA_USER.format(
        recent_context="", question=row["question"], passages="\n---\n".join(blocks)
    )


def _qa_user_v6(row: dict[str, Any]) -> str:
    """The v6 numbered-passage user prompt for one eval row.

    Mirrors grounded_qa._format_passages_numbered: a [n] label prefixes the
    same pair header, so the harness feeds the model exactly the prompt shape
    the runtime sends.
    """
    blocks = []
    for position, passage in enumerate(row["passages"], start=1):
        section = f" ({passage['section']})" if passage.get("section") else ""
        blocks.append(
            f"[{position}] [{passage['short_name']}, p.{passage['page']}]{section}\n"
            f"{passage['text']}\n"
        )
    return GROUNDED_QA_USER_V6.format(
        recent_context="", question=row["question"], passages="\n---\n".join(blocks)
    )


def _qa_user_v7(row: dict[str, Any]) -> str:
    """The v7 numbered-passage user prompt for one eval row.

    Same numbered-passage builder as _qa_user_v6 -- the shape is identical,
    only the tail paragraph differs (GROUNDED_QA_USER_V7 vs _V6).
    """
    blocks = []
    for position, passage in enumerate(row["passages"], start=1):
        section = f" ({passage['section']})" if passage.get("section") else ""
        blocks.append(
            f"[{position}] [{passage['short_name']}, p.{passage['page']}]{section}\n"
            f"{passage['text']}\n"
        )
    return GROUNDED_QA_USER_V7.format(
        recent_context="", question=row["question"], passages="\n---\n".join(blocks)
    )


def _qa_passages(row: dict[str, Any]) -> list[RetrievedPassage]:
    """Synthetic passages in the shape the gate validates against.

    The eval feeds the gate the SAME objects the runtime does, so a citation the
    gate would reject at runtime is rejected here too -- the harness cannot pass
    an answer the product would decline.
    """
    return [
        RetrievedPassage(
            chunk_id=f"{row['id']}-{index}",
            text=passage["text"],
            score=1.0,
            doc_id=index,
            version_id=index,
            page=int(passage["page"]),
            section_path=passage.get("section"),
            normalized_name="synthetic",
            source_url="",
            short_name=str(passage["short_name"]),
            metadata={},
        )
        for index, passage in enumerate(row["passages"])
    ]


def _guidance_options(row: dict[str, Any], field: str) -> list[ClarifyOption]:
    raw_options = row[field]
    if not isinstance(raw_options, list):
        raise ValueError(f"guidance:{row['id']}: {field} must be a list")
    options: list[ClarifyOption] = []
    for index, raw in enumerate(raw_options):
        if not isinstance(raw, dict):
            raise ValueError(f"guidance:{row['id']}: {field}[{index}] must be an object")
        label = raw.get("label")
        query = raw.get("query")
        filters = raw.get("filters")
        if not isinstance(label, str) or not label.strip():
            raise ValueError(f"guidance:{row['id']}: {field}[{index}] needs a label")
        if not isinstance(query, str) or not query.strip():
            raise ValueError(f"guidance:{row['id']}: {field}[{index}] needs a query")
        if filters is not None and not isinstance(filters, dict):
            raise ValueError(f"guidance:{row['id']}: {field}[{index}].filters must be an object")
        options.append(ClarifyOption(label=label, query=query, filters=filters))
    return options


def _guidance_request(row: dict[str, Any]) -> GuidanceRequest:
    question = row["question"]
    status = row["status"]
    reason = row["reason"]
    product = row["product"]
    if not isinstance(question, str) or not question.strip():
        raise ValueError(f"guidance:{row['id']}: question must be a non-empty string")
    if not isinstance(status, str) or not status:
        raise ValueError(f"guidance:{row['id']}: status must be a non-empty string")
    if not isinstance(reason, str) or not reason:
        raise ValueError(f"guidance:{row['id']}: reason must be a non-empty string")
    if product is not None and (not isinstance(product, str) or not product.strip()):
        raise ValueError(f"guidance:{row['id']}: product must be null or a non-empty string")
    return build_guidance_request(
        question=question,
        status=status,
        reason=reason,
        product=product,
        clarify=_guidance_options(row, "clarify"),
        related=_guidance_options(row, "related"),
    )


def _route_request(row: dict[str, Any]) -> RouteRequest:
    question = row["question"]
    trusted_product = row["trusted_product_context"]
    raw_turns = row["recent_turns"]
    if not isinstance(question, str) or not question.strip():
        raise ValueError(f"route:{row['id']}: question must be a non-empty string")
    if trusted_product is not None and (
        not isinstance(trusted_product, str) or not trusted_product.strip()
    ):
        raise ValueError(f"route:{row['id']}: trusted_product_context must be null or non-empty")
    if not isinstance(raw_turns, list):
        raise ValueError(f"route:{row['id']}: recent_turns must be a list")
    turns: list[RouteHistoryTurn] = []
    for index, raw_turn in enumerate(raw_turns):
        if not isinstance(raw_turn, dict):
            raise ValueError(f"route:{row['id']}: recent_turns[{index}] must be an object")
        prior_question = raw_turn.get("question")
        answer = raw_turn.get("answer")
        scope_kind = raw_turn.get("scope_kind", "none")
        scope_audited = raw_turn.get("scope_audited", False)
        raw_policy = raw_turn.get("corpus_policy")
        if not isinstance(prior_question, str) or not isinstance(answer, str):
            raise ValueError(f"route:{row['id']}: recent_turns[{index}] needs question and answer")
        if scope_kind not in {"none", "product", "corpus"}:
            raise ValueError(f"route:{row['id']}: invalid prior scope_kind")
        if not isinstance(scope_audited, bool):
            raise ValueError(f"route:{row['id']}: scope_audited must be boolean")
        try:
            policy = CorpusPolicyHint(raw_policy) if raw_policy is not None else None
        except ValueError as exc:
            raise ValueError(f"route:{row['id']}: invalid prior corpus_policy") from exc
        turns.append(
            RouteHistoryTurn(
                question=prior_question,
                answer=answer,
                scope_kind=scope_kind,
                scope_audited=scope_audited,
                corpus_policy=policy,
            )
        )
    raw_allowed = row.get("allowed_corpus_policies", [CorpusPolicyHint.INHALATION_PSG.value])
    if not isinstance(raw_allowed, list):
        raise ValueError(f"route:{row['id']}: allowed_corpus_policies must be a list")
    try:
        allowed = tuple(CorpusPolicyHint(value) for value in raw_allowed)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"route:{row['id']}: invalid allowed_corpus_policies") from exc
    return build_route_request(
        question=question,
        recent_turns=tuple(turns),
        trusted_product_context=trusted_product,
        allowed_corpus_policies=allowed,
    )


def _run_qa(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    settings = get_settings()
    # Flag-aware, mirroring the runtime exactly: with REGWATCH_PROSE_SYNTHESIS
    # set, rows run through the v6 prose prompt, the prose parser, and the same
    # gate with correction live and the uncited-downgrade OFF -- so a dark eval
    # measures the chain prod would serve after the flip, scored on the SAME
    # rendered string (rendering is unchanged, so iter_psg_citations scoring
    # stays valid).
    prose_mode = bool(getattr(settings, "prose_synthesis_enabled", False))
    # Same topology as grounded_qa: v7 is a POLICY change on top of v6's
    # FORMAT, so it is only honored when prose is also on.
    selective = prose_mode and bool(getattr(settings, "selective_citation_enabled", False))
    provider = get_llm_provider(role="synthesizer")
    details: list[dict[str, Any]] = []
    for row in rows:
        passages = _qa_passages(row)
        if selective:
            messages = [LLMMessage(role="system", content=GROUNDED_QA_SYSTEM_V7)]
            messages.extend(
                LLMMessage(role=exemplar_role, content=exemplar_text)
                for exemplar_role, exemplar_text in GROUNDED_QA_EXEMPLARS_V7
            )
            messages.append(LLMMessage(role="user", content=_qa_user_v7(row)))
            response_format: str | None = None
        elif prose_mode:
            messages = [LLMMessage(role="system", content=GROUNDED_QA_SYSTEM_V6)]
            messages.extend(
                LLMMessage(role=exemplar_role, content=exemplar_text)
                for exemplar_role, exemplar_text in GROUNDED_QA_EXEMPLARS_V6
            )
            messages.append(LLMMessage(role="user", content=_qa_user_v6(row)))
            response_format = None
        else:
            messages = [
                LLMMessage(role="system", content=GROUNDED_QA_SYSTEM),
                LLMMessage(role="user", content=_qa_user(row)),
                TURN_SCHEMA_MESSAGE,
            ]
            response_format = "json"
        response = provider.complete(
            messages,
            temperature=0.0,
            max_tokens=settings.synthesizer_max_tokens,
            response_format=response_format,
        )
        raw_completion = response.text.strip()
        killed_sentences: list[str] = []
        if prose_mode:
            parsed = prose_turn.parse(raw_completion, passages=passages, selective=selective)
            material_kill = parsed.truncated_material or any(
                tg.materiality_trigger(t) is not None for t in parsed.leftover_brackets
            )
            if material_kill or (parsed.turn_type == "ANSWER" and not parsed.claims):
                # The runtime declines these turns before the gate; the row is
                # a failure the same way a GateFailure is.
                details.append({"id": row["id"], "passed": False, "model": response.model})
                continue
            killed_sentences = list(parsed.leftover_brackets)
            gate_input = prose_turn.gate_payload(parsed, passages)
        else:
            gate_input = raw_completion
        admitted = tg.admit_turn(
            gate_input,
            passages=passages,
            question=row["question"],
            correct=prose_mode,
            downgrade_uncited=False,
            # Gated on selective, not prose_mode: admit_turn only ever
            # consults kinds when selective=True, so passing it unconditionally
            # under v6-prose was inert -- but it diverged from grounded_qa.py's
            # own v6 call shape (no kinds argument at all), a latent gap the
            # post-launch review flagged before it could become a real one.
            kinds=[c.kind for c in parsed.claims] if selective else None,
            selective=selective,
        )
        if isinstance(admitted, tg.GateFailure):
            details.append({"id": row["id"], "passed": False, "model": response.model})
            continue
        # Every text assertion runs against the RENDERED string -- what a user
        # would actually read -- not against the model's raw draft. A v7
        # conversational decline has no render_answer shape (no admitted
        # source fact to render), so it is scored on its own decline text and
        # treated as the NO_EVIDENCE turn type it functionally is.
        if admitted.verdict == tg.VERDICT_CONVERSATIONAL_DECLINE:
            text, _ = tg.render_decline(admitted)
            text = text or ""
            turn_type = "NO_EVIDENCE"
        else:
            text = tg.render_answer(admitted)
            turn_type = "NO_EVIDENCE" if admitted.verdict == tg.VERDICT_NO_EVIDENCE else "ANSWER"
        citations = {(name.upper(), page) for name, page in iter_psg_citations(text)}
        expected_citations = {
            (str(name).upper(), int(page)) for name, page in row["expected_citations"]
        }
        partial_ok = bool(row.get("expect_partial")) == (_PARTIAL_PREFIX in text)
        labels_ok = all(
            label.lower() in text.lower() for label in row.get("unsupported_labels", [])
        )
        passed = all(
            (
                turn_type == str(row["expected_turn_type"]),
                citations == expected_citations,
                _contains_all(text, row["expected_facts"]),
                _contains_none(text, row.get("forbidden", [])),
                partial_ok,
                labels_ok,
                # Strictly stronger than the old "no uncited segment" criterion:
                # nothing the model drafted may have been dropped at all.
                not admitted.dropped,
                # Prose parity for the same criterion: a parser-killed sentence
                # is a drop that never reached the gate.
                not killed_sentences,
            )
        )
        details.append({"id": row["id"], "passed": passed, "model": response.model})
    return details


def _run_guidance(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Score bounded next-step selections; never render or score display copy."""
    provider = get_llm_provider(role="router")
    details: list[dict[str, Any]] = []
    for row in rows:
        request = _guidance_request(row)
        response = provider.complete(
            request.messages,
            temperature=0.0,
            max_tokens=600,
            response_format="json",
        )
        try:
            plan = parse_guidance_plan(response.text, request)
        except ValueError as exc:
            details.append(
                {
                    "id": row["id"],
                    "passed": False,
                    "model": response.model,
                    "failure": str(exc),
                }
            )
            continue
        expected_first = row["expected_first_option_ids"]
        option_match = not expected_first or (
            bool(plan.option_ids) and plan.option_ids[0] in expected_first
        )
        details.append(
            {
                "id": row["id"],
                "passed": plan.next_step in row["expected_next_steps"] and option_match,
                "model": response.model,
                "next_step": plan.next_step,
                "option_ids": plan.option_ids,
                "option_match": option_match,
            }
        )
    return details


def _run_route(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Score advisory mode and scope jointly; do not execute either one."""
    provider = get_llm_provider(role="router")
    details: list[dict[str, Any]] = []
    for row in rows:
        request = _route_request(row)
        response = provider.complete(
            request.messages,
            temperature=0.0,
            # This is an opt-in synthetic eval budget, not the runtime shadow
            # budget. PR11b still probes the endpoint's reasoning floor before
            # choosing the production setting.
            max_tokens=1200,
            response_format="json",
        )
        try:
            decision = parse_route_decision(response.text, request)
        except ValueError as exc:
            details.append(
                {
                    "id": row["id"],
                    "passed": False,
                    "model": response.model,
                    "failure": str(exc),
                }
            )
            continue
        policy = decision.corpus_policy_hint.value if decision.corpus_policy_hint else None
        mode_ok = decision.mode.value in row["expected_modes"]
        scope_ok = decision.scope_hint.value in row["expected_scope_hints"]
        policy_ok = policy in row["expected_corpus_policies"]
        standalone_ok = _contains_all(
            decision.standalone_question, row["expected_standalone_terms"]
        )
        details.append(
            {
                "id": row["id"],
                "passed": mode_ok and scope_ok and policy_ok and standalone_ok,
                "model": response.model,
                "mode": decision.mode.value,
                "scope_hint": decision.scope_hint.value,
                "corpus_policy_hint": policy,
                "mode_ok": mode_ok,
                "scope_ok": scope_ok,
                "policy_ok": policy_ok,
                "standalone_ok": standalone_ok,
            }
        )
    return details


def _run_extraction(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    provider = get_llm_provider(role="extractor")
    details: list[dict[str, Any]] = []
    for row in rows:
        pages = [str(page) for page in row["pages"]]
        response = provider.complete(
            [
                LLMMessage(role="system", content=BE_EXTRACTION_SYSTEM),
                LLMMessage(
                    role="user",
                    content=BE_EXTRACTION_USER.format(passages=_passages_for_prompt(pages)),
                ),
            ],
            temperature=0.0,
            max_tokens=1500,
            response_format="json",
        )
        try:
            payload = json.loads(response.text)
        except json.JSONDecodeError:
            payload = {}
        raw_fields = payload.get("fields", payload) if isinstance(payload, dict) else {}
        values: dict[str, Any] = {}
        for name in FIELD_NAMES:
            value, _citation = _validate_field_citation(name, raw_fields.get(name), pages)
            values[name] = value
        expected_ok = all(
            _contains_all(str(values.get(name) or ""), terms)
            for name, terms in row["expected"].items()
        )
        null_ok = all(values.get(name) is None for name in row["expected_null"])
        forbidden_ok = _contains_none(json.dumps(values), row.get("forbidden", []))
        details.append(
            {
                "id": row["id"],
                "passed": expected_ok and null_ok and forbidden_ok,
                "model": response.model,
            }
        )
    return details


def _run_changes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for row in rows:
        current_pages = [str(page) for page in row["current_pages"]]
        summary = summarize_change(
            _PAGE_SEP.join(str(page) for page in row["previous_pages"]),
            _PAGE_SEP.join(current_pages),
            current_page_count=len(current_pages),
        )
        passed = all(
            (
                _contains_all(summary, row["expected_terms"]),
                all(marker in summary for marker in row["expected_markers"]),
                _contains_none(summary, row.get("forbidden", [])),
            )
        )
        details.append(
            {"id": row["id"], "passed": passed, "model": current_model_name(role="extractor")}
        )
    return details


@app.command("validate")
def validate_command() -> None:
    """Validate committed sets without a database, API key, or model call."""
    typer.echo(
        json.dumps({"sets": validate_prompt_sets(), "prompts": _prompt_manifest()}, indent=2)
    )


@app.command("run")
def run_command(
    out: Path = typer.Option(..., "--out", help="Write the fingerprinted JSON result here."),
) -> None:
    """Run all sets against the configured provider. This makes live model calls."""
    sets = {name: _load_jsonl(path) for name, path in _SET_FILES.items()}
    results = {
        "qa": _run_qa(sets["qa"].rows),
        "guidance": _run_guidance(sets["guidance"].rows),
        "route": _run_route(sets["route"].rows),
        "extraction": _run_extraction(sets["extraction"].rows),
        "changes": _run_changes(sets["changes"].rows),
    }
    artifact = {
        "artifact_schema_version": 1,
        "prompts": _prompt_manifest(),
        "sets": {
            name: {"count": len(item.rows), "sha256": item.sha256} for name, item in sets.items()
        },
        "results": results,
        "passed": all(row["passed"] for rows in results.values() for row in rows),
    }
    out.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    typer.echo(f"prompt eval artifact written to {out}")
    if not artifact["passed"]:
        raise typer.Exit(code=2)


if __name__ == "__main__":
    app()
