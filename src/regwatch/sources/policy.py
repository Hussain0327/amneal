"""Fail-closed policy for the authoritative FDA source universe.

The corpus is deliberately smaller than "anything published by FDA".  Every
network artifact must belong to one of the five source families below and must
come from an FDA-owned HTTPS endpoint whose path is appropriate for that
family.  Callers validate both the requested URL and every redirect target.

This module is the source boundary.  Adding a host or path is a reviewed code
change, never a runtime configuration knob.
"""

from __future__ import annotations

from enum import StrEnum
from urllib.parse import unquote, urlsplit, urlunsplit


class SourcePolicyError(ValueError):
    """A URL or source kind is outside the approved corpus boundary."""


class FdaSourceFamily(StrEnum):
    DRUGS_AT_FDA = "drugs_at_fda"
    ACTION_PACKAGE = "action_package"
    PSG = "psg"
    FDA_BE_GUIDANCE = "fda_be_guidance"
    ORANGE_BOOK = "orange_book"


class FdaDocumentType(StrEnum):
    APPLICATION_METADATA = "application_metadata"
    APPROVED_LABEL = "approved_label"
    APPROVAL_LETTER = "approval_letter"
    REGULATORY_ACTION = "regulatory_action"
    CLINICAL_REVIEW = "clinical_review"
    STATISTICAL_REVIEW = "statistical_review"
    CLINICAL_PHARMACOLOGY_REVIEW = "clinical_pharmacology_review"
    CMC_QUALITY_REVIEW = "cmc_quality_review"
    INTEGRATED_REVIEW = "integrated_review"
    MULTIDISCIPLINARY_REVIEW = "multidisciplinary_review"
    OTHER_REVIEW = "other_review"
    PRODUCT_SPECIFIC_GUIDANCE = "product_specific_guidance"
    BIOEQUIVALENCE_GUIDANCE = "bioequivalence_guidance"
    ORANGE_BOOK_PRODUCT = "orange_book_product"
    ORANGE_BOOK_PATENT = "orange_book_patent"
    ORANGE_BOOK_EXCLUSIVITY = "orange_book_exclusivity"


_FDA_HOSTS = frozenset(
    {
        "fda.gov",
        "www.fda.gov",
        "accessdata.fda.gov",
        "www.accessdata.fda.gov",
    }
)

# These are intentionally family-specific.  A broad ``*.fda.gov`` allowlist
# would admit a retired API again and would also turn any future FDA-hosted
# redirector into an SSRF trampoline.
_PATH_PREFIXES: dict[FdaSourceFamily, tuple[str, ...]] = {
    FdaSourceFamily.DRUGS_AT_FDA: (
        "/drugs/drug-approvals-and-databases/drugsfda-data-files",
        "/media/89850/download",
        "/scripts/cder/daf/",
        "/drugsatfda_docs/",
        # Historical Drugs@FDA rows link approved Medication Guides and
        # regulatory actions at these former FDA paths. They remain constrained
        # to FDA-owned hosts and are upgraded to HTTPS below.
        "/downloads/drugs/",
        "/drugs/drugsafety/",
        "/drugs/drug-safety-and-availability/",
        "/drugs/postmarket-drug-safety-information-patients-and-providers/",
        "/drugs/emergencypreparedness/",
        "/newsevents/newsroom/pressannouncements/",
        "/cder/drug/",
    ),
    FdaSourceFamily.ACTION_PACKAGE: (
        "/scripts/cder/daf/",
        "/drugsatfda_docs/",
    ),
    FdaSourceFamily.PSG: (
        "/drugs/guidances-drugs/product-specific-guidances-generic-drug-development",
        "/scripts/cder/psg/",
        "/drugsatfda_docs/psg/",
    ),
    FdaSourceFamily.FDA_BE_GUIDANCE: (
        "/regulatory-information/search-fda-guidance-documents/",
        "/media/",
    ),
    FdaSourceFamily.ORANGE_BOOK: (
        "/drugs/drug-approvals-and-databases/orange-book-data-files",
        "/drugs/drug-approvals-and-databases/orange-book-",
        "/media/76860/download",
        "/scripts/cder/ob/",
        "/drugsatfda_docs/ob/",
    ),
}

_BE_GUIDANCE_MEDIA_PATHS = frozenset(
    {
        "/media/163638/download",
        "/media/165049/download",
        "/media/183189/download",
        "/media/186703/download",
        "/media/192774/download",
    }
)


def normalize_authoritative_url(url: str, family: FdaSourceFamily) -> str:
    """Return a canonical HTTPS FDA URL or raise :class:`SourcePolicyError`.

    The Drugs@FDA data file still contains historical ``http://`` links on both
    approved FDA hosts. FDA serves those resources over HTTPS, so those links
    are upgraded before validation. No other scheme rewrite is allowed.
    """

    raw = (url or "").strip()
    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise SourcePolicyError(f"invalid FDA source URL: {url!r}") from exc

    host = (parts.hostname or "").lower().rstrip(".")
    if not host or host not in _FDA_HOSTS:
        raise SourcePolicyError(f"unapproved FDA source host: {host or '<missing>'}")
    if parts.username or parts.password:
        raise SourcePolicyError("FDA source URLs may not contain credentials")
    try:
        port = parts.port
    except ValueError as exc:
        raise SourcePolicyError("FDA source URL has an invalid port") from exc
    scheme = parts.scheme.lower()
    if scheme == "http" and host in _FDA_HOSTS:
        if port not in (None, 80):
            raise SourcePolicyError(f"unapproved FDA source port: {port}")
        scheme = "https"
        port = None
    elif scheme == "https":
        if port not in (None, 443):
            raise SourcePolicyError(f"unapproved FDA source port: {port}")
    else:
        raise SourcePolicyError("FDA source URLs must use HTTPS")

    path = parts.path or "/"
    decoded_segments = unquote(path).replace("\\", "/").split("/")
    if any(segment in {".", ".."} for segment in decoded_segments):
        raise SourcePolicyError("FDA source URL contains an unsafe path segment")
    prefixes = _PATH_PREFIXES[family]
    lower_path = path.lower()
    prefix_allowed = any(
        lower_path == prefix.lower().rstrip("/")
        or lower_path.startswith(prefix.lower().rstrip("/") + "/")
        or (prefix.endswith("/") and lower_path.startswith(prefix.lower()))
        for prefix in prefixes
    )
    if (
        family is FdaSourceFamily.FDA_BE_GUIDANCE
        and lower_path.startswith("/media/")
        and lower_path not in _BE_GUIDANCE_MEDIA_PATHS
    ):
        prefix_allowed = False
    if not prefix_allowed:
        raise SourcePolicyError(f"URL path is not approved for {family.value}: {path!r}")

    # Canonicalize the two accepted host spellings and strip fragments, which
    # are client-side state and cannot identify source bytes.
    canonical_host = "www." + host if host in {"fda.gov", "accessdata.fda.gov"} else host
    netloc = canonical_host
    if port not in (None, 443):
        netloc = f"{canonical_host}:{port}"
    return urlunsplit(("https", netloc, path, parts.query, ""))


def assert_authoritative_url(url: str, family: FdaSourceFamily) -> None:
    """Validate ``url`` without changing it."""

    normalize_authoritative_url(url, family)


def allowed_source_families() -> tuple[str, ...]:
    """Stable, display-ready source policy used by status and tests."""

    return tuple(family.value for family in FdaSourceFamily)
