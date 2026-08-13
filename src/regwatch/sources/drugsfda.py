"""Official Drugs@FDA snapshot adapter.

FDA publishes a business-day ZIP containing twelve tab-delimited tables.  This
module treats that ZIP as the structured source of truth for
applications, products, submissions, regulatory history, labels, approval
letters, and review/action-package links.
"""

from __future__ import annotations

import csv
import hashlib
import io
import time
import zipfile
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

import httpx
from config.settings import get_settings

from regwatch.common.text_normalize import canonical_name
from regwatch.sources._utils import bare_application_number, clean_text
from regwatch.sources.http import get_authoritative_bytes, owned_fda_client
from regwatch.sources.policy import (
    FdaDocumentType,
    FdaSourceFamily,
    SourcePolicyError,
    normalize_authoritative_url,
)
from regwatch.sources.types import SourceKind, SourceQuery, SourceRecord

DRUGSFDA_ZIP_URL = "https://www.fda.gov/media/89850/download?attachment="
DRUGSFDA_DOC_URL = "https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm"

_MEMBERS = (
    "ActionTypes_Lookup.txt",
    "ApplicationDocs.txt",
    "Applications.txt",
    "ApplicationsDocsType_Lookup.txt",
    "Join_Submission_ActionTypes_Lookup.txt",
    "MarketingStatus.txt",
    "MarketingStatus_Lookup.txt",
    "Products.txt",
    "SubmissionClass_Lookup.txt",
    "SubmissionPropertyType.txt",
    "Submissions.txt",
    "TE.txt",
)
_MAX_ARCHIVE_MEMBERS = 32
_MAX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024

_REQUIRED_COLUMNS: dict[str, frozenset[str]] = {
    "Applications.txt": frozenset({"ApplNo", "ApplType", "SponsorName"}),
    "Products.txt": frozenset(
        {
            "ApplNo",
            "ProductNo",
            "Form",
            "Strength",
            "ReferenceDrug",
            "DrugName",
            "ActiveIngredient",
            "ReferenceStandard",
        }
    ),
    "Submissions.txt": frozenset(
        {
            "ApplNo",
            "SubmissionType",
            "SubmissionNo",
            "SubmissionStatus",
            "SubmissionStatusDate",
        }
    ),
    "ApplicationDocs.txt": frozenset(
        {
            "ApplicationDocsID",
            "ApplicationDocsTypeID",
            "ApplNo",
            "SubmissionType",
            "SubmissionNo",
            "ApplicationDocsTitle",
            "ApplicationDocsURL",
            "ApplicationDocsDate",
        }
    ),
    "ApplicationsDocsType_Lookup.txt": frozenset(
        {"ApplicationDocsType_Lookup_ID", "ApplicationDocsType_Lookup_Description"}
    ),
    "MarketingStatus.txt": frozenset({"ApplNo", "ProductNo", "MarketingStatusID"}),
    "MarketingStatus_Lookup.txt": frozenset({"MarketingStatusID", "MarketingStatusDescription"}),
    "TE.txt": frozenset({"ApplNo", "ProductNo", "TECode"}),
}


@dataclass(frozen=True)
class DrugsFdaDocument:
    application_docs_id: str
    application_number: str
    application_type: str
    submission_type: str
    submission_number: str
    title: str
    source_url: str
    document_date: str | None
    document_type: FdaDocumentType
    source_family: FdaSourceFamily

    @property
    def canonical_id(self) -> str:
        return f"drugs-at-fda:application-doc:{self.application_docs_id}"


