"""Watch CLI: crawl → match → ingest matched → alert → digest (INV-4).

The crawl is mocked (deterministic listings, stub PDFs); providers are echo
via conftest. Asserts the composition semantics:
  - first run with a matched NEW listing → exactly one alert in the feed
  - second identical run → no NEW alert (idempotent); the prior alert persists
  - a match whose version was never ingested → never alerts (INV-4)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from regwatch.cli import app
from regwatch.common.text_normalize import canonical_name, stripped_name
from regwatch.ingest import pipeline as pipeline_mod
from regwatch.ingest.pdf_parser import ParsedPdf
from regwatch.ingest.psg_crawler import PsgListing
from regwatch.store.db import init_db
from regwatch.watch import run as run_mod
from regwatch.watch.alerts import latest_digest_records
from regwatch.watch.watchlist import add_manual_product

runner = CliRunner()

PAGES = [
    "I. Introduction\nThis Product-Specific Guidance describes the agency's "
    "current recommendations for bioequivalence (BE) studies for Albuterol "
    "Sulfate Inhalation Aerosol.",
    "II. Recommendations for BE Studies\n"
    "A. Type of study: A single-dose, randomized, in-vivo BE study is recommended.\n"
    "B. Subjects: Adult healthy non-smokers.\n"
    "C. Dissolution: USP Apparatus 2 at 50 RPM in 900 mL of pH 6.8 buffer.",
]


def _listing(appl_no: str = "020503", name: str = "Albuterol Sulfate") -> PsgListing:
    return PsgListing(
        appl_no=appl_no,
        active_ingredient=name,
        normalized_name=canonical_name(name),
        stripped_name=stripped_name(name),
        psg_type="final",
        route="Inhalation",
        dosage_form="Aerosol, Metered",
        rld_or_rs_numbers=[appl_no],
        recommended_date="2026-05-21",
        pdf_url=f"https://example.invalid/PSG_{appl_no}.pdf",
        source_url="https://example.invalid/index.cfm",
    )


def _patch_crawl(monkeypatch: pytest.MonkeyPatch, listings: list[PsgListing]) -> None:
    monkeypatch.setattr(run_mod, "fetch_all_listings", lambda: list(listings))


def _patch_pdf(monkeypatch: pytest.MonkeyPatch, state: dict[str, Any]) -> None:
    """Stub PDF download/parse with mutable hash/pages (simulates revisions)."""

    def fake_download(url: str, *, client: object | None = None) -> tuple[Path, bytes, str]:
        return Path("/tmp/regwatch-watch-test.pdf"), b"%PDF-1.4 stub", str(state["hash"])

    def fake_parse(pdf_bytes: bytes) -> ParsedPdf:
        pages = list(state["pages"])
        return ParsedPdf(text="\n\f\n".join(pages), pages=pages, engine="stub")

    monkeypatch.setattr(pipeline_mod, "download_pdf", fake_download)
    monkeypatch.setattr(pipeline_mod, "parse_pdf", fake_parse)


def _seed_watchlist(name: str = "Albuterol Sulfate") -> None:
    init_db()
    add_manual_product(
        active_ingredient=name,
        dosage_form="Aerosol, Metered",
        route="Inhalation",
        rld_name=None,
        rld_application_number="020503",
        company_status="approved",
        source="manual",
        source_url=None,
    )


def test_watch_first_run_alerts_on_new_matched_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_watchlist()
    _patch_crawl(monkeypatch, [_listing()])
    _patch_pdf(monkeypatch, {"hash": "hash-v1", "pages": PAGES})

    result = runner.invoke(app, ["watch", "--no-extract"])
    assert result.exit_code == 0

    records = latest_digest_records()
    assert len(records) == 1
    assert records[0]["listing_appl_no"] == "020503"
    assert records[0]["psg_version_id"] > 0
    assert records[0]["rationale"] == "canonical"


def test_watch_second_identical_run_emits_no_new_alert(monkeypatch: pytest.MonkeyPatch) -> None:
    """Idempotence: an unchanged PSG must NOT add a second alert (INV-4).

    The first run's alert persists durably in the `alert` table; the identical
    re-run builds no alert (unchanged outcome) and the ON CONFLICT DO NOTHING
    guard means it is never duplicated — and, unlike the old per-file digest,
    never erased either.
    """
    _seed_watchlist()
    _patch_crawl(monkeypatch, [_listing()])
    _patch_pdf(monkeypatch, {"hash": "hash-v1", "pages": PAGES})

    first = runner.invoke(app, ["watch", "--no-extract"])
    assert first.exit_code == 0
    assert len(latest_digest_records()) == 1

    second = runner.invoke(app, ["watch", "--no-extract"])
    assert second.exit_code == 0
    assert len(latest_digest_records()) == 1  # original persists; no duplicate


def test_watch_revision_alerts_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real content change after a clean run must produce a fresh alert.

    Durable semantics: the revision creates a new psg_version, so it alerts on a
    new version_id — and the original capture's alert is NOT erased. Both events
    persist, newest first.
    """
    _seed_watchlist()
    _patch_crawl(monkeypatch, [_listing()])
    state: dict[str, Any] = {"hash": "hash-v1", "pages": PAGES}
    _patch_pdf(monkeypatch, state)

    assert runner.invoke(app, ["watch", "--no-extract"]).exit_code == 0
    state["hash"] = "hash-v2"
    state["pages"] = [PAGES[0], PAGES[1] + "\nRevised dissolution recommendation."]

    result = runner.invoke(app, ["watch", "--no-extract"])
    assert result.exit_code == 0
    records = latest_digest_records()
    assert len(records) == 2  # original capture + revision both persist
    assert records[0]["listing_appl_no"] == "020503"
    # distinct versions: the revision (newest) and the original capture
    assert records[0]["psg_version_id"] != records[1]["psg_version_id"]


