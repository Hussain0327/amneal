"""Rendering a rebuilt PSG as a Word document.

The assertions that matter are the ones about honesty: the file has to say it
is an extraction, name the FDA source, and admit when it is incomplete. The
rest pins the block-type mapping a reader sees as document structure.
"""

from __future__ import annotations

from io import BytesIO

from docx import Document

from regwatch.process.psg_document import PsgBlock
from regwatch.process.psg_docx import PsgDocxMeta, safe_file_stem, write_psg_docx

_SOURCE = "https://www.accessdata.fda.gov/drugsatfda_docs/psg/PSG_215256.pdf"


def _meta(**overrides: object) -> PsgDocxMeta:
    fields: dict[str, object] = {
        "active_ingredient": "Semaglutide",
        "dosage_form": "Solution",
        "route": "Subcutaneous",
        "appl_no": "215256",
        "psg_type": "draft",
        "recommended_date": "2025-12-04",
        "source_url": _SOURCE,
    }
    fields.update(overrides)
    return PsgDocxMeta(**fields)  # type: ignore[arg-type]


def _blocks() -> list[PsgBlock]:
    return [
        PsgBlock(id="b0", type="title", text="Draft Guidance on Semaglutide", page=1),
        PsgBlock(id="b1", type="meta", text="December 2025", page=1),
        PsgBlock(id="b2", type="h2", text="Recommended Study:", page=1),
        PsgBlock(id="b3", type="p", text="A waiver may be requested.", page=1),
    ]


def _read(data: bytes) -> list[tuple[str, str]]:
    """(style name, text) for every paragraph in the generated file."""
    document = Document(BytesIO(data))
    return [(p.style.name, p.text) for p in document.paragraphs]


def test_blocks_map_onto_word_heading_levels() -> None:
    paragraphs = _read(write_psg_docx(_blocks(), _meta()))
    styles = dict((text, style) for style, text in paragraphs)
    assert styles["Draft Guidance on Semaglutide"] == "Heading 1"
    assert styles["Recommended Study:"] == "Heading 2"
    assert styles["A waiver may be requested."] == "Normal"


def test_header_names_the_document_and_its_standing() -> None:
    texts = [text for _, text in _read(write_psg_docx(_blocks(), _meta()))]
    assert texts[0] == "PSG: Semaglutide"
    assert texts[1] == (
        "Solution  |  Subcutaneous  |  Draft guidance  |  " "Recommended 2025-12-04  |  PSG_215256"
    )


def test_final_guidance_is_not_labelled_draft() -> None:
    texts = [text for _, text in _read(write_psg_docx(_blocks(), _meta(psg_type="final")))]
    assert "Final guidance" in texts[1]
    assert "Draft guidance" not in texts[1]


def test_provenance_names_the_source_and_disclaims_authorship() -> None:
    texts = [text for _, text in _read(write_psg_docx(_blocks(), _meta()))]
    provenance = texts[-1]
    assert _SOURCE in provenance
    assert "not an FDA-issued document" in provenance
    assert "authoritative" in provenance


def test_truncated_extract_says_so_in_the_document() -> None:
    texts = [text for _, text in _read(write_psg_docx(_blocks(), _meta(), truncated=True))]
    assert any("truncated" in t for t in texts)


def test_complete_extract_carries_no_truncation_note() -> None:
    texts = [text for _, text in _read(write_psg_docx(_blocks(), _meta()))]
    assert not any("truncated" in t for t in texts)


def test_a_document_with_no_blocks_still_explains_itself() -> None:
    # Never an empty file: the header and the provenance note survive, so a
    # download of a text-less PSG says what it is rather than opening blank.
    texts = [text for _, text in _read(write_psg_docx([], _meta()))]
    assert texts[0] == "PSG: Semaglutide"
    assert _SOURCE in texts[-1]


def test_missing_form_and_date_drop_out_of_the_descriptor() -> None:
    meta = _meta(dosage_form=None, route=None, recommended_date=None, appl_no=None)
    texts = [text for _, text in _read(write_psg_docx(_blocks(), meta))]
    assert texts[1] == "Draft guidance"


def test_file_stem_drops_characters_a_header_cannot_carry() -> None:
    # A drug name reaches the Content-Disposition header; a quote or a CR in
    # it would let the name break out of the header value.
    assert safe_file_stem("Ethinyl Estradiol; Levonorgestrel") == "Ethinyl_Estradiol_Levonorgestrel"
    assert safe_file_stem('bad"name\r\nX') == "bad_name_X"
    assert safe_file_stem("///") == "psg"
