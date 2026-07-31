"""Cut the parsed-document JSON into section JSON.

Input and output are both plain JSON (dicts) -- this stage consumes `extract_pdf`'s
document dict and returns a list of section dicts, doing the physical->logical
transformation: cut sections from the block geometry (TOC-anchor -> geometry fallback),
drop the cover + running headers/footers, stitch cross-page grid tables, and attach
tables/figures to their section. No CTD classification -- section identity is the
heading text only.

A block dict is  {role, text, bbox, page, reading_order, confidence, style, lines}.
A table dict is  {kind, title, headers, rows, pairs, bbox, page, n_cols, n_rows,
                  source_pages, continues_from, continues_to, confidence}.
A section dict is {heading, text, blocks, tables, figures, page_start, page_end}.
"""

from __future__ import annotations

import re

_TOC_SECTION_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)\.?\s+(.+)")
# \u00d7 = multiplication sign, \u03bc/\u00b5 = mu/micro: escape form keeps the
# source ASCII (repo rule) with byte-identical matching to upstream.
_DATA_ROW_RE = re.compile(r"^[\d\.\-\+\s\u00d7xX%\u03bc\u00b5]")


# --- heading detection -------------------------------------------------------
def _toc_section_entries(toc: list[dict]) -> list[tuple[str, str, int]]:
    """TOC entries that are numbered sections ("1.4.2 Linearity"), not Table/Figure lists."""
    entries = []
    for entry in toc:
        match = _TOC_SECTION_RE.match(entry.get("title", ""))
        if match:
            entries.append((match.group(1), match.group(2).strip(), entry.get("page", 0)))
    return entries


def _match_heading_block(
    by_page: dict[int, list[tuple[int, dict]]], num: str, page: int
) -> int | None:
    """Find the body-block index whose text begins with this section number, near `page`."""
    pat = re.compile(r"^\s*" + re.escape(num) + r"\.?(?![\d.])")
    for candidate_page in (page, page + 1, page - 1):
        for idx, block in by_page.get(candidate_page, []):
            if pat.match(block.get("text", "")):
                return idx
    return None


def _toc_anchors(doc: dict, body_blocks: list[dict]) -> list[tuple[int, str, str]]:
    entries = _toc_section_entries(doc.get("toc", []))
    if not entries:
        return []

    by_page: dict[int, list[tuple[int, dict]]] = {}
    for idx, block in enumerate(body_blocks):
        by_page.setdefault(block["page"], []).append((idx, block))

    anchors: list[tuple[int, str, str]] = []
    seen_num: set[str] = set()
    seen_idx: set[int] = set()
    for num, title, page in entries:
        if num in seen_num:
            continue
        idx = _match_heading_block(by_page, num, page)
        if idx is not None and idx not in seen_idx:
            anchors.append((idx, num, title))
            seen_num.add(num)
            seen_idx.add(idx)
    anchors.sort(key=lambda a: a[0])
    return anchors


def _plausible_section_number(num: str) -> bool:
    parts = num.split(".")
    if not 1 <= len(parts) <= 4:
        return False
    # 6920.93 / 281.39 are data, not section numbers
    return all(part.isdigit() and int(part) <= 50 for part in parts)


def _geometry_headings(body_blocks: list[dict]) -> list[tuple[int, str, str]]:
    """Fallback when there is no usable TOC: numbering + geometry, rejecting data rows."""
    headings: list[tuple[int, str, str]] = []
    seen: set[str] = set()
    for idx, block in enumerate(body_blocks):
        text = block.get("text", "")
        if len(text) > 120 or len(block.get("lines", [])) > 2:
            continue  # a heading is short and not a wrapped paragraph
        match = _TOC_SECTION_RE.match(text)
        if not match:
            continue
        num, title = match.group(1), match.group(2).strip()
        if not _plausible_section_number(num) or num in seen:
            continue
        if _DATA_ROW_RE.match(title) or not any(c.isalpha() for c in title):
            continue
        first_alpha = next((c for c in title if c.isalpha()), "")
        if first_alpha and not first_alpha.isupper():
            continue
        seen.add(num)
        headings.append((idx, num, title))
    return headings


def _find_headings(doc: dict, body_blocks: list[dict]) -> list[tuple[int, str, str]]:
    anchors = _toc_anchors(doc, body_blocks)
    if len(anchors) >= 2:
        return anchors
    return _geometry_headings(body_blocks)


# --- cross-page table stitching ----------------------------------------------
def _norm_row(cells: list[str]) -> list[str]:
    return [c.strip().lower() for c in cells]


def _can_stitch(
    a: dict, last_page: int, last_bbox, b: dict, page_heights: dict[int, float]
) -> bool:
    """Whether grid `b` continues `a`, checked against a's LAST fragment (page/bbox)."""
    if a["kind"] != "grid" or b["kind"] != "grid" or not last_bbox or not b.get("bbox"):
        return False
    if b["page"] != last_page + 1 or b.get("title"):
        return False
    if a["n_cols"] == 0 or a["n_cols"] != b["n_cols"]:
        return False
    if abs(last_bbox[0] - b["bbox"][0]) > 20 or abs(last_bbox[2] - b["bbox"][2]) > 20:
        return False  # columns must line up
    top_height = page_heights.get(last_page, 0.0)
    if top_height and last_bbox[3] < top_height * 0.80:
        return False  # the last fragment must reach the bottom of its page
    bottom_height = page_heights.get(b["page"], 0.0)
    # b must start at the top of its page
    return not (bottom_height and b["bbox"][1] > bottom_height * 0.30)