@dataclass(frozen=True)
class DrugsFdaSnapshot:
    """One internally consistent Drugs@FDA ZIP snapshot."""

    applications: tuple[dict[str, str], ...]
    products: tuple[dict[str, str], ...]
    submissions: tuple[dict[str, str], ...]
    documents: tuple[dict[str, str], ...]
    document_types: Mapping[str, str]
    marketing_statuses: Mapping[str, str]
    product_marketing_status: Mapping[tuple[str, str], tuple[str, ...]]
    product_te_codes: Mapping[tuple[str, str], tuple[str, ...]]
    application_by_number: Mapping[str, dict[str, str]]
    products_by_application: Mapping[str, tuple[dict[str, str], ...]]
    submissions_by_application: Mapping[str, tuple[dict[str, str], ...]]
    documents_by_application: Mapping[str, tuple[dict[str, str], ...]]
    fetched_at: datetime
    snapshot_sha256: str
    rejected_document_links: tuple[dict[str, str], ...]

    def application(self, appl_no: str | None) -> dict[str, str] | None:
        bare = bare_application_number(appl_no)
        if bare is None:
            return None
        return self.application_by_number.get(bare)

    def application_products(self, appl_no: str | None) -> list[dict[str, str]]:
        bare = bare_application_number(appl_no)
        if bare is None:
            return []
        return list(self.products_by_application.get(bare, ()))

    def application_submissions(self, appl_no: str | None) -> list[dict[str, str]]:
        bare = bare_application_number(appl_no)
        if bare is None:
            return []
        return list(self.submissions_by_application.get(bare, ()))

    def application_documents(self, appl_no: str | None) -> list[DrugsFdaDocument]:
        application = self.application(appl_no)
        if application is None:
            return []
        bare = application["ApplNo"]
        appl_type = application.get("ApplType") or ""
        out: list[DrugsFdaDocument] = []
        for row in self.documents_by_application.get(bare, ()):
            if not row.get("ApplicationDocsURL"):
                continue
            type_id = row.get("ApplicationDocsTypeID") or ""
            type_label = self.document_types.get(type_id, "")
            title = row.get("ApplicationDocsTitle") or type_label or "Drugs@FDA document"
            document_type = classify_application_document(
                type_id, type_label, title, row["ApplicationDocsURL"]
            )
            family = _document_family(document_type)
            try:
                source_url = normalize_authoritative_url(row["ApplicationDocsURL"], family)
            except SourcePolicyError:
                # The live snapshot contains a small, explicitly measured set
                # of external, malformed, and out-of-policy links. Those rows
                # are excluded from the corpus instead of weakening the source
                # boundary or aborting discovery of every valid FDA document.
                continue
            out.append(
                DrugsFdaDocument(
                    application_docs_id=row.get("ApplicationDocsID") or "",
                    application_number=bare,
                    application_type=appl_type,
                    submission_type=(row.get("SubmissionType") or "").strip(),
                    submission_number=row.get("SubmissionNo") or "",
                    title=title,
                    source_url=source_url,
                    document_date=row.get("ApplicationDocsDate") or None,
                    document_type=document_type,
                    source_family=family,
                )
            )
        return sorted(
            out,
            key=lambda doc: (
                doc.document_date or "",
                doc.application_docs_id.zfill(12),
            ),
        )


@dataclass(frozen=True)
class _SnapshotCache:
    snapshot: DrugsFdaSnapshot
    monotonic_at: float


_CACHE: _SnapshotCache | None = None


def reset_snapshot_cache() -> None:
    global _CACHE
    _CACHE = None


def get_drugsfda_snapshot(*, client: httpx.Client | None = None) -> DrugsFdaSnapshot:
    global _CACHE
    settings = get_settings()
    cached = _CACHE
    if (
        cached is not None
        and settings.drugsfda_cache_ttl_s > 0
        and time.monotonic() - cached.monotonic_at < settings.drugsfda_cache_ttl_s
    ):
        return cached.snapshot
    with owned_fda_client(client) as active_client:
        _, body, _ = get_authoritative_bytes(
            active_client,
            DRUGSFDA_ZIP_URL,
            FdaSourceFamily.DRUGS_AT_FDA,
            max_bytes=settings.drugsfda_zip_max_bytes,
        )
    snapshot = parse_drugsfda_zip(body, fetched_at=datetime.now(UTC))
    _CACHE = _SnapshotCache(snapshot=snapshot, monotonic_at=time.monotonic())
    return snapshot


