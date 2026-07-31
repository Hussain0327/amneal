"""Reconstruction is pure geometry, so it is tested with synthetic boxes -- no Databricks.

These cover paragraph grouping (Option C), the peak-table column reconstruction, and the
key-value vs grid over-merge fix.

Ported from upstream tests/unit/test_layout.py (DefPredict). Import rewritten onto the
vendored module; logic and assertions otherwise unchanged.
"""

from regwatch.deficiency.parse.layout import (
    OCRRegion,
    RawLine,
    group_lines_into_blocks,
    mark_header_footer,
    reconstruct_ocr_page,
)


def _line(text, x0, y, x1, h=12.0):
    return RawLine(text=text, bbox=(x0, y, x1, y + h))


def _cell(text, x_center, y, half_width=18.0, height=12.0, score=0.99):
    return OCRRegion(
        text=text,
        x0=x_center - half_width,
        y0=y,
        x1=x_center + half_width,
        y1=y + height,
        score=score,
    )


# --- paragraph grouping ------------------------------------------------------
def test_gap_starts_new_paragraph():
    lines = [
        _line("The Linearity of an analytical method is its ability", 72, 100, 560),
        _line("well-defined mathematical transformation proportional", 72, 112, 560),
        _line("within a given range.", 72, 124, 560),  # full width, no short-line clue
        _line("The linearity of each component was established", 72, 150, 560),  # after a gap
    ]
    blocks = group_lines_into_blocks(lines, page=11)
    assert len(blocks) == 2
    assert blocks[0].lines and len(blocks[0].lines) == 3  # Option C keeps the lines
    assert blocks[1].text.startswith("The linearity of each component")


def test_short_line_ends_paragraph_without_a_gap():
    lines = [
        _line("No interference observed from the diluent at the", 72, 100, 560),
        _line("use.", 72, 112, 200),  # short -> paragraph ends
        _line("The next paragraph begins right here at full width", 72, 124, 560),  # no gap
    ]
    blocks = group_lines_into_blocks(lines, page=1)
    assert len(blocks) == 2
    assert blocks[0].text.endswith("use.")


def test_mark_header_footer_by_band():
    blocks = group_lines_into_blocks(
        [
            _line("Estradiol, USP (Hemihydrate, Micronized)", 72, 30, 560),  # top band
            _line("Body paragraph in the middle of the page here", 72, 400, 560),
            _line("Confidential   Page 10 of 54", 72, 760, 560),  # bottom band
        ],
        page=11,
    )
    mark_header_footer(blocks, page_height=792.0)
    roles = [b.role for b in blocks]
    assert roles[0] == "page_header"
    assert roles[-1] == "page_footer"
    assert roles[1] == "paragraph"


# --- table reconstruction ----------------------------------------------------
COLS = {"name": 40, "rt": 130, "area": 190, "pct": 250, "int": 310, "res": 380}


def _peak_table_regions():
    r = []
    header_y = 100
    r += [
        _cell("Peak Name", COLS["name"], header_y),
        _cell("RT", COLS["rt"], header_y),
        _cell("Area", COLS["area"], header_y),
        _cell("% Area", COLS["pct"], header_y),
        _cell("Int Type", COLS["int"], header_y),
        _cell("USP Resolution", COLS["res"], header_y, 40),
    ]
    # row 1 -- no resolution cell (the shift bug)
    r += [
        _cell("1", COLS["name"], 116),
        _cell("0.1", COLS["rt"], 116),
        _cell("201", COLS["area"], 116),
        _cell("1.2", COLS["pct"], 116),
        _cell("BB", COLS["int"], 116),
    ]
    # row 2 -- all six columns
    r += [
        _cell("2", COLS["name"], 132),
        _cell("2.1", COLS["rt"], 132),
        _cell("13439", COLS["area"], 132),
        _cell("80.0", COLS["pct"], 132),
        _cell("BB", COLS["int"], 132),
        _cell("3.6", COLS["res"], 132),
    ]
    return r


def test_grid_missing_cell_stays_empty_not_shifted():
    _, tables = reconstruct_ocr_page(_peak_table_regions(), page=38)
    grid = [t for t in tables if t.kind == "grid"]
    assert len(grid) == 1
    t = grid[0]
    assert len(t.headers) == 6 and "USP Resolution" in t.headers
    assert t.rows[0] == ["1", "0.1", "201", "1.2", "BB", ""]  # BB stays put, res blank
    assert t.rows[1] == ["2", "2.1", "13439", "80.0", "BB", "3.6"]


def test_key_value_block_not_merged_into_grid():
    # A SAMPLE INFORMATION key-value block above the peak table, separated by a gap.
    kv = [
        _cell("Sample Name:", 60, 40, 40),
        _cell("Diluent", 170, 40),
        _cell("Vial:", 60, 56, 40),
        _cell("1", 170, 56),
    ]
    regions = kv + _peak_table_regions()  # peak table starts at y=100 (gap after kv)
    _, tables = reconstruct_ocr_page(regions, page=38)
    kinds = sorted(t.kind for t in tables)
    assert kinds == ["grid", "key_value"]  # two tables, not one wide grid
    kv_table = next(t for t in tables if t.kind == "key_value")
    pairs = {p.label: p.value for p in kv_table.pairs}
    assert pairs.get("Sample Name") == "Diluent"
    assert pairs.get("Vial") == "1"


def test_colonless_key_value_detected_without_regressing_grid():
    # CoA-style colon-less pairs (label cell + value cell) above a numeric grid.
    kv = [
        _cell("Lot number", 60, 40, half_width=40),
        _cell("L00036941", 200, 40),
        _cell("Molecular Formula", 60, 60, half_width=55),
        _cell("C18H24O2", 200, 60),
    ]
    grid = _peak_table_regions()  # y >= 100, a real numeric grid
    _, tables = reconstruct_ocr_page(kv + grid, page=34)
    kinds = sorted(t.kind for t in tables)
    assert kinds == ["grid", "key_value"]
    kv_table = next(t for t in tables if t.kind == "key_value")
    pairs = {p.label: p.value for p in kv_table.pairs}
    assert pairs == {"Lot number": "L00036941", "Molecular Formula": "C18H24O2"}


def test_data_row_with_text_cells_stays_grid():
    # "7 Estradiol 12.0 Missing" must NOT become key-value (single-word cells aren't labels).
    rows = [
        [
            _cell("Peak", 40, 100),
            _cell("RT", 120, 100),
            _cell("Area", 190, 100),
            _cell("Note", 260, 100),
        ],
        [
            _cell("7", 40, 116),
            _cell("Estradiol", 120, 116),
            _cell("12.0", 190, 116),
            _cell("Missing", 260, 116),
        ],
        [
            _cell("8", 40, 132),
            _cell("Estrone", 120, 132),
            _cell("16.8", 190, 132),
            _cell("Missing", 260, 132),
        ],
    ]
    regions = [c for row in rows for c in row]
    _, tables = reconstruct_ocr_page(regions, page=38)
    assert [t.kind for t in tables] == ["grid"]


def test_empty_input_is_safe():
    assert reconstruct_ocr_page([], page=1) == ([], [])
