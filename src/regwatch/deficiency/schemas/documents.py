from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class CTDSection(StrEnum):
    S_1_GENERAL = "3.2.S.1"
    S_2_MANUFACTURE = "3.2.S.2"
    S_3_CHARACTERIZATION = "3.2.S.3"
    S_4_1_SPECIFICATION = "3.2.S.4.1"
    S_4_2_ANALYTICAL_PROCEDURES = "3.2.S.4.2"
    S_4_3_VALIDATION = "3.2.S.4.3"
    S_4_4_BATCH_ANALYSES = "3.2.S.4.4"
    S_4_5_JUSTIFICATION = "3.2.S.4.5"
    S_5_REFERENCE_STANDARDS = "3.2.S.5"
    S_6_CONTAINER_CLOSURE = "3.2.S.6"
    S_7_STABILITY = "3.2.S.7"
    P_1_DESCRIPTION = "3.2.P.1"
    P_2_DEVELOPMENT = "3.2.P.2"
    P_3_MANUFACTURE = "3.2.P.3"
    P_4_1_SPECIFICATION = "3.2.P.4.1"
    P_4_2_ANALYTICAL_PROCEDURES = "3.2.P.4.2"
    P_4_3_VALIDATION = "3.2.P.4.3"
    P_4_4_BATCH_ANALYSES = "3.2.P.4.4"
    P_4_5_JUSTIFICATION = "3.2.P.4.5"
    P_5_REFERENCE_STANDARDS = "3.2.P.5"
    P_6_CONTAINER_CLOSURE = "3.2.P.6"
    P_7_STABILITY = "3.2.P.7"
    P_8_APPENDICES = "3.2.P.8"
    A_FACILITIES = "3.2.A.1"
    A_ADVENTITIOUS = "3.2.A.2"
    R_REGIONAL = "3.2.R"
    UNKNOWN = "unknown"


Bbox = tuple[float, float, float, float]  # (x0, y0, x1, y1) in PDF points, top-left origin


class TextStyle(BaseModel):
    """Font attributes of a text block. Populated on digital pages; None on OCR pages."""

    font: str = ""
    size: float = 0.0
    bold: bool = False


class LayoutLine(BaseModel):
    """One visual line of text with its bounding box."""

    text: str = ""
    bbox: Bbox = (0.0, 0.0, 0.0, 0.0)
    confidence: float = 1.0


class LayoutBlock(BaseModel):
    """A paragraph-level text block that keeps its constituent lines (reversible grouping).

    role is a descriptive hint from PARSE, not an authoritative heading label -- the
    section splitter decides section boundaries. page_header/page_footer mark repeated
    running headers/footers so the splitter can drop them.
    """

    role: str = "paragraph"  # paragraph | page_header | page_footer | caption | list_item
    text: str = ""
    bbox: Bbox = (0.0, 0.0, 0.0, 0.0)
    page: int = 0
    reading_order: int = 0
    confidence: float = 1.0
    style: TextStyle | None = None
    lines: list[LayoutLine] = Field(default_factory=list)


class LayoutFigure(BaseModel):
    """A figure located on the page. Caption + bbox only -- the image is not interpreted."""

    bbox: Bbox = (0.0, 0.0, 0.0, 0.0)
    page: int = 0
    caption: str = ""
    image_ref: str = ""  # PDF image xref on digital pages; "" on scans
    confidence: float = 1.0


class TablePair(BaseModel):
    """One label/value pair of a key_value table."""

    label: str = ""
    value: str = ""


class ExtractedTable(BaseModel):
    title: str = ""
    headers: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    page: int = 0
    # structured-layout additions
    kind: str = "grid"  # "grid" | "key_value"
    pairs: list[TablePair] = Field(default_factory=list)  # populated when kind == "key_value"
    bbox: Bbox | None = None
    n_cols: int = 0
    n_rows: int = 0
    source_pages: list[int] = Field(default_factory=list)  # >1 entry once stitched across pages
    continues_from: bool = False  # this fragment continues a table from the previous page
    continues_to: bool = False  # this table continues onto the next page
    confidence: float = 1.0


class ParsedSection(BaseModel):
    section_id: CTDSection = CTDSection.UNKNOWN
    heading: str = ""
    text: str = ""
    tables: list[ExtractedTable] = Field(default_factory=list)
    figures: list[LayoutFigure] = Field(default_factory=list)
    blocks: list[LayoutBlock] = Field(default_factory=list)
    page_start: int = 0
    page_end: int = 0


class ChunkGroup(BaseModel):
    """A set of sections grouped for one review pass."""

    group_id: str
    sections: list[ParsedSection]