def parse_drugsfda_zip(content: bytes, *, fetched_at: datetime) -> DrugsFdaSnapshot:
    """Parse and validate one official twelve-table snapshot."""

    files = _validated_zip_texts(content)
    parsed = {member: _parse_table(member, text) for member, text in files.items()}
    document_types = {
        row["ApplicationDocsType_Lookup_ID"]: row["ApplicationDocsType_Lookup_Description"]
        for row in parsed["ApplicationsDocsType_Lookup.txt"]
    }
    marketing_statuses = {
        row["MarketingStatusID"]: row["MarketingStatusDescription"]
        for row in parsed["MarketingStatus_Lookup.txt"]
    }
    statuses: dict[tuple[str, str], list[str]] = defaultdict(list)
    for row in parsed["MarketingStatus.txt"]:
        value = marketing_statuses.get(row.get("MarketingStatusID") or "")
        if value:
            statuses[(row.get("ApplNo") or "", row.get("ProductNo") or "")].append(value)
    te_codes: dict[tuple[str, str], list[str]] = defaultdict(list)
    for row in parsed["TE.txt"]:
        value = row.get("TECode")
        if value:
            te_codes[(row.get("ApplNo") or "", row.get("ProductNo") or "")].append(value)
    applications_by_number = {
        row["ApplNo"]: row for row in parsed["Applications.txt"] if row.get("ApplNo")
    }
    rejections = _document_policy_rejections(parsed["ApplicationDocs.txt"], document_types)
    products_by_application = _group_by_application(parsed["Products.txt"])
    submissions_by_application = _group_by_application(parsed["Submissions.txt"])
    documents_by_application = _group_by_application(parsed["ApplicationDocs.txt"])
    return DrugsFdaSnapshot(
        applications=tuple(parsed["Applications.txt"]),
        products=tuple(parsed["Products.txt"]),
        submissions=tuple(parsed["Submissions.txt"]),
        documents=tuple(parsed["ApplicationDocs.txt"]),
        document_types=MappingProxyType(document_types),
        marketing_statuses=MappingProxyType(marketing_statuses),
        product_marketing_status=MappingProxyType(
            {key: tuple(dict.fromkeys(values)) for key, values in statuses.items()}
        ),
        product_te_codes=MappingProxyType(
            {key: tuple(dict.fromkeys(values)) for key, values in te_codes.items()}
        ),
        application_by_number=MappingProxyType(applications_by_number),
        products_by_application=MappingProxyType(products_by_application),
        submissions_by_application=MappingProxyType(submissions_by_application),
        documents_by_application=MappingProxyType(documents_by_application),
        fetched_at=fetched_at,
        snapshot_sha256=hashlib.sha256(content).hexdigest(),
        rejected_document_links=tuple(rejections),
    )


_ACTION_PACKAGE_TYPES = frozenset(
    {
        FdaDocumentType.CLINICAL_REVIEW,
        FdaDocumentType.STATISTICAL_REVIEW,
        FdaDocumentType.CLINICAL_PHARMACOLOGY_REVIEW,
        FdaDocumentType.CMC_QUALITY_REVIEW,
        FdaDocumentType.INTEGRATED_REVIEW,
        FdaDocumentType.MULTIDISCIPLINARY_REVIEW,
        FdaDocumentType.OTHER_REVIEW,
    }
)


def _document_family(document_type: FdaDocumentType) -> FdaSourceFamily:
    if document_type in _ACTION_PACKAGE_TYPES:
        return FdaSourceFamily.ACTION_PACKAGE
    return FdaSourceFamily.DRUGS_AT_FDA


def _document_policy_rejections(
    rows: list[dict[str, str]],
    document_types: Mapping[str, str],
) -> list[dict[str, str]]:
    """Return deterministic audit facts for snapshot links excluded by policy."""

    rejected: list[dict[str, str]] = []
    for row in rows:
        source_url = row.get("ApplicationDocsURL") or ""
        if not source_url:
            continue
        type_id = row.get("ApplicationDocsTypeID") or ""
        type_label = document_types.get(type_id, "")
        document_type = classify_application_document(
            type_id,
            type_label,
            row.get("ApplicationDocsTitle") or type_label,
            source_url,
        )
        family = _document_family(document_type)
        try:
            normalize_authoritative_url(source_url, family)
        except SourcePolicyError as exc:
            rejected.append(
                {
                    "application_docs_id": row.get("ApplicationDocsID") or "",
                    "application_number": row.get("ApplNo") or "",
                    "source_family": family.value,
                    "document_type": document_type.value,
                    "reason": str(exc),
                }
            )
    return sorted(rejected, key=lambda item: item["application_docs_id"].zfill(12))


