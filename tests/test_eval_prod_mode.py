"""The eval must refuse to score an arm production does not serve.

The blocking gate spent its whole life measuring v5 while production served v7
(docs/ROADMAP.md), and nothing in the run said so: a green check meant "some
arm passed", not "the arm you ship passed". These tests pin the comparison that
makes that impossible to repeat.

Pure comparison only -- no settings, no DB, no network -- so the rule is
testable without a live arm.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from regwatch.eval import run_eval
from regwatch.eval.prod_mode import ManifestError, load_manifest, mismatches

_PROD = {
    "prose_synthesis_enabled": True,
    "selective_citation_enabled": True,
}


def test_an_arm_that_matches_production_reports_no_mismatch() -> None:
    assert mismatches(dict(_PROD), _PROD) == []


def test_the_v5_arm_is_reported_against_a_v7_production() -> None:
    """The exact defect this exists to catch."""
    effective = {"prose_synthesis_enabled": False, "selective_citation_enabled": False}
    found = mismatches(effective, _PROD)
    assert len(found) == 2
    assert any("prose_synthesis_enabled" in m for m in found)
    assert any("selective_citation_enabled" in m for m in found)


def test_a_mismatch_names_both_the_expected_and_the_actual_value() -> None:
    """A bare key name would not tell an operator which way it drifted."""
    effective = {"prose_synthesis_enabled": False, "selective_citation_enabled": True}
    (found,) = mismatches(effective, _PROD)
    assert "prose_synthesis_enabled" in found
    assert "True" in found
    assert "False" in found


def test_a_key_production_declares_but_the_run_cannot_report_is_an_error() -> None:
    """Silently skipping an unreadable key would re-open the same hole."""
    with pytest.raises(ManifestError):
        mismatches({"prose_synthesis_enabled": True}, _PROD)


def test_the_run_may_carry_keys_the_manifest_does_not_pin() -> None:
    """The manifest pins what MUST match; extra effective settings are fine."""
    effective = dict(_PROD) | {"vector_top_k": 50}
    assert mismatches(effective, _PROD) == []


def test_the_committed_manifest_declares_the_flags_prod_actually_runs() -> None:
    """The shipped file, not a fixture: it is the single source of truth."""
    manifest = load_manifest()
    assert manifest["prose_synthesis_enabled"] is True
    assert manifest["selective_citation_enabled"] is True


def test_manifest_provenance_notes_are_not_asserted_as_settings() -> None:
    """JSON has no comments, so the file carries _-prefixed notes.

    Left in the contract they would be compared against settings that cannot
    exist, and every run would fail on the file's own documentation.
    """
    assert not [key for key in load_manifest() if key.startswith("_")]


def test_a_manifest_that_is_not_an_object_is_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "prod_mode.json"
    bad.write_text('["prose_synthesis_enabled"]', encoding="utf-8")
    with pytest.raises(ManifestError):
        load_manifest(bad)


def test_a_missing_manifest_is_an_error_not_an_empty_contract(tmp_path: Path) -> None:
    """An absent file must never read as "nothing to check"."""
    with pytest.raises(ManifestError):
        load_manifest(tmp_path / "does_not_exist.json")


def test_the_flag_stops_a_v5_run_before_it_touches_the_corpus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate's actual behavior: exit non-zero, and exit EARLY.

    Settings default both flags off, which is exactly the v5 arm the blocking
    CI eval has been scoring, so the default test settings ARE the defect.
    """
    opened: list[str] = []
    monkeypatch.setattr(run_eval, "init_db", lambda *a, **k: opened.append("init_db"), raising=True)
    with pytest.raises(SystemExit) as exit_info:
        run_eval._assert_prod_mode()
    assert exit_info.value.code == run_eval.EXIT_WRONG_ARM
    # Nothing was seeded, connected to, or embedded before the refusal.
    assert opened == []


def test_calling_run_as_a_function_leaves_the_assertion_off() -> None:
    """The flag must default OFF for a caller that is not the CLI.

    Declared the old typer way (``x: bool = typer.Option(False, ...)``) the
    runtime default is an OptionInfo object, and OptionInfo is truthy -- so
    every direct caller that omitted the argument would silently turn the
    production-mode assertion ON. The eval's own CLI tests call run() directly,
    and that is exactly how this was caught.
    """
    signature = inspect.signature(run_eval.run)
    assert signature.parameters["assert_prod_mode"].default is False
