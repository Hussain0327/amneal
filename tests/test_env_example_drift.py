"""Drift guard: .env.example must document every user-facing Settings knob.

Mirrors the repo's api-types codegen-drift CI gate (which fails when the
committed frontend types fall out of sync with the backend schema). Here the
contract is the operator-facing one: every env var the Settings model binds
should be discoverable in .env.example, and .env.example must not advertise a
var the model no longer reads. Either direction is silent config rot otherwise
— a new knob nobody knows to set, or copy-paste cargo for a deleted one.

Policy: every Settings field that binds an env name must appear in .env.example,
EXCEPT names in _ALLOWLIST below (each with a documented reason). Secrets are
fine to document with an empty value (the file already does, e.g. OPENAI_API_KEY).
"""

from __future__ import annotations

import re
from pathlib import Path

from config.settings import Settings

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ENV_EXAMPLE = _REPO_ROOT / ".env.example"

# Env names the model binds but that we deliberately keep OUT of .env.example.
# Anything added here needs a one-line reason — the allowlist is the escape
# hatch, not the default.
_ALLOWLIST: dict[str, str] = {
    # Deprecated legacy alias for RERANK_TOP_K, kept only for backwards compat
    # (see Settings.effective_rerank_top_k). Intentionally NOT advertised so
    # operators adopt RERANK_TOP_K instead of resurrecting the old name.
    "RETRIEVAL_TOP_K": "deprecated legacy alias for RERANK_TOP_K",
}

# Matches `NAME=...` assignment lines (commented-out `# NAME=...` examples for
# advanced/optional JSON knobs count as documented too).
_ASSIGN_RE = re.compile(r"^\s*#?\s*([A-Z][A-Z0-9_]*)\s*=", re.MULTILINE)


def _settings_env_names() -> set[str]:
    """Env-var names the Settings model binds (validation_alias or UPPER field)."""
    names: set[str] = set()
    for field_name, field in Settings.model_fields.items():
        alias = field.validation_alias
        env = alias if isinstance(alias, str) else field_name
        names.add(env.upper())
    return names


def _env_example_names() -> set[str]:
    text = _ENV_EXAMPLE.read_text(encoding="utf-8")
    return {m.group(1) for m in _ASSIGN_RE.finditer(text)}


def test_env_example_documents_every_settings_field() -> None:
    """Fail if a Settings env knob is missing from .env.example (minus allowlist)."""
    documented = _env_example_names()
    required = _settings_env_names() - set(_ALLOWLIST)
    missing = sorted(required - documented)
    assert not missing, (
        "These Settings env vars are not documented in .env.example: "
        f"{missing}. Add them under the right section header (with a short "
        "comment), or, if a var is intentionally internal/secret, add it to "
        "_ALLOWLIST in this test with a reason."
    )


def test_env_example_has_no_phantom_vars() -> None:
    """Fail if .env.example advertises a var the Settings model no longer reads."""
    documented = _env_example_names()
    known = _settings_env_names() | set(_ALLOWLIST)
    phantom = sorted(documented - known)
    assert not phantom, (
        "These vars are in .env.example but are not bound by any Settings "
        f"field: {phantom}. Remove the stale lines, or add the field to "
        "Settings if the var should be read."
    )


def test_allowlist_entries_are_real_settings_fields() -> None:
    """An allowlist entry that no longer exists in Settings is itself drift."""
    env_names = _settings_env_names()
    stale = sorted(name for name in _ALLOWLIST if name not in env_names)
    assert not stale, (
        f"_ALLOWLIST names no longer bound by Settings: {stale}. " "Drop them from the allowlist."
    )
