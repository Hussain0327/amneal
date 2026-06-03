"""Parse a fixture of the PSG index page (no network)."""

from __future__ import annotations

from pathlib import Path

from regwatch.ingest.psg_crawler import filter_listings, parse_listings

FIXTURE = Path(__file__).parent / "fixtures" / "psg_index_sample.html"


def _load() -> list:
    return parse_listings(FIXTURE.read_text(encoding="utf-8"))


def test_parses_rows() -> None:
    rows = _load()
    assert len(rows) == 4
    names = {r.normalized_name for r in rows}
    assert "albuterol sulfate" in names
    assert "beclomethasone dipropionate" in names
    assert "romidepsin" in names


def test_application_numbers() -> None:
    rows = _load()
    albuterol = next(r for r in rows if r.appl_no == "020503")
    assert albuterol.rld_or_rs_numbers == ["020503", "020983", "021457"]


def test_filter_by_seed_names() -> None:
    rows = _load()
    seeds = filter_listings(rows, normalized_names=["albuterol", "beclomethasone", "romidepsin"])
    assert len(seeds) == 3
    appls = sorted(r.appl_no for r in seeds)
    assert appls == ["020503", "020911", "022393"]


def test_iso_date() -> None:
    rows = _load()
    albuterol = next(r for r in rows if r.appl_no == "020503")
    assert albuterol.recommended_date == "2026-05-21"


def test_dosage_form_and_route() -> None:
    rows = _load()
    albuterol = next(r for r in rows if r.appl_no == "020503")
    assert albuterol.dosage_form == "Aerosol, Metered"
    assert albuterol.route == "Inhalation"
    assert albuterol.psg_type == "draft"


def test_filter_does_not_match_unrelated() -> None:
    rows = _load()
    seeds = filter_listings(rows, normalized_names=["albuterol", "beclomethasone", "romidepsin"])
    names = {r.normalized_name for r in seeds}
    assert "sodium chloride" not in names
