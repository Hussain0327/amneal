"""What production serves, declared once and asserted by the eval.

WHY: the blocking eval called by ci.yml ran with ``prose: false`` and
``selective: false`` from the day the prose arms shipped, so the merge gate
scored the v5 claims-JSON chain while production served v7 selective citation
(docs/ROADMAP.md). Nothing in the run surfaced that. A green check meant "some
arm cleared the floors", and a reader had no way to tell which arm.

This module is the fix's load-bearing half: ``config/prod_mode.json`` states the
production answer mode, and ``run_eval --assert-prod-mode`` refuses to score a
run whose effective settings disagree. The manifest is the single source of
truth -- docs cite it rather than restating the flags, so the two cannot drift.

Deliberately NOT pinned here: ``qwen_embedding_dimension``. Its committed
default (1536) does not match the served 0.6B model (1024) on purpose -- the
value feeds the embedding-profile fingerprint, so moving it invalidates a staged
profile (see docs/DECISIONS.md). Asserting it would fail every run for a known,
deliberate reason and teach operators to pass --no-assert. Pin what an operator
can actually act on.

Pure comparison: no settings import, no DB, no network, so the rule is testable
without a live arm.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# <repo root>/src/regwatch/eval/prod_mode.py -> <repo root>
_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MANIFEST = _REPO_ROOT / "config" / "prod_mode.json"


class ManifestError(RuntimeError):
    """The production-mode contract could not be read or applied.

    Its own type so the caller can exit on a broken contract distinctly from a
    run that merely measured the wrong arm: an unreadable manifest is an
    operator error, a mismatch is a finding.
    """


def load_manifest(path: Path | None = None) -> dict[str, Any]:
    """Read the production-mode manifest.

    Args:
        path: Manifest to read. Defaults to the committed
            ``config/prod_mode.json``.

    Returns:
        The declared production settings, as a mapping of setting name to the
        value production runs.

    Raises:
        ManifestError: If the file is missing, unreadable, not valid JSON, or
            not a JSON object. A missing contract must never read as an empty
            one -- that is the silent-green failure this module exists to
            prevent, one level up.
    """
    target = path or DEFAULT_MANIFEST
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"cannot read production-mode manifest {target}: {exc}") from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"{target} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ManifestError(f"{target} must be a JSON object, got {type(parsed).__name__}")
    # Underscore-prefixed keys are provenance notes for whoever edits the file
    # (when it was verified, against what). JSON has no comments, and a note
    # left in the contract would be asserted as a setting and fail every run.
    return {key: value for key, value in parsed.items() if not key.startswith("_")}


def mismatches(effective: Mapping[str, Any], expected: Mapping[str, Any]) -> list[str]:
    """Every declared setting whose live value differs, as readable lines.

    Args:
        effective: The settings this process actually resolved.
        expected: The manifest -- what production serves.

    Returns:
        One human-readable line per mismatch, naming the setting, the value
        production runs and the value this run resolved. Empty when the run
        matches production.

    Raises:
        ManifestError: If the manifest declares a setting the run cannot
            report. Skipping it would silently shrink the contract, which is
            the same class of hole as skipping the eval entirely.
    """
    missing = [key for key in expected if key not in effective]
    if missing:
        raise ManifestError(
            "production-mode manifest declares settings this run cannot report: "
            + ", ".join(sorted(missing))
        )
    return [
        f"{key}: production runs {expected[key]!r}, this run resolved {effective[key]!r}"
        for key in expected
        if effective[key] != expected[key]
    ]