def test_watch_unmatched_listing_is_not_ingested_or_alerted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_watchlist("Albuterol Sulfate")
    _patch_crawl(monkeypatch, [_listing(appl_no="999999", name="Romidepsin")])

    def boom(url: str, *, client: object | None = None) -> tuple[Path, bytes, str]:
        raise AssertionError("unmatched listing must never be downloaded")

    monkeypatch.setattr(pipeline_mod, "download_pdf", boom)

    result = runner.invoke(app, ["watch", "--no-extract"])
    assert result.exit_code == 0
    assert latest_digest_records() == []


def test_watch_failed_ingest_never_alerts(monkeypatch: pytest.MonkeyPatch) -> None:
    """INV-4: a match whose version was never actually ingested must not alert."""
    _seed_watchlist()
    _patch_crawl(monkeypatch, [_listing()])

    def boom(url: str, *, client: object | None = None) -> tuple[Path, bytes, str]:
        raise RuntimeError("download failed")

    monkeypatch.setattr(pipeline_mod, "download_pdf", boom)

    result = runner.invoke(app, ["watch", "--no-extract"])
    assert result.exit_code == 2
    assert latest_digest_records() == []


def test_watch_errored_run_does_not_erase_prior_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed re-run must NOT overwrite an earlier same-day digest with an empty
    all-clear file — a failed run cannot masquerade as a quiet day (INV-4)."""
    _seed_watchlist()
    _patch_crawl(monkeypatch, [_listing()])
    _patch_pdf(monkeypatch, {"hash": "hash-v1", "pages": PAGES})

    # Morning run: a real alert is written to today's digest.
    assert runner.invoke(app, ["watch", "--no-extract"]).exit_code == 0
    morning = latest_digest_records()
    assert len(morning) == 1

    # A later run the SAME day fails to ingest (download error) → exit 2, no alerts.
    def boom(url: str, *, client: object | None = None) -> tuple[Path, bytes, str]:
        raise RuntimeError("download failed")

    monkeypatch.setattr(pipeline_mod, "download_pdf", boom)
    assert runner.invoke(app, ["watch", "--no-extract"]).exit_code == 2

    # The morning alert is preserved — not clobbered by an empty all-clear file.
    assert latest_digest_records() == morning