def _group_by_application(
    rows: list[dict[str, str]],
) -> dict[str, tuple[dict[str, str], ...]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        if row.get("ApplNo"):
            grouped[row["ApplNo"]].append(row)
    return {key: tuple(values) for key, values in grouped.items()}


def _validated_zip_texts(content: bytes) -> dict[str, str]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise RuntimeError("Drugs@FDA download is not a valid ZIP") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > _MAX_ARCHIVE_MEMBERS:
            raise RuntimeError("Drugs@FDA ZIP contains too many members")
        if sum(info.file_size for info in infos) > _MAX_UNCOMPRESSED_BYTES:
            raise RuntimeError("Drugs@FDA ZIP exceeds the uncompressed-size limit")
        by_basename: dict[str, list[zipfile.ZipInfo]] = defaultdict(list)
        for info in infos:
            normalized = info.filename.replace("\\", "/")
            if normalized.startswith("/") or ".." in normalized.split("/"):
                raise RuntimeError("Drugs@FDA ZIP contains an unsafe member path")
            by_basename[normalized.rsplit("/", 1)[-1].lower()].append(info)
        out: dict[str, str] = {}
        for member in _MEMBERS:
            matches = by_basename.get(member.lower(), [])
            if len(matches) != 1:
                raise RuntimeError(
                    f"Drugs@FDA ZIP must contain exactly one {member}; found {len(matches)}"
                )
            out[member] = archive.read(matches[0]).decode("cp1252")
        return out


def _parse_table(member: str, text: str) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")), delimiter="\t")
    headers = reader.fieldnames or []
    if len(headers) != len(set(headers)):
        raise RuntimeError(f"Drugs@FDA {member} contains duplicate headers")
    required = _REQUIRED_COLUMNS.get(member, frozenset())
    missing = sorted(required - set(headers))
    if missing:
        raise RuntimeError(f"Drugs@FDA {member} is missing columns: {missing}")
    rows: list[dict[str, str]] = []
    for row in reader:
        normalized = {key: clean_text(value) for key, value in row.items() if key is not None}
        if normalized.get("ApplNo"):
            normalized["ApplNo"] = normalized["ApplNo"].zfill(6)
        rows.append(normalized)
    return rows


def classify_application_document(
    type_id: str,
    type_label: str,
    title: str,
    source_url: str,
) -> FdaDocumentType:
    """Conservative, deterministic classification of a Drugs@FDA document.

    Classification changes retrieval facets, so ambiguous reviews stay
    ``other_review`` rather than being guessed into a scientific discipline.
    """

    if type_id in {"1", "12"}:
        return FdaDocumentType.APPROVAL_LETTER
    if type_id in {"2", "6", "8", "11", "15", "16"}:
        return FdaDocumentType.APPROVED_LABEL
    text = " ".join((type_label, title, source_url)).lower().replace("-", " ")
    if type_id == "17" or "statistical review" in text:
        return FdaDocumentType.STATISTICAL_REVIEW
    if type_id in {"18", "62"} or "clinical pharmacology" in text or "biopharm" in text:
        return FdaDocumentType.CLINICAL_PHARMACOLOGY_REVIEW
    if any(
        term in text
        for term in ("chemistry review", "cmc review", "quality review", "product quality")
    ):
        return FdaDocumentType.CMC_QUALITY_REVIEW
    if "multidisciplin" in text or "multi disciplin" in text:
        return FdaDocumentType.MULTIDISCIPLINARY_REVIEW
    if "integrated review" in text:
        return FdaDocumentType.INTEGRATED_REVIEW
    if type_id in {"20", "58", "59"} or "clinical review" in text or "medical review" in text:
        return FdaDocumentType.CLINICAL_REVIEW
    if (
        type_id in {"3", "17", "18", "20", "21", "57", "58", "59", "60", "61", "62"}
        or "review" in text
    ):
        return FdaDocumentType.OTHER_REVIEW
    return FdaDocumentType.REGULATORY_ACTION


class DrugsFdaHandler:
    source = SourceKind.DRUGSFDA

    def search(
        self,
        query: SourceQuery,
        *,
        client: httpx.Client | None = None,
    ) -> list[SourceRecord]:
        snapshot = get_drugsfda_snapshot(client=client)
        appl_numbers = _matching_application_numbers(snapshot, query)
        return [_record(snapshot, appl_no) for appl_no in appl_numbers[: query.limit]]


def _matching_application_numbers(snapshot: DrugsFdaSnapshot, query: SourceQuery) -> list[str]:
    bare = bare_application_number(query.application_number)
    if bare:
        return [bare] if snapshot.application(bare) is not None else []
    ingredient = canonical_name(query.active_ingredient or "")
    brand = canonical_name(query.brand_name or "")
    if not ingredient and not brand:
        return []
    matches: set[str] = set()
    for product in snapshot.products:
        if ingredient and ingredient not in canonical_name(product.get("ActiveIngredient") or ""):
            continue
        if brand and brand not in canonical_name(product.get("DrugName") or ""):
            continue
        if product.get("ApplNo"):
            matches.add(product["ApplNo"])
    return sorted(matches)


def _record(snapshot: DrugsFdaSnapshot, appl_no: str) -> SourceRecord:
    application = snapshot.application(appl_no)
    if application is None:
        raise KeyError(appl_no)
    appl_type = application.get("ApplType") or ""
    application_number = f"{appl_type}{appl_no}" if appl_type else appl_no
    products: list[dict[str, Any]] = []
    for product in snapshot.application_products(appl_no):
        form, _, route = (product.get("Form") or "").partition(";")
        key = (appl_no, product.get("ProductNo") or "")
        products.append(
            {
                "product_number": product.get("ProductNo"),
                "brand_name": product.get("DrugName"),
                "dosage_form": form or None,
                "route": route or None,
                "marketing_status": list(snapshot.product_marketing_status.get(key, ())),
                "reference_drug": product.get("ReferenceDrug") == "1",
                "reference_standard": product.get("ReferenceStandard") == "1",
                "te_codes": list(snapshot.product_te_codes.get(key, ())),
                "active_ingredients": [
                    {
                        "name": product.get("ActiveIngredient"),
                        "strength": product.get("Strength"),
                    }
                ],
            }
        )
    source_url = f"{DRUGSFDA_DOC_URL}?event=overview.process&ApplNo={appl_no}"
    return SourceRecord(
        source=SourceKind.DRUGSFDA,
        title=f"Drugs@FDA: {application_number}",
        source_url=source_url,
        identifiers={"application_number": application_number},
        fields={
            "application_type": appl_type,
            "sponsor_name": application.get("SponsorName"),
            "public_notes": application.get("ApplPublicNotes"),
            "submissions": snapshot.application_submissions(appl_no),
            "products": products,
            "documents": [
                {
                    "title": doc.title,
                    "url": doc.source_url,
                    "date": doc.document_date,
                    "document_type": doc.document_type.value,
                }
                for doc in snapshot.application_documents(appl_no)
            ],
            "snapshot_sha256": snapshot.snapshot_sha256,
            "fetched_at": snapshot.fetched_at.isoformat(),
        },
        raw=dict(application),
    )


def render_application_metadata(snapshot: DrugsFdaSnapshot, appl_no: str) -> str:
    """Deterministic citable text for one application's structured metadata."""

    record = _record(snapshot, appl_no)
    lines = [record.title]
    sponsor = record.fields.get("sponsor_name")
    if sponsor:
        lines.append(f"Sponsor: {sponsor}")
    notes = record.fields.get("public_notes")
    if notes:
        lines.append(f"Application public notes: {notes}")
    for product in record.fields.get("products") or []:
        lines.append(
            "Product {product_number}: {brand_name}; active ingredient {ingredient}; "
            "strength {strength}; dosage form {dosage_form}; route {route}; "
            "reference drug {reference_drug}; reference standard {reference_standard}; "
            "therapeutic equivalence codes {te_codes}; marketing status {marketing_status}.".format(
                product_number=product.get("product_number") or "unknown",
                brand_name=product.get("brand_name") or "unknown",
                ingredient=(product.get("active_ingredients") or [{}])[0].get("name") or "unknown",
                strength=(product.get("active_ingredients") or [{}])[0].get("strength")
                or "unknown",
                dosage_form=product.get("dosage_form") or "unknown",
                route=product.get("route") or "unknown",
                reference_drug="yes" if product.get("reference_drug") else "no",
                reference_standard="yes" if product.get("reference_standard") else "no",
                te_codes=", ".join(product.get("te_codes") or []) or "none listed",
                marketing_status=", ".join(product.get("marketing_status") or []) or "not listed",
            )
        )
    for submission in record.fields.get("submissions") or []:
        lines.append(
            "Submission {kind} {number}: status {status}; status date {date}; "
            "review priority {priority}; notes {notes}.".format(
                kind=submission.get("SubmissionType") or "unknown",
                number=submission.get("SubmissionNo") or "unknown",
                status=submission.get("SubmissionStatus") or "unknown",
                date=submission.get("SubmissionStatusDate") or "unknown",
                priority=submission.get("ReviewPriority") or "not listed",
                notes=submission.get("SubmissionsPublicNotes") or "none",
            )
        )
    return "\n".join(lines)
