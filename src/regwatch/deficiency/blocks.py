"""Compliance Studio editor blocks -> the structured-document dict PARSE emits.

The detection pipeline (split_document -> run_detection) was written against a
PDF: it reads pages, geometry and extracted tables. A Studio document has none
of those -- it is a flat list of editable blocks -- so this adapter fabricates
the minimum geometry the pipeline actually consults, and nothing more.

Four fabrications are load-bearing, each pinned by a test:

  * ONE page, numbered 1. section_splitter._kept_pages discards page 1 as a
    cover sheet on any document of more than two pages, so paginating a Studio
    document would delete its opening section before detection ever ran, with
    no error: split_document simply returns [].
  * DENSE reading_order and strictly increasing block tops. split_document
    sorts on (page, reading_order) and positions tables against block tops;
    constant values would leave document order to the sort's stability and
    collapse every table onto the first section.
  * A page TALLER than its content. Page height is the scale the cross-page
    table stitcher compares fragment edges against; zero is falsy there and
    would disarm both of its ratio guards rather than raise.
  * role "paragraph" for every Studio block type. page_header and page_footer
    are the one role pair the splitter, the oracles, the checklists and the
    prompt renderer all exclude, so mapping a title or a meta line onto either
    would delete the drug-product identity from four consumers at once.

Text is copied verbatim. A fault quotes the document, verify.verify_and_tier
grades that quote against the block corpus, and the Studio anchors it back into
the block it came from (lib/studio-reference.ts locate()); normalising
whitespace here would downgrade real quotes to model judgment and break every
offset the canvas holds.

A document carrying no text at all is refused rather than adapted. An empty
document still runs the whole pipeline -- selection falls back to three keyword
domains and fans specialists out over an empty string -- and reports a clean
result, which is the one outcome a compliance check must never fabricate.

No table of contents is fabricated. The splitter anchors TOC entries by
matching a leading section NUMBER in the block text, so a synthesised entry for
an unnumbered heading could not anchor anyway. A document without numbered
headings therefore comes back as a single section, which is true rather than
convenient.

This module is regwatch-authored and stays fully typed, which is why it sits
beside the other authored seams rather than inside the vendored parse package
(see the mypy overrides in pyproject.toml).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from regwatch.deficiency.schemas.documents import ExtractedTable, LayoutBlock

# US Letter width in points, with the top-left origin a PDF page reports, so
# the fabricated boxes read the same way as measured ones to every consumer.
_PAGE_WIDTH = 612.0
_MARGIN = 72.0
# One ordering slot per block. Nothing here is measured, so this only has to be
# positive: it is what makes tops strictly increasing and in document order.
_LINE = 12.0


def _cells(row: Mapping[str, Any]) -> list[str]:
    return [str(cell) for cell in (row.get("cells") or [])]


def _grid(rows: Sequence[Mapping[str, Any]], top: float) -> ExtractedTable | None:
    """One Studio table as an extracted grid, or None if it carries no cells.

    Only a row the editor marked as a header becomes `headers`. Assuming the
    first row is one would let a data row name the columns, and the columns are
    what the deterministic oracles match on (oracles.result_vs_limit); a table
    whose header is unmarked loses those checks rather than getting wrong ones.

    Args:
      rows: The block's rows, each {cells: list[str], head?: bool}.
      top: The y coordinate this table occupies on the fabricated page.

    Returns:
      The table, or None when every row was empty.
    """
    headers: list[str] = []
    body: list[list[str]] = []
    for row in rows:
        cells = _cells(row)
        if not cells:
            continue
        if row.get("head") and not headers and not body:
            headers = cells
        else:
            body.append(cells)

    if not headers and not body:
        return None

    return ExtractedTable(
        headers=headers,
        rows=body,
        page=1,
        kind="grid",
        bbox=(_MARGIN, top, _PAGE_WIDTH - _MARGIN, top + _LINE),
        n_cols=len(headers) if headers else max((len(row) for row in body), default=0),
        # Counts the header, matching what the stitcher recomputes on merge.
        n_rows=len(body) + (1 if headers else 0),
        source_pages=[1],
    )


def doc_from_blocks(name: str, blocks: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Build the structured-document dict for one Compliance Studio document.

    Args:
      name: The document's display name. It becomes `filename`, which the
        splitter uses as the heading when a document has no numbered ones and
        which the domain selector puts in its prompt.
      blocks: Studio blocks in document order, each {id, type, text, rows?}.
        `text` is required; a block missing it is a wire-format bug and raises
        rather than being skipped, so a malformed payload cannot masquerade as
        an empty document.

    Returns:
      The dict shape parse.pdf.extract_pdf returns, holding exactly one page.
      Blank blocks are dropped; a table block with no cells falls back to its
      text so that content is never silently lost.

    Raises:
      ValueError: The document carries no text and no table cells at all.
      KeyError: A block has no `text` key.
    """
    layout: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    # Blocks and tables share one slot counter so their vertical positions
    # interleave in document order; _assign_items attaches a table to the last
    # section starting at or above it, which is only right if they agree.
    slot = 0

    for block in blocks:
        top = _MARGIN + slot * _LINE

        if block.get("type") == "table":
            grid = _grid(block.get("rows") or [], top)
            if grid is not None:
                tables.append(grid.model_dump())
                slot += 1
                continue

        text: str = block["text"]
        if not text.strip():
            continue

        layout.append(
            LayoutBlock(
                text=text,
                bbox=(_MARGIN, top, _PAGE_WIDTH - _MARGIN, top + _LINE),
                page=1,
                # Dense over surviving blocks. Note this makes block i of the
                # document a different block from studio block i once a blank
                # one is dropped: a fault is mapped back by its quote, never by
                # position.
                reading_order=len(layout),
            ).model_dump()
        )
        slot += 1

    if not layout and not tables:
        raise ValueError("doc_from_blocks: the document carries no text")

    return {
        "filename": name,
        "page_count": 1,
        "toc": [],
        "pages": [
            {
                "page_number": 1,
                "page_label": "",
                "width": _PAGE_WIDTH,
                # Taller than the last slot by a full margin, so no block or
                # table sits below the page it claims to be on.
                "height": 2 * _MARGIN + _LINE * slot,
                "rotation": 0,
                # Names the provenance of a document that was never parsed, so
                # a log line cannot mistake it for something a parser measured.
                "source": "studio",
                "is_scanned": False,
                "blocks": layout,
                "tables": tables,
                "figures": [],
            }
        ],
    }
