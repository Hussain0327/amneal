"""PubChem lookup for the chemical identity of an active ingredient.

Last updated: 2026-08-21.

THE SAFETY RULE, FIRST
A chemical structure shown beside a regulatory answer is a FIGURE, never a
citable fact, and it is never model-authored. It comes from exactly one
place: a deterministic registry lookup of the product's active-ingredient
NAME against PubChem, run offline by an operator (``regwatch
chemistry-backfill``), stored with its PubChem CID, and drawn from the stored
SMILES in the browser. Ambiguity resolves to NOTHING: a name PubChem maps to
more than one compound is recorded as ambiguous and no structure is shown.
A wrong structure in a regulatory tool is worse than no structure.

EGRESS BOUNDARY
``sources.policy`` is FDA-specific by design, so this module carries its own
fail-closed boundary: every request URL is built here from ``_BASE`` and a
URL-quoted name; no caller-supplied URL is ever fetched, and a redirect is
never followed (``follow_redirects=False``), so a request can only ever reach
``pubchem.ncbi.nlm.nih.gov``. Requests are bounded by ``http_timeout_s`` and
retried only on the transient statuses, with Retry-After honoured, and paced
to stay under PubChem's published 5 requests/second.

Pure parsing (``_parse_*``) is separated from transport (``resolve``) so the
decision logic is testable without a socket.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
from config.settings import get_settings

from regwatch.common.logging import get_logger

log = get_logger(__name__)

_BASE = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
_COMPOUND_URL = "https://pubchem.ncbi.nlm.nih.gov/compound/{cid}"
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_ATTEMPTS = 3
# PubChem's usage policy: no more than 5 requests per second per user.
_MIN_INTERVAL_S = 0.25
# PubChem renamed its SMILES properties in 2025 (IsomericSMILES -> SMILES,
# CanonicalSMILES -> ConnectivitySMILES). An unknown property name is a 400, so
# the request is tried new-names-first and falls back to the old ones.
_PROPERTY_SETS = (
    "MolecularFormula,MolecularWeight,InChIKey,IUPACName,SMILES",
    "MolecularFormula,MolecularWeight,InChIKey,IUPACName,IsomericSMILES",
)
_UNII_RE = re.compile(r"^UNII-([A-Z0-9]{10})$")

STATUS_RESOLVED = "resolved"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_NOT_FOUND = "not_found"


@dataclass(frozen=True)
class ChemistryRecord:
    """What PubChem said about one ingredient name, or why it said nothing."""

    ingredient: str
    status: str
    pubchem_cid: int | None = None
    smiles: str | None = None
    inchikey: str | None = None
    molecular_formula: str | None = None
    molecular_weight: float | None = None
    iupac_name: str | None = None
    unii: str | None = None

    @property
    def source_url(self) -> str | None:
        if self.pubchem_cid is None:
            return None
        return _COMPOUND_URL.format(cid=self.pubchem_cid)


class PubChemError(RuntimeError):
    """PubChem could not be consulted (transport failure after retries)."""


def build_client() -> httpx.Client:
    settings = get_settings()
    return httpx.Client(
        headers={"User-Agent": settings.user_agent, "Accept": "application/json"},
        timeout=settings.http_timeout_s,
        follow_redirects=False,
    )


# -- pure parsing --------------------------------------------------------------


def _parse_cids(payload: dict[str, Any]) -> list[int]:
    cids = payload.get("IdentifierList", {}).get("CID", [])
    return [int(c) for c in cids if isinstance(c, int)]


def _parse_properties(payload: dict[str, Any]) -> dict[str, Any] | None:
    rows = payload.get("PropertyTable", {}).get("Properties", [])
    if not rows or not isinstance(rows[0], dict):
        return None
    row = rows[0]
    smiles = row.get("SMILES") or row.get("IsomericSMILES") or row.get("ConnectivitySMILES")
    if not smiles:
        return None
    weight = row.get("MolecularWeight")
    try:
        weight_f = float(weight) if weight is not None else None
    except (TypeError, ValueError):
        weight_f = None
    return {
        "smiles": str(smiles),
        "inchikey": row.get("InChIKey"),
        "molecular_formula": row.get("MolecularFormula"),
        "molecular_weight": weight_f,
        "iupac_name": row.get("IUPACName"),
    }


def _parse_unii(payload: dict[str, Any]) -> str | None:
    info = payload.get("InformationList", {}).get("Information", [])
    for entry in info:
        for synonym in entry.get("Synonym", []) if isinstance(entry, dict) else []:
            match = _UNII_RE.match(str(synonym))
            if match:
                return match.group(1)
    return None


# -- transport -----------------------------------------------------------------


def _get_json(client: httpx.Client, url: str) -> tuple[int, dict[str, Any]]:
    """One bounded, retried GET. Returns (status, json-or-empty-dict).

    404 and 400 are returned to the caller as decisions (not found / bad
    property set); transient statuses are retried with backoff; anything
    else after the retry budget raises ``PubChemError``.
    """
    last: Exception | None = None
    for attempt in range(_ATTEMPTS):
        time.sleep(_MIN_INTERVAL_S if attempt == 0 else min(0.25 * (2**attempt), 5.0))
        try:
            response = client.get(url)
        except httpx.RequestError as exc:
            # The whole request-side family: transport failures AND a body
            # that fails content decoding (DecodingError is a sibling of
            # TransportError, not a child). Anything here is retried and then
            # surfaced as PubChemError, never as a raw httpx exception.
            last = exc
            continue
        if response.status_code in _RETRYABLE_STATUS:
            retry_after = response.headers.get("retry-after", "").strip()
            if retry_after.isdigit():
                time.sleep(min(float(retry_after), 5.0))
            last = httpx.HTTPStatusError(
                f"pubchem {response.status_code}", request=response.request, response=response
            )
            continue
        if response.status_code in (400, 404):
            return response.status_code, {}
        if response.status_code != 200:
            raise PubChemError(f"pubchem returned {response.status_code} for {url}")
        try:
            body = response.json()
        except ValueError as exc:
            raise PubChemError(f"pubchem returned non-JSON for {url}") from exc
        return 200, body if isinstance(body, dict) else {}
    raise PubChemError(f"pubchem unreachable after {_ATTEMPTS} attempts: {last}")


def resolve(ingredient: str, *, client: httpx.Client | None = None) -> ChemistryRecord:
    """Resolve one ingredient NAME to a PubChem compound, or record why not.

    Args:
        ingredient: A single ingredient name as it appears in the corpus
            ("albuterol sulfate"). Multi-ingredient strings must be split by
            the caller; this function never guesses at separators.
        client: Optional shared client (the backfill reuses one connection).

    Returns:
        A ``ChemistryRecord`` whose ``status`` is resolved, ambiguous or
        not_found. Only a resolved record carries a structure.

    Raises:
        PubChemError: transport failure after retries; the caller decides
            whether to stop or skip. Never raised for a "no such name" answer.
    """
    name = " ".join((ingredient or "").split()).lower()
    if not name:
        return ChemistryRecord(ingredient=ingredient, status=STATUS_NOT_FOUND)
    own = client is None
    session = client or build_client()
    try:
        # safe="" so a "/" inside a name can never become an extra path segment.
        encoded = quote(name, safe="")
        status, payload = _get_json(session, f"{_BASE}/compound/name/{encoded}/cids/JSON")
        if status == 404:
            return ChemistryRecord(ingredient=name, status=STATUS_NOT_FOUND)
        cids = _parse_cids(payload)
        if not cids:
            return ChemistryRecord(ingredient=name, status=STATUS_NOT_FOUND)
        if len(cids) > 1:
            log.info("pubchem_ambiguous", ingredient=name, cids=cids[:10])
            return ChemistryRecord(ingredient=name, status=STATUS_AMBIGUOUS)
        cid = cids[0]

        props: dict[str, Any] | None = None
        for property_set in _PROPERTY_SETS:
            status, payload = _get_json(
                session, f"{_BASE}/compound/cid/{cid}/property/{property_set}/JSON"
            )
            if status == 200:
                props = _parse_properties(payload)
                break
        if props is None:
            return ChemistryRecord(ingredient=name, status=STATUS_NOT_FOUND)

        unii: str | None = None
        try:
            status, payload = _get_json(session, f"{_BASE}/compound/cid/{cid}/synonyms/JSON")
            if status == 200:
                unii = _parse_unii(payload)
        except PubChemError:
            # Best effort: the UNII is a caption detail, not the identity.
            unii = None

        return ChemistryRecord(
            ingredient=name,
            status=STATUS_RESOLVED,
            pubchem_cid=cid,
            smiles=props["smiles"],
            inchikey=props["inchikey"],
            molecular_formula=props["molecular_formula"],
            molecular_weight=props["molecular_weight"],
            iupac_name=props["iupac_name"],
            unii=unii,
        )
    finally:
        if own:
            session.close()
