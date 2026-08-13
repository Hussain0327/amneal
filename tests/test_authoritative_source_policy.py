from __future__ import annotations

import pytest

from regwatch.sources.policy import (
    FdaSourceFamily,
    SourcePolicyError,
    allowed_source_families,
    normalize_authoritative_url,
)


def test_source_universe_is_exact_and_stable() -> None:
    assert allowed_source_families() == (
        "drugs_at_fda",
        "action_package",
        "psg",
        "fda_be_guidance",
        "orange_book",
    )


@pytest.mark.parametrize(
    ("url", "family"),
    [
        (
            "https://www.fda.gov/media/89850/download?attachment=",
            FdaSourceFamily.DRUGS_AT_FDA,
        ),
        (
            "https://www.accessdata.fda.gov/drugsatfda_docs/label/2026/123456lbl.pdf",
            FdaSourceFamily.DRUGS_AT_FDA,
        ),
        (
            "https://www.accessdata.fda.gov/drugsatfda_docs/nda/2026/review.pdf",
            FdaSourceFamily.ACTION_PACKAGE,
        ),
        (
            "https://www.accessdata.fda.gov/drugsatfda_docs/psg/PSG_123456.pdf",
            FdaSourceFamily.PSG,
        ),
        (
            "https://www.fda.gov/regulatory-information/search-fda-guidance-documents/example",
            FdaSourceFamily.FDA_BE_GUIDANCE,
        ),
        ("https://www.fda.gov/media/165049/download", FdaSourceFamily.FDA_BE_GUIDANCE),
        ("https://www.fda.gov/media/76860/download", FdaSourceFamily.ORANGE_BOOK),
    ],
)
def test_policy_accepts_only_family_appropriate_fda_paths(
    url: str, family: FdaSourceFamily
) -> None:
    assert normalize_authoritative_url(url, family).startswith("https://")


def test_historical_drugsfda_http_link_is_upgraded() -> None:
    assert (
        normalize_authoritative_url(
            "http://www.accessdata.fda.gov/drugsatfda_docs/appletter/2003/letter.pdf",
            FdaSourceFamily.DRUGS_AT_FDA,
        )
        == "https://www.accessdata.fda.gov/drugsatfda_docs/appletter/2003/letter.pdf"
    )
    assert (
        normalize_authoritative_url(
            "http://www.fda.gov/downloads/Drugs/DrugSafety/medication-guide.pdf",
            FdaSourceFamily.DRUGS_AT_FDA,
        )
        == "https://www.fda.gov/downloads/Drugs/DrugSafety/medication-guide.pdf"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://api.fda.gov/drug/drugsfda.json",
        "https://open.fda.gov/apis/drug/drugsfda/",
        "https://download.open.fda.gov/example.zip",
        "https://evil.example/fda.pdf",
        "file:///etc/passwd",
        "https://user:pass@www.fda.gov/media/89850/download",
    ],
)
def test_policy_rejects_retired_api_and_non_fda_sources(url: str) -> None:
    with pytest.raises(SourcePolicyError):
        normalize_authoritative_url(url, FdaSourceFamily.DRUGS_AT_FDA)


def test_policy_rejects_cross_family_fda_path() -> None:
    with pytest.raises(SourcePolicyError, match="not approved"):
        normalize_authoritative_url(
            "https://www.fda.gov/media/76860/download",
            FdaSourceFamily.DRUGS_AT_FDA,
        )
