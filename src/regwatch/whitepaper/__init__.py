"""CRA White Paper populator (Gate 2).

A populate-on-demand mode: given an RLD name + NDA/ANDA number, fill every cell
of the CRA White Paper template, each carrying provenance (source + locator +
fetched timestamp). The system surfaces, organizes, and cites — it never renders
regulatory judgment (INV-3) and never asserts an unverified fact (INV-5).

Public surface:
  - ``build_whitepaper`` — the wire-contract result builder.
  - ``SpineResolutionError`` — raised (422 on the API) when the spine cannot
    resolve or the RLD name and number disagree.
  - ``WhitepaperBuildTimeoutError`` -- raised (504 on the API) when the fetch
    phase exceeds the overall build deadline; the failure is audited.
  - ``write_whitepaper_docx`` — render the result as a Word document.
  - ``CELL_SPECS`` — the schema encoded as an ordered registry (single source
    of truth, mirroring docs/whitepaper_schema.md).
"""

from __future__ import annotations

from regwatch.whitepaper.docx_writer import write_whitepaper_docx
from regwatch.whitepaper.populator import (
    SpineResolutionError,
    WhitepaperBuildTimeoutError,
    build_whitepaper,
)
from regwatch.whitepaper.template import CELL_SPECS, CellMode, CellSpec

__all__ = [
    "CELL_SPECS",
    "CellMode",
    "CellSpec",
    "SpineResolutionError",
    "WhitepaperBuildTimeoutError",
    "build_whitepaper",
    "write_whitepaper_docx",
]
