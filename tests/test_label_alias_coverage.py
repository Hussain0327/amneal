"""Invariant: the docx label-alias registry covers exactly the cell registry.

``_fill_template`` routes each populated, cited cell value into its template
cell by looking the cell id up in ``_LABEL_ALIASES``. A cell id present in
``CELL_SPECS`` but missing from ``_LABEL_ALIASES`` is NOT an error the writer
raises — its value is silently diverted into the "Additional populated values"
appendix instead of the cell it belongs in (a cited fact landing in the wrong
place on the official form). The reverse drift (a stale alias for a cell that
no longer exists) is dead config that masks this kind of mistake.

The two registries are kept in sync by comment alone, so this guards against
either drifting from the other. It would fail if anyone added a cell to
``CELL_SPECS`` (or removed one) without updating ``_LABEL_ALIASES`` in lockstep.
"""

from __future__ import annotations

from regwatch.whitepaper.docx_writer import _LABEL_ALIASES
from regwatch.whitepaper.template import CELL_SPECS


def test_label_aliases_cover_exactly_the_cell_specs() -> None:
    spec_ids = {spec.id for spec in CELL_SPECS}
    alias_ids = set(_LABEL_ALIASES)

    missing_aliases = spec_ids - alias_ids
    stale_aliases = alias_ids - spec_ids

    assert alias_ids == spec_ids, (
        "_LABEL_ALIASES has drifted from CELL_SPECS. Cells with no alias get "
        "their cited value diverted into the 'Additional populated values' "
        "appendix instead of their template cell; aliases with no cell are dead "
        f"config. Cells missing an alias: {sorted(missing_aliases)}. "
        f"Aliases with no matching cell: {sorted(stale_aliases)}."
    )
