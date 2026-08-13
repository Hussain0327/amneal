"""Discover every document in the approved FDA source universe."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable

import httpx

from regwatch.common.text_normalize import canonical_name
from regwatch.corpus.manifest import (
    CorpusArtifact,
    CorpusManifest,
    guidance_manifest_path,
    load_be_guidance_artifacts,
)
from regwatch.ingest.psg_crawler import PsgListing, fetch_all_listings
from regwatch.sources.drugsfda import (
    DRUGSFDA_DOC_URL,
    DrugsFdaSnapshot,
    get_drugsfda_snapshot,
    render_application_metadata,
)
from regwatch.sources.orange_book import (
    ORANGE_BOOK_SEARCH_URL,
    OrangeBookSnapshot,
    get_orange_book_snapshot,
)
from regwatch.sources.policy import FdaDocumentType, FdaSourceFamily

_APPLICATION_PREFIX = {"N": "NDA", "A": "ANDA", "B": "BLA"}


def discover_authoritative_manifest(
    *,
    families: Iterable[FdaSourceFamily] | None = None,
    application_numbers: Iterable[str] = (),
    client: httpx.Client | None = None,
    drugs_snapshot: DrugsFdaSnapshot | None = None,
    orange_book_snapshot: OrangeBookSnapshot | None = None,
    psg_listings: list[PsgListing] | None = None,
) -> CorpusManifest:
    """Build a deterministic manifest; network discovery is injectable for tests."""
    selected = set(families or FdaSourceFamily)
    requested_apps = {_bare_application(value) for value in application_numbers if value.strip()}
    artifacts: list[CorpusArtifact] = []
    source_snapshots: dict[str, str] = {}

    if selected & {FdaSourceFamily.DRUGS_AT_FDA, FdaSourceFamily.ACTION_PACKAGE}:
        snapshot = (
            drugs_snapshot if drugs_snapshot is not None else get_drugsfda_snapshot(client=client)
        )
        artifacts.extend(
            artifacts_from_drugsfda(
                snapshot,
                families=selected,
                application_numbers=requested_apps,
            )
        )
        source_snapshots["drugs_at_fda_zip_sha256"] = snapshot.snapshot_sha256
        source_snapshots["drugs_at_fda_rejected_document_links"] = str(
            len(snapshot.rejected_document_links)
        )
        source_snapshots["drugs_at_fda_rejected_links_sha256"] = hashlib.sha256(
            json.dumps(
                snapshot.rejected_document_links,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    if FdaSourceFamily.ORANGE_BOOK in selected:
        orange_snapshot = (
            orange_book_snapshot
            if orange_book_snapshot is not None
            else get_orange_book_snapshot(client=client)
        )
        missing = orange_snapshot.missing_members & {"patent.txt", "exclusivity.txt"}
        if missing:
            raise RuntimeError(
                "Orange Book corpus discovery requires all three files; missing "
                + ", ".join(sorted(missing))
            )
        artifacts.extend(
            artifacts_from_orange_book(orange_snapshot, application_numbers=requested_apps)
        )
        source_snapshots["orange_book_zip_sha256"] = orange_snapshot.snapshot_sha256

    if FdaSourceFamily.PSG in selected:
        listings = psg_listings if psg_listings is not None else fetch_all_listings()
        psg_artifacts = artifacts_from_psg(listings, application_numbers=requested_apps)
        artifacts.extend(psg_artifacts)
        source_snapshots["psg_catalog_sha256"] = _artifact_list_hash(psg_artifacts)

    if FdaSourceFamily.FDA_BE_GUIDANCE in selected:
        guidance = load_be_guidance_artifacts()
        artifacts.extend(guidance)
        source_snapshots["be_guidance_manifest_sha256"] = hashlib.sha256(
            guidance_manifest_path().read_bytes()
        ).hexdigest()

    return CorpusManifest(
        artifacts=tuple(artifacts),
        source_snapshots=source_snapshots,
        complete_universe=(selected == set(FdaSourceFamily) and not requested_apps),
    )


def artifacts_from_drugsfda(
    snapshot: DrugsFdaSnapshot,
    *,
    families: set[FdaSourceFamily],
    application_numbers: set[str] | None = None,
) -> list[CorpusArtifact]:
    """Application metadata plus every published label, letter, and review."""
    requested = application_numbers or set()
    out: list[CorpusArtifact] = []
    for application in sorted(
        snapshot.applications,
        key=lambda row: ((row.get("ApplType") or ""), (row.get("ApplNo") or "")),
    ):
        bare = application.get("ApplNo") or ""
        if not bare or (requested and bare not in requested):
            continue
        appl_type = application.get("ApplType") or ""
        application_number = f"{appl_type}{bare}" if appl_type else bare
        products = snapshot.application_products(bare)
        primary = products[0] if products else {}
        form, _, route = (primary.get("Form") or "").partition(";")
        identity = {
            "application_number": application_number,
            "active_ingredient": primary.get("ActiveIngredient") or None,
            "normalized_name": canonical_name(primary.get("ActiveIngredient") or "") or None,
            "brand_name": primary.get("DrugName") or None,
            "dosage_form": form or None,
            "route": route or None,
        }
        if FdaSourceFamily.DRUGS_AT_FDA in families:
            out.append(
                CorpusArtifact(
                    canonical_id=f"drugs-at-fda:application:{application_number.lower()}",
                    source_family=FdaSourceFamily.DRUGS_AT_FDA,
                    document_type=FdaDocumentType.APPLICATION_METADATA,
                    title=f"Drugs@FDA application {application_number}",
                    source_url=(f"{DRUGSFDA_DOC_URL}?event=overview.process&ApplNo={bare}"),
                    inline_text=render_application_metadata(snapshot, bare),
                    metadata={
                        "snapshot_sha256": snapshot.snapshot_sha256,
                        "sponsor_name": application.get("SponsorName"),
                        "product_count": len(products),
                    },
                    **identity,
                )
            )
        for document in snapshot.application_documents(bare):
            if document.source_family not in families:
                continue
            if not document.application_docs_id:
                raise RuntimeError(
                    f"Drugs@FDA document for {application_number} has no ApplicationDocsID"
                )
            out.append(
                CorpusArtifact(
                    canonical_id=document.canonical_id,
                    source_family=document.source_family,
                    document_type=document.document_type,
                    title=document.title,
                    source_url=document.source_url,
                    source_updated_at=document.document_date,
                    metadata={
                        "snapshot_sha256": snapshot.snapshot_sha256,
                        "application_docs_id": document.application_docs_id,
                        "submission_type": document.submission_type,
                        "submission_number": document.submission_number,
                    },
                    **identity,
                )
            )
    return out


def artifacts_from_orange_book(
    snapshot: OrangeBookSnapshot,
    *,
    application_numbers: set[str] | None = None,
) -> list[CorpusArtifact]:
    """Create independently citable product, patent, and exclusivity records."""
    requested = application_numbers or set()
    products = _group_ob_rows(snapshot.products)
    patents = _group_ob_rows(snapshot.patents)
    exclusivities = _group_ob_rows(snapshot.exclusivities)
    out: list[CorpusArtifact] = []
    for key in sorted(set(products) | set(patents) | set(exclusivities)):
        appl_type, bare = key
        if requested and bare not in requested:
            continue
        application_number = f"{_APPLICATION_PREFIX.get(appl_type, appl_type)}{bare}"
        product_rows = products.get(key, [])
        primary = product_rows[0] if product_rows else {}
        dosage_form, _, route = (primary.get("dosage_form_route") or "").partition(";")
        identity = {
            "application_number": application_number,
            "active_ingredient": primary.get("ingredient") or None,
            "normalized_name": canonical_name(primary.get("ingredient") or "") or None,
            "brand_name": primary.get("trade_name") or None,
            "dosage_form": dosage_form or None,
            "route": route or None,
        }
        for suffix, document_type, rows in (
            ("product", FdaDocumentType.ORANGE_BOOK_PRODUCT, product_rows),
            ("patent", FdaDocumentType.ORANGE_BOOK_PATENT, patents.get(key, [])),
            (
                "exclusivity",
                FdaDocumentType.ORANGE_BOOK_EXCLUSIVITY,
                exclusivities.get(key, []),
            ),
        ):
            if not rows:
                continue
            out.append(
                CorpusArtifact(
                    canonical_id=f"orange-book:{suffix}:{appl_type.lower()}:{bare}",
                    source_family=FdaSourceFamily.ORANGE_BOOK,
                    document_type=document_type,
                    title=f"Orange Book {suffix} records for {application_number}",
                    source_url=ORANGE_BOOK_SEARCH_URL,
                    inline_text=_render_rows(
                        f"Orange Book {suffix} records for {application_number}", rows
                    ),
                    metadata={
                        "snapshot_sha256": snapshot.snapshot_sha256,
                        "row_count": len(rows),
                    },
                    **identity,
                )
            )
    return out


def artifacts_from_psg(
    listings: list[PsgListing],
    *,
    application_numbers: set[str] | None = None,
) -> list[CorpusArtifact]:
    requested = application_numbers or set()
    out: list[CorpusArtifact] = []
    for listing in sorted(listings, key=lambda item: item.appl_no):
        bare = _bare_application(listing.appl_no)
        if requested and bare not in requested:
            continue
        out.append(
            CorpusArtifact(
                canonical_id=f"psg:{bare}",
                source_family=FdaSourceFamily.PSG,
                document_type=FdaDocumentType.PRODUCT_SPECIFIC_GUIDANCE,
                title=f"Product-specific guidance: {listing.active_ingredient}",
                source_url=listing.pdf_url,
                application_number=bare,
                active_ingredient=listing.active_ingredient,
                normalized_name=listing.normalized_name,
                dosage_form=listing.dosage_form,
                route=listing.route,
                source_updated_at=listing.recommended_date,
                metadata={
                    "guidance_status": listing.psg_type,
                    "rld_or_rs_numbers": sorted(listing.rld_or_rs_numbers),
                    "catalog_url": listing.source_url,
                },
            )
        )
    return out


def _group_ob_rows(
    rows: tuple[dict[str, str], ...],
) -> dict[tuple[str, str], list[dict[str, str]]]:
    grouped: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        key = (row.get("appl_type") or "", row.get("appl_no") or "")
        if all(key):
            grouped[key].append(row)
    for values in grouped.values():
        values.sort(key=lambda row: json.dumps(row, sort_keys=True))
    return grouped


def _render_rows(title: str, rows: list[dict[str, str]]) -> str:
    lines = [title]
    for index, row in enumerate(rows, start=1):
        fields = "; ".join(
            f"{key.replace('_', ' ')}: {value}" for key, value in sorted(row.items()) if value
        )
        lines.append(f"Record {index}. {fields}.")
    return "\n".join(lines)


def _bare_application(value: str) -> str:
    digits = "".join(character for character in value if character.isdigit())
    if not digits:
        raise ValueError(f"application number has no digits: {value!r}")
    return digits.zfill(6)


def _artifact_list_hash(artifacts: list[CorpusArtifact]) -> str:
    payload = [artifact.fingerprint_record() for artifact in artifacts]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
