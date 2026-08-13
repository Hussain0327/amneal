from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime

import pytest

from regwatch.sources import drugsfda
from regwatch.sources.drugsfda import (
    DrugsFdaHandler,
    classify_application_document,
    parse_drugsfda_zip,
    render_application_metadata,
)
from regwatch.sources.policy import FdaDocumentType, FdaSourceFamily
from regwatch.sources.types import SourceQuery


def _snapshot_zip(*, document_url: str | None = None) -> bytes:
    tables = {
        "ActionTypes_Lookup.txt": "ActionTypes_LookupID\tActionTypes_LookupDescription\n1\tApproval\n",
        "ApplicationDocs.txt": (
            "ApplicationDocsID\tApplicationDocsTypeID\tApplNo\tSubmissionType\tSubmissionNo\t"
            "ApplicationDocsTitle\tApplicationDocsURL\tApplicationDocsDate\n"
            "77\t3\t020503\tORIG\t1\tClinical Pharmacology Review\t"
            f"{document_url or 'http://www.accessdata.fda.gov/drugsatfda_docs/nda/2026/review.pdf'}"
            "\t2026-08-01 00:00:00\n"
        ),
        "Applications.txt": (
            "ApplNo\tApplType\tApplPublicNotes\tSponsorName\n"
            "020503\tNDA\tOfficial note\tFDA SPONSOR\n"
        ),
        "ApplicationsDocsType_Lookup.txt": (
            "ApplicationDocsType_Lookup_ID\tApplicationDocsType_Lookup_Description\n3\tReview\n"
        ),
        "Join_Submission_ActionTypes_Lookup.txt": (
            "J_SubmissionActionTypeID\tSubmissionNo\tSubmissionType\tApplNo\tActionTypes_LookupID\n"
            "1\t1\tORIG\t020503\t1\n"
        ),
        "MarketingStatus.txt": "ApplNo\tProductNo\tMarketingStatusID\n020503\t001\t1\n",
        "MarketingStatus_Lookup.txt": (
            "MarketingStatusID\tMarketingStatusDescription\n1\tPrescription\n"
        ),
        "Products.txt": (
            "ApplNo\tProductNo\tForm\tStrength\tReferenceDrug\tDrugName\t"
            "ActiveIngredient\tReferenceStandard\n"
            "020503\t001\tAEROSOL, METERED;INHALATION\t0.09MG/INH\t1\tPROVENTIL HFA\t"
            "ALBUTEROL SULFATE\t1\n"
        ),
        "SubmissionClass_Lookup.txt": (
            "SubmissionClassCodeID\tSubmissionClassCode\tSubmissionClassCodeDescription\n"
            "1\tTYPE 1\tNew molecular entity\n"
        ),
        "SubmissionPropertyType.txt": (
            "ApplNo\tSubmissionType\tSubmissionNo\tSubmissionPropertyTypeCode\t"
            "SubmissionPropertyTypeID\n020503\tORIG\t1\tORPHAN\t1\n"
        ),
        "Submissions.txt": (
            "ApplNo\tSubmissionClassCodeID\tSubmissionType\tSubmissionNo\tSubmissionStatus\t"
            "SubmissionStatusDate\tSubmissionsPublicNotes\tReviewPriority\n"
            "020503\t1\tORIG\t1\tAP\t1996-12-04 00:00:00\tApproved\tSTANDARD\n"
        ),
        "TE.txt": "ApplNo\tProductNo\tMarketingStatusID\tTECode\n020503\t001\t1\tAB\n",
    }
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in tables.items():
            archive.writestr(name, text.encode("cp1252"))
    return out.getvalue()


def test_snapshot_parses_twelve_official_tables_and_links() -> None:
    snapshot = parse_drugsfda_zip(_snapshot_zip(), fetched_at=datetime(2026, 8, 13, tzinfo=UTC))
    assert snapshot.application("NDA020503") == {
        "ApplNo": "020503",
        "ApplType": "NDA",
        "ApplPublicNotes": "Official note",
        "SponsorName": "FDA SPONSOR",
    }
    document = snapshot.application_documents("020503")[0]
    assert document.source_family == FdaSourceFamily.ACTION_PACKAGE
    assert document.document_type == FdaDocumentType.CLINICAL_PHARMACOLOGY_REVIEW
    assert document.source_url.startswith("https://www.accessdata.fda.gov/")


def test_handler_maps_official_snapshot_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = parse_drugsfda_zip(_snapshot_zip(), fetched_at=datetime.now(UTC))
    monkeypatch.setattr(drugsfda, "get_drugsfda_snapshot", lambda client=None: snapshot)
    records = DrugsFdaHandler().search(SourceQuery(application_number="NDA020503"))
    assert len(records) == 1
    record = records[0]
    assert record.identifiers == {"application_number": "NDA020503"}
    assert record.fields["sponsor_name"] == "FDA SPONSOR"
    assert record.fields["products"][0]["reference_drug"] is True
    assert record.fields["products"][0]["te_codes"] == ["AB"]


def test_metadata_render_is_deterministic_and_citable() -> None:
    snapshot = parse_drugsfda_zip(_snapshot_zip(), fetched_at=datetime.now(UTC))
    text = render_application_metadata(snapshot, "020503")
    assert text.startswith("Drugs@FDA: NDA020503\nSponsor: FDA SPONSOR")
    assert "reference drug yes" in text
    assert "therapeutic equivalence codes AB" in text
    assert "Submission ORIG 1: status AP" in text


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Statistical Review", FdaDocumentType.STATISTICAL_REVIEW),
        ("CMC Quality Review", FdaDocumentType.CMC_QUALITY_REVIEW),
        ("Integrated Review", FdaDocumentType.INTEGRATED_REVIEW),
        ("Multidisciplinary Review", FdaDocumentType.MULTIDISCIPLINARY_REVIEW),
        ("Review", FdaDocumentType.OTHER_REVIEW),
    ],
)
def test_review_classifier_does_not_guess_ambiguous_discipline(
    title: str, expected: FdaDocumentType
) -> None:
    assert (
        classify_application_document(
            "3",
            "Review",
            title,
            "https://www.accessdata.fda.gov/drugsatfda_docs/nda/2026/review.pdf",
        )
        == expected
    )


def test_snapshot_rejects_document_url_outside_authorized_fda_sources() -> None:
    snapshot = parse_drugsfda_zip(
        _snapshot_zip(document_url="https://api.fda.gov/drug/drugsfda.json"),
        fetched_at=datetime.now(UTC),
    )
    assert snapshot.application_documents("020503") == []
    assert snapshot.rejected_document_links == (
        {
            "application_docs_id": "77",
            "application_number": "020503",
            "source_family": "action_package",
            "document_type": "clinical_pharmacology_review",
            "reason": "unapproved FDA source host: api.fda.gov",
        },
    )


def test_snapshot_rejects_missing_required_member() -> None:
    source = io.BytesIO(_snapshot_zip())
    rebuilt = io.BytesIO()
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(rebuilt, "w") as out:
        for info in original.infolist():
            if info.filename != "Products.txt":
                out.writestr(info, original.read(info))
    with pytest.raises(RuntimeError, match=r"exactly one Products\.txt"):
        parse_drugsfda_zip(rebuilt.getvalue(), fetched_at=datetime.now(UTC))
