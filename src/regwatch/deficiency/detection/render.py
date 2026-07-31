"""Render structured sections into compact markdown for an LLM to read.

Tables become markdown tables (with their title + page as a caption), key-value blocks
become label/value bullets, prose stays prose. Provenance (page, table title) is kept
inline so a specialist can cite it. This is the "feed structure, not a summary" path.
"""

from __future__ import annotations


def _esc(cell: object) -> str:
    return str(cell if cell is not None else "").replace("\n", " ").replace("|", "\\|").strip()


def _render_table(t: dict) -> str:
    title = (t.get("title") or "").strip()
    page = t.get("page", 0)
    cap = ""
    if title:
        cap = f"**{title}**" + (f" (p.{page})" if page else "")

    if t.get("kind") == "key_value":
        rows = "\n".join(
            f"- {(p.get('label') or '').strip()}: {(p.get('value') or '').strip()}".strip(
                ": "
            ).strip()
            for p in t.get("pairs", [])
        )
        return "\n".join(x for x in (cap, rows) if x)

    headers = t.get("headers") or []
    body = t.get("rows") or []
    parts: list[str] = [cap] if cap else []
    if headers:
        parts.append("| " + " | ".join(_esc(h) for h in headers) + " |")
        parts.append("|" + "|".join(["---"] * len(headers)) + "|")
    for row in body:
        parts.append("| " + " | ".join(_esc(c) for c in row) + " |")
    return "\n".join(parts)


def render_section(section: dict) -> str:
    """One section -> markdown: heading (+ page span), prose blocks, tables."""
    head = section.get("heading", "") or "Section"
    ps, pe = section.get("page_start", 0), section.get("page_end", 0)
    span = f" (p.{ps})" if ps == pe else f" (pp.{ps}-{pe})"
    lines: list[str] = [f"## {head}{span}"]

    for block in section.get("blocks", []):
        if block.get("role") in ("page_header", "page_footer"):
            continue
        text = (block.get("text") or "").strip()
        if text:
            lines.append(text)

    for table in section.get("tables", []):
        rendered = _render_table(table)
        if rendered:
            lines.append(rendered)

    return "\n\n".join(x for x in lines if x)


def render_sections(sections: list[dict], char_budget: int = 45_000) -> str:
    """Concatenate rendered sections up to a character budget (keeps the LLM input bounded)."""
    out: list[str] = []
    used = 0
    for section in sections:
        piece = render_section(section)
        if not piece:
            continue
        if used + len(piece) > char_budget and out:
            out.append("\n[... further sections omitted for length ...]")
            break
        out.append(piece)
        used += len(piece)
    return "\n\n---\n\n".join(out)
