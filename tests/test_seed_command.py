"""`regwatch seed` must actually seed the pinned documents, or fail loudly.

This exists because of a silent, total failure measured on 2026-08-05: cmd_seed
enumerated the catalog with ``fetch_index_html`` (the PSG landing page), which
server-renders only a small recent slice -- 15 rows that day, containing NONE of
SEED_APPL_NOS. The command matched 0 listings, ingested nothing, and exited 0.

Nothing caught it. The live eval gate is the only consumer, and it was skipped in
CI for want of provider credentials, so an empty store never surfaced. The moment
credentials are configured the gate would run against 0 chunks.

Both tests are network-free: they monkeypatch the crawler seam.
"""

from __future__ import annotations

from typing import Any

import pytest
import typer

from regwatch import cli
from regwatch.ingest import psg_crawler
from regwatch.ingest.psg_crawler import SEED_APPL_NOS, PsgListing


def _listing(appl_no: str) -> PsgListing:
    return PsgListing(
        appl_no=appl_no,
        active_ingredient="Albuterol Sulfate",
        normalized_name="albuterol sulfate",
        stripped_name="albuterol",
        psg_type="draft",
        route="Inhalation",
        dosage_form="Aerosol, Metered",
        rld_or_rs_numbers=[],
        recommended_date="2026-05-21",
        pdf_url=f"https://example.test/PSG_{appl_no}.pdf",
        source_url="https://example.test/index.cfm",
    )


@pytest.fixture
def _no_db_no_ingest(monkeypatch: pytest.MonkeyPatch) -> list[list[PsgListing]]:
    """Stub out the two side effects so only the crawl choice is under test."""
    ingested: list[list[PsgListing]] = []

    class _Stats:
        scanned = added = revised = unchanged = errors = 0

    monkeypatch.setattr(cli, "init_db", lambda: None)
    import regwatch.ingest.pipeline as pipeline_mod

    def _capture(listings: list[PsgListing]) -> Any:
        ingested.append(list(listings))
        return _Stats()

    monkeypatch.setattr(pipeline_mod, "ingest_listings", _capture)
    return ingested


def test_seed_enumerates_the_full_catalog_not_the_landing_page(
    monkeypatch: pytest.MonkeyPatch, _no_db_no_ingest: list[list[PsgListing]]
) -> None:
    """The regression: the landing page does not contain the pinned applications.

    fetch_index_html is wired to raise, so a reintroduction of the landing-page
    path fails this test rather than quietly seeding nothing.
    """

    def _boom(*_a: Any, **_k: Any) -> str:
        raise AssertionError("cmd_seed must not enumerate via the landing page")

    monkeypatch.setattr(psg_crawler, "fetch_index_html", _boom)
    monkeypatch.setattr(
        psg_crawler, "fetch_all_listings", lambda **_k: [_listing(a) for a in SEED_APPL_NOS]
    )

    with pytest.raises(typer.Exit) as exc:
        cli.cmd_seed()
    assert exc.value.exit_code == 0

    assert len(_no_db_no_ingest) == 1
    assert sorted(row.appl_no for row in _no_db_no_ingest[0]) == sorted(SEED_APPL_NOS)


def test_seed_fails_loudly_when_a_pinned_application_is_absent(
    monkeypatch: pytest.MonkeyPatch, _no_db_no_ingest: list[list[PsgListing]]
) -> None:
    """A partial seed makes gold items unanswerable and reads as a retrieval
    regression, so it must be an error, not a quiet success."""
    partial = [_listing(a) for a in SEED_APPL_NOS[:-1]]
    monkeypatch.setattr(psg_crawler, "fetch_all_listings", lambda **_k: partial)

    with pytest.raises(typer.Exit) as exc:
        cli.cmd_seed()
    assert exc.value.exit_code == 2
    # Nothing was ingested: a short seed is refused before it can half-fill the store.
    assert _no_db_no_ingest == []


def test_seed_refuses_an_empty_catalog(
    monkeypatch: pytest.MonkeyPatch, _no_db_no_ingest: list[list[PsgListing]]
) -> None:
    """The exact observed failure: zero matches must not exit 0."""
    monkeypatch.setattr(psg_crawler, "fetch_all_listings", lambda **_k: [])

    with pytest.raises(typer.Exit) as exc:
        cli.cmd_seed()
    assert exc.value.exit_code == 2
    assert _no_db_no_ingest == []
