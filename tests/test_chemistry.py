"""Chemistry plate backend: PubChem lookup, stored identity, read endpoint.

The safety rule these pin: a structure is a registry identity resolved from
the ingredient NAME, stored with its CID, and read back verbatim. Ambiguity
and misses store nothing drawable; the endpoint never calls out; a model is
nowhere in the path.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from regwatch.sources import pubchem
from regwatch.store import chemistry
from regwatch.store.db import session_scope
from regwatch.store.models import IngredientChemistry, Product

pytestmark = pytest.mark.invariants

_SMILES = "CC(C)(C)NCC(C1=CC(=C(C=C1)O)CO)O"


def _json(payload: dict[str, Any], status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, content=json.dumps(payload).encode(), headers={"content-type": "application/json"}
    )


def _pubchem(
    cids: list[int] | None,
    *,
    props_status: int = 200,
    fail_first_property_set: bool = False,
) -> tuple[httpx.Client, list[str]]:
    """A fake PubChem: records every URL hit, answers the three routes."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        assert request.url.host == "pubchem.ncbi.nlm.nih.gov"
        path = request.url.path
        if "/compound/name/" in path:
            if cids is None:
                return _json({"Fault": {"Message": "No CID found"}}, 404)
            return _json({"IdentifierList": {"CID": cids}})
        if "/property/" in path:
            if fail_first_property_set and "IsomericSMILES" not in path:
                return _json({"Fault": {"Message": "bad property"}}, 400)
            if props_status != 200:
                return _json({}, props_status)
            key = "IsomericSMILES" if "IsomericSMILES" in path else "SMILES"
            return _json(
                {
                    "PropertyTable": {
                        "Properties": [
                            {
                                "CID": 2083,
                                "MolecularFormula": "C13H21NO3",
                                "MolecularWeight": "239.31",
                                "InChIKey": "NDAUXUAQIAJITI-UHFFFAOYSA-N",
                                "IUPACName": "4-[2-(tert-butylamino)-1-hydroxyethyl]-2-(hydroxymethyl)phenol",
                                key: _SMILES,
                            }
                        ]
                    }
                }
            )
        if "/synonyms/" in path:
            return _json(
                {
                    "InformationList": {
                        "Information": [{"Synonym": ["albuterol", "UNII-QF8SVZ843E"]}]
                    }
                }
            )
        raise AssertionError(f"unexpected pubchem route {path}")

    return httpx.Client(transport=httpx.MockTransport(handler)), seen


@pytest.fixture(autouse=True)
def _no_pacing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("regwatch.sources.pubchem.time.sleep", lambda *_: None)


# ---------- resolver ----------


def test_exact_name_resolves_to_one_cid_with_structure_and_unii() -> None:
    client, seen = _pubchem([2083])
    rec = pubchem.resolve("Albuterol", client=client)
    assert rec.status == pubchem.STATUS_RESOLVED
    assert rec.pubchem_cid == 2083
    assert rec.smiles == _SMILES
    assert rec.molecular_formula == "C13H21NO3"
    assert rec.molecular_weight == pytest.approx(239.31)
    assert rec.unii == "QF8SVZ843E"
    assert rec.source_url == "https://pubchem.ncbi.nlm.nih.gov/compound/2083"
    # The name is URL-quoted and lowercased; nothing else reaches the host.
    assert seen[0].endswith("/compound/name/albuterol/cids/JSON")


def test_ambiguous_name_stores_nothing_drawable() -> None:
    client, seen = _pubchem([2083, 39859])
    rec = pubchem.resolve("albuterol", client=client)
    assert rec.status == pubchem.STATUS_AMBIGUOUS
    assert rec.smiles is None and rec.pubchem_cid is None
    # No property fetch for an ambiguous name: we do not pick one.
    assert len(seen) == 1


def test_unknown_name_is_not_found_not_an_error() -> None:
    client, _ = _pubchem(None)
    assert pubchem.resolve("zzz-not-a-drug", client=client).status == pubchem.STATUS_NOT_FOUND


def test_property_name_fallback_after_a_400() -> None:
    client, seen = _pubchem([2083], fail_first_property_set=True)
    rec = pubchem.resolve("albuterol", client=client)
    assert rec.status == pubchem.STATUS_RESOLVED
    assert any("IsomericSMILES" in u for u in seen)


def test_transient_failures_are_retried_then_raise() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(pubchem.PubChemError):
        pubchem.resolve("albuterol", client=client)
    assert calls == pubchem._ATTEMPTS


def test_timeout_is_a_pubchem_error_not_a_hang() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(pubchem.PubChemError):
        pubchem.resolve("albuterol", client=client)


