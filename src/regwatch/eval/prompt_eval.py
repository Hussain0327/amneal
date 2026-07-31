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
from regwatch.generate.grounded_qa import _uncited_answer_segments
from regwatch.generate.llm import LLMMessage, current_model_name, get_llm_provider
from regwatch.generate.prompts import (
    BE_EXTRACTION_SYSTEM,
    BE_EXTRACTION_USER,
    GROUNDED_QA_SYSTEM,
    GROUNDED_QA_USER,
    generation_prompt_manifest,
)
from regwatch.process.change_detector import summarize_change
from regwatch.process.extractor import FIELD_NAMES, _passages_for_prompt, _validate_field_citation

app = typer.Typer(add_completion=False, help="Validate or run synthetic prompt evaluations.")
_SET_DIR = Path(__file__).with_name("prompt_sets")
_SET_FILES = {
    "qa": _SET_DIR / "qa.jsonl",
    "extraction": _SET_DIR / "extraction.jsonl",
    "changes": _SET_DIR / "changes.jsonl",
}
_PARTIAL_PREFIX = "Evidence not found in the supplied passages for:"
_PAGE_SEP = "\n\f\n"


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
        "qa": {"question", "passages", "expected_facts", "expected_citations", "must_refuse"},
        "extraction": {"pages", "expected", "expected_null"},
        "changes": {"previous_pages", "current_pages", "expected_terms", "expected_markers"},
    }
    for name, item in loaded.items():
        for row in item.rows:
            missing = required[name] - row.keys()
            if missing:
                raise ValueError(f"{name}:{row['id']}: missing {sorted(missing)}")
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


def _run_qa(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    settings = get_settings()
    provider = get_llm_provider(role="synthesizer")
    details: list[dict[str, Any]] = []
    for row in rows:
        response = provider.complete(
            [
                LLMMessage(
                    role="system",
                    content=GROUNDED_QA_SYSTEM.format(refusal=settings.refusal_text),
                ),
                LLMMessage(role="user", content=_qa_user(row)),
            ],
            temperature=0.0,
            max_tokens=settings.synthesizer_max_tokens,
        )
        text = response.text.strip()
        refused = text.startswith(settings.refusal_text)
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
                refused == bool(row["must_refuse"]),
                citations == expected_citations,
                _contains_all(text, row["expected_facts"]),
                _contains_none(text, row.get("forbidden", [])),
                partial_ok,
                labels_ok,
                not _uncited_answer_segments(text) if not refused else True,
            )
        )
        details.append({"id": row["id"], "passed": passed, "model": response.model})
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
        json.dumps(
            {"sets": validate_prompt_sets(), "prompts": generation_prompt_manifest()}, indent=2
        )
    )


@app.command("run")
def run_command(
    out: Path = typer.Option(..., "--out", help="Write the fingerprinted JSON result here."),
) -> None:
    """Run all sets against the configured provider. This makes live model calls."""
    sets = {name: _load_jsonl(path) for name, path in _SET_FILES.items()}
    results = {
        "qa": _run_qa(sets["qa"].rows),
        "extraction": _run_extraction(sets["extraction"].rows),
        "changes": _run_changes(sets["changes"].rows),
    }
    artifact = {
        "artifact_schema_version": 1,
        "prompts": generation_prompt_manifest(),
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
