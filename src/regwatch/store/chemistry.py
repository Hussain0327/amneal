"""Read/write helpers for ``ingredient_chemistry`` (see the model's docstring).

The one read path, ``lookup_structures``, is what ``GET /chemistry/structures``
serves: it never calls PubChem, it only reads what the backfill stored, so a
request can never wait on an external host.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func
from sqlmodel import Session, select

from regwatch.common.text_normalize import canonical_name, split_ingredients, stripped_name
from regwatch.sources.pubchem import STATUS_RESOLVED, ChemistryRecord
from regwatch.store.models import IngredientChemistry, Product, PsgDocument

MATCH_EXACT = "exact"
MATCH_PARENT = "parent"
# A product name is at most a handful of ingredients; anything beyond this is
# not a lookup key, it is garbage in the query string.
MAX_PARTS = 4


@dataclass(frozen=True)
class StructureView:
    """One drawable structure for the wire, with its provenance."""

    name: str
    pubchem_cid: int
    smiles: str
    inchikey: str | None
    molecular_formula: str | None
    molecular_weight: float | None
    iupac_name: str | None
    unii: str | None
    match: str
    source_url: str
    fetched_at: datetime


def ingredient_keys(ingredient: str) -> list[str]:
    """The per-ingredient lookup keys of a (possibly combined) product name."""
    keys: list[str] = []
    for part in split_ingredients(ingredient or ""):
        key = canonical_name(part)
        if key and key not in keys:
            keys.append(key)
    return keys[:MAX_PARTS]


def _resolved_row(session: Session, key: str) -> IngredientChemistry | None:
    statement = select(IngredientChemistry).where(
        IngredientChemistry.ingredient_key == key,
        IngredientChemistry.status == STATUS_RESOLVED,
    )
    return session.exec(statement).first()


def _view(row: IngredientChemistry, *, match: str) -> StructureView | None:
    if row.pubchem_cid is None or not row.smiles:
        return None
    return StructureView(
        name=row.ingredient_key,
        pubchem_cid=row.pubchem_cid,
        smiles=row.smiles,
        inchikey=row.inchikey,
        molecular_formula=row.molecular_formula,
        molecular_weight=row.molecular_weight,
        iupac_name=row.iupac_name,
        unii=row.unii,
        match=match,
        source_url=row.source_url or f"https://pubchem.ncbi.nlm.nih.gov/compound/{row.pubchem_cid}",
        fetched_at=row.fetched_at,
    )


def lookup_structures(session: Session, ingredient: str) -> list[StructureView]:
    """Stored structures for a product name, one per ingredient part.

    The exact salt/form key wins; when only the salt-stripped parent is
    stored, that row is returned flagged ``match="parent"`` so the caption
    can say so. Nothing stored -> nothing returned; the caller hides the
    figure.
    """
    out: list[StructureView] = []
    for key in ingredient_keys(ingredient):
        row = _resolved_row(session, key)
        match = MATCH_EXACT
        if row is None:
            parent = stripped_name(key)
            if parent and parent != key:
                row = _resolved_row(session, parent)
                match = MATCH_PARENT
        if row is None:
            continue
        view = _view(row, match=match)
        if view is not None:
            out.append(view)
    return out


def record(session: Session, rec: ChemistryRecord) -> IngredientChemistry:
    """Upsert one lookup result under its ingredient key."""
    key = canonical_name(rec.ingredient) or rec.ingredient.strip().lower()
    row = session.exec(
        select(IngredientChemistry).where(IngredientChemistry.ingredient_key == key)
    ).first()
    if row is None:
        row = IngredientChemistry(ingredient_key=key, status=rec.status)
    row.status = rec.status
    row.pubchem_cid = rec.pubchem_cid
    row.smiles = rec.smiles
    row.inchikey = rec.inchikey
    row.molecular_formula = rec.molecular_formula
    row.molecular_weight = rec.molecular_weight
    row.iupac_name = rec.iupac_name
    row.unii = rec.unii
    row.source_url = rec.source_url
    row.fetched_at = datetime.now(UTC)
    session.add(row)
    return row


def corpus_ingredient_keys(session: Session) -> list[str]:
    """Every per-ingredient key named by a product or a PSG, deduplicated."""
    names: set[str] = set()
    for column in (Product.normalized_name, PsgDocument.normalized_name):
        for value in session.exec(select(column).distinct()).all():
            if value:
                names.add(str(value))
    keys: set[str] = set()
    for name in names:
        keys.update(ingredient_keys(name))
    return sorted(keys)


def known_keys(session: Session) -> set[str]:
    return set(session.exec(select(IngredientChemistry.ingredient_key)).all())


def count_by_status(session: Session) -> dict[str, int]:
    rows = session.exec(
        select(IngredientChemistry.status, func.count()).group_by(IngredientChemistry.status)
    ).all()
    return {str(status): int(count) for status, count in rows}