def test_a_body_that_fails_decoding_is_a_pubchem_error_too() -> None:
    """DecodingError is a sibling of TransportError under RequestError; the
    backfill must see PubChemError, never a raw httpx exception."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.DecodingError("bad gzip", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(pubchem.PubChemError):
        pubchem.resolve("albuterol", client=client)


def test_a_slash_in_a_name_never_becomes_a_path_segment() -> None:
    client, seen = _pubchem(None)
    pubchem.resolve("foo/bar", client=client)
    assert seen[0].endswith("/compound/name/foo%2Fbar/cids/JSON")


def test_redirects_are_never_followed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example/x"})

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    with pytest.raises(pubchem.PubChemError):
        pubchem.resolve("albuterol", client=client)


def test_blank_name_never_reaches_the_network() -> None:
    client, seen = _pubchem([2083])
    assert pubchem.resolve("   ", client=client).status == pubchem.STATUS_NOT_FOUND
    assert seen == []


# ---------- store ----------


def _store(rec: pubchem.ChemistryRecord) -> None:
    with session_scope() as s:
        chemistry.record(s, rec)


def _resolved(name: str, cid: int) -> pubchem.ChemistryRecord:
    return pubchem.ChemistryRecord(
        ingredient=name,
        status=pubchem.STATUS_RESOLVED,
        pubchem_cid=cid,
        smiles=_SMILES,
        inchikey="K",
        molecular_formula="C13H21NO3",
        molecular_weight=239.31,
    )


def test_lookup_prefers_the_exact_salt_and_falls_back_to_the_parent() -> None:
    _store(_resolved("albuterol", 2083))
    with session_scope() as s:
        views = chemistry.lookup_structures(s, "albuterol sulfate")
    assert [(v.name, v.pubchem_cid, v.match) for v in views] == [("albuterol", 2083, "parent")]

    _store(_resolved("albuterol sulfate", 39859))
    with session_scope() as s:
        views = chemistry.lookup_structures(s, "albuterol sulfate")
    assert [(v.name, v.pubchem_cid, v.match) for v in views] == [
        ("albuterol sulfate", 39859, "exact")
    ]


def test_lookup_splits_combination_products_and_skips_unknown_parts() -> None:
    _store(_resolved("ipratropium bromide", 657309))
    with session_scope() as s:
        views = chemistry.lookup_structures(s, "albuterol sulfate; ipratropium bromide")
    assert [v.name for v in views] == ["ipratropium bromide"]


def test_ambiguous_and_not_found_rows_are_never_returned() -> None:
    _store(pubchem.ChemistryRecord(ingredient="insulin", status=pubchem.STATUS_AMBIGUOUS))
    _store(pubchem.ChemistryRecord(ingredient="zzz", status=pubchem.STATUS_NOT_FOUND))
    with session_scope() as s:
        assert chemistry.lookup_structures(s, "insulin") == []
        assert chemistry.lookup_structures(s, "zzz") == []
        assert chemistry.known_keys(s) >= {"insulin", "zzz"}


def test_record_upserts_by_key() -> None:
    _store(pubchem.ChemistryRecord(ingredient="Albuterol", status=pubchem.STATUS_NOT_FOUND))
    _store(_resolved("albuterol", 2083))
    with session_scope() as s:
        rows = s.query(IngredientChemistry).filter_by(ingredient_key="albuterol").all()
        assert len(rows) == 1
        assert rows[0].status == pubchem.STATUS_RESOLVED
        assert rows[0].pubchem_cid == 2083


def test_corpus_keys_come_from_products_and_psgs_split_per_ingredient() -> None:
    with session_scope() as s:
        s.add(
            Product(
                active_ingredient="Albuterol Sulfate; Ipratropium Bromide",
                normalized_name="albuterol sulfate; ipratropium bromide",
                source="manual",
            )
        )
    with session_scope() as s:
        keys = chemistry.corpus_ingredient_keys(s)
    assert {"albuterol sulfate", "ipratropium bromide"} <= set(keys)


# ---------- endpoint ----------


def test_endpoint_returns_stored_structures_and_empty_for_unknown(auth_client: TestClient) -> None:
    _store(_resolved("albuterol sulfate", 39859))
    response = auth_client.get("/chemistry/structures", params={"ingredient": "albuterol sulfate"})
    assert response.status_code == 200
    body = response.json()
    assert body["ingredient"] == "albuterol sulfate"
    assert len(body["structures"]) == 1
    structure = body["structures"][0]
    assert structure["pubchem_cid"] == 39859
    assert structure["smiles"] == _SMILES
    assert structure["match"] == "exact"
    assert structure["source_url"] == "https://pubchem.ncbi.nlm.nih.gov/compound/39859"

    empty = auth_client.get("/chemistry/structures", params={"ingredient": "nothing here"})
    assert empty.status_code == 200
    assert empty.json()["structures"] == []


def test_endpoint_validates_the_key_and_requires_auth(auth_client: TestClient) -> None:
    assert auth_client.get("/chemistry/structures", params={"ingredient": ""}).status_code == 422
    assert (
        auth_client.get("/chemistry/structures", params={"ingredient": "x" * 201}).status_code
        == 422
    )
    from regwatch.api.main import app

    with TestClient(app) as anonymous:
        assert anonymous.get(
            "/chemistry/structures", params={"ingredient": "albuterol"}
        ).status_code in (
            401,
            403,
        )