def _merge_tables(a: dict, b: dict) -> None:
    if b.get("headers") and a.get("headers") and _norm_row(b["headers"]) == _norm_row(a["headers"]):
        add_rows = b["rows"]  # B repeated the header -> drop the duplicate
    else:
        add_rows = ([b["headers"]] if b.get("headers") else []) + b["rows"]  # B's first row is data
    a["rows"].extend(add_rows)
    a["source_pages"] = list(a.get("source_pages", [])) + list(b.get("source_pages") or [b["page"]])
    a["continues_to"] = True
    a["n_rows"] = len(a["rows"]) + 1


def _stitch_cross_page_tables(tables: list[dict], page_heights: dict[int, float]) -> list[dict]:
    ordered = sorted(tables, key=lambda t: (t["page"], t["bbox"][1] if t.get("bbox") else 0.0))
    result: list[dict] = []
    last_frag: list[tuple[int, object]] = []  # (page, bbox) of the newest fragment merged
    for table in ordered:
        if result and _can_stitch(
            result[-1], last_frag[-1][0], last_frag[-1][1], table, page_heights
        ):
            _merge_tables(result[-1], table)
            last_frag[-1] = (table["page"], table.get("bbox"))
        else:
            result.append(table)
            last_frag.append((table["page"], table.get("bbox")))
    return result


# --- assembly ----------------------------------------------------------------
def _kept_pages(doc: dict) -> set[int]:
    """Drop the cover/approval page 1, unless the document is tiny."""
    if doc["page_count"] <= 2:
        return {p["page_number"] for p in doc["pages"]}
    return {p["page_number"] for p in doc["pages"] if p["page_number"] > 1}


def _position(page: int, bbox) -> tuple[int, float]:
    return (page, bbox[1] if bbox else 0.0)


def _build_section(heading: str, blocks: list[dict]) -> dict:
    pages = [b["page"] for b in blocks]
    return {
        "heading": heading,
        "text": "\n".join(b["text"] for b in blocks if b.get("text")),
        "blocks": list(blocks),
        "tables": [],
        "figures": [],
        "page_start": min(pages) if pages else 0,
        "page_end": max(pages) if pages else 0,
    }


def _assign_items(
    items: list[dict], section_starts: list[tuple[tuple[int, float], dict]], key: str
) -> None:
    """Attach each table/figure to the last section that starts at or before its position."""
    for item in items:
        pos = _position(item["page"], item.get("bbox"))
        target = section_starts[0][1]
        for start_pos, section in section_starts:
            if start_pos <= pos:
                target = section
            else:
                break
        target[key].append(item)


def split_document(doc: dict) -> list[dict]:
    kept = _kept_pages(doc)

    body_blocks = [
        b
        for page in doc["pages"]
        if page["page_number"] in kept
        for b in page["blocks"]
        if b.get("role") not in ("page_header", "page_footer")
    ]
    body_blocks.sort(key=lambda b: (b["page"], b["reading_order"]))

    page_heights = {p["page_number"]: p["height"] for p in doc["pages"]}
    tables = [t for page in doc["pages"] if page["page_number"] in kept for t in page["tables"]]
    figures = [f for page in doc["pages"] if page["page_number"] in kept for f in page["figures"]]
    tables = _stitch_cross_page_tables(tables, page_heights)

    if not body_blocks:
        if not (tables or figures):
            return []
        section = _build_section(doc.get("filename", ""), [])
        section["tables"] = list(tables)
        section["figures"] = list(figures)
        item_pages = [t["page"] for t in tables] + [f["page"] for f in figures]
        section["page_start"] = min(item_pages) if item_pages else 0
        section["page_end"] = max(item_pages) if item_pages else 0
        return [section]

    headings = _find_headings(doc, body_blocks)
    if not headings:
        section = _build_section(doc.get("filename", ""), body_blocks)
        start = [(_position(body_blocks[0]["page"], body_blocks[0].get("bbox")), section)]
        _assign_items(tables, start, "tables")
        _assign_items(figures, start, "figures")
        return [section]

    sections: list[dict] = []
    section_starts: list[tuple[tuple[int, float], dict]] = []

    first_idx = headings[0][0]
    preamble = body_blocks[:first_idx]
    if sum(len(b.get("text", "")) for b in preamble) > 200:
        pre = _build_section("Main Content", preamble)
        sections.append(pre)
        section_starts.append((_position(preamble[0]["page"], preamble[0].get("bbox")), pre))

    for i, (start, num, title) in enumerate(headings):
        end = headings[i + 1][0] if i + 1 < len(headings) else len(body_blocks)
        sec_blocks = body_blocks[start:end]
        section = _build_section(f"{num} {title}", sec_blocks)
        sections.append(section)
        section_starts.append(
            (_position(sec_blocks[0]["page"], sec_blocks[0].get("bbox")), section)
        )

    _assign_items(tables, section_starts, "tables")
    _assign_items(figures, section_starts, "figures")
    return sections


def group_sections(sections: list[dict], max_sections_per_group: int = 5) -> list[dict]:
    groups: list[dict] = []
    for i in range(0, len(sections), max_sections_per_group):
        groups.append(
            {
                "group_id": f"group_{i // max_sections_per_group}",
                "sections": sections[i : i + max_sections_per_group],
            }
        )
    return groups
