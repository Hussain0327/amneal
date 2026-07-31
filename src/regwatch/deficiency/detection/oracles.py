"""Stage 1 -- deterministic oracle battery.

Code checks over structured cells. This is the only layer allowed to assert a fault as
code-verified. It is intentionally conservative: it only fires when it can pair a result
column with an acceptance-criterion column by header, so a wrong pairing does not manufacture
a false high-severity finding. `ORACLES` is a registry -- add checks here to widen coverage.
"""

from __future__ import annotations

import re

from regwatch.common.logging import get_logger
from regwatch.deficiency.schemas.faults import EvidenceClass, Fault, Tier
from regwatch.deficiency.schemas.flaws import FlawCategory, Severity

log = get_logger(__name__)

_SIGNED = r"[-+]?\d+(?:\.\d+)?"
_UNSIGNED = r"\d+(?:\.\d+)?"


def parse_number(text: object) -> float | None:
    """A measured value: '0.17%', '< 0.05 %', '+81' -> float; 'ND'/'N/A' -> None."""
    if text is None:
        return None
    s = str(text)
    if re.search(r"\bnd\b|not detected|n/?a", s, re.I):
        return None
    m = re.search(_SIGNED, s.replace(",", ""))
    return float(m.group()) if m else None


def parse_limit(text: object) -> tuple[str, float | None, float | None] | None:
    """'NMT 0.1%' -> ('max', None, .1); 'NLT 2.5' -> ('min', 2.5, None); '0.9 - 1.5' -> ('range', .9, 1.5)."""
    s = str(text or "").replace("%", " ")
    nums = [float(x) for x in re.findall(_UNSIGNED, s)]
    if len(nums) >= 2 and re.search(r"-|\u2013|to|between", s, re.I):
        return ("range", min(nums), max(nums))
    if nums and re.search(r"nmt|not more than|\u2264|<=|<|max", s, re.I):
        return ("max", None, nums[0])
    if nums and re.search(r"nlt|not less than|\u2265|>=|>|min", s, re.I):
        return ("min", nums[0], None)
    return None


def satisfies(value: float, limit: tuple[str, float | None, float | None]) -> bool:
    kind, low, high = limit
    if kind == "max":
        return value <= high
    if kind == "min":
        return value >= low
    return low <= value <= high


def _find_col(headers: list[str], keys: list[str]) -> int | None:
    for i, h in enumerate(headers):
        if any(k in h for k in keys):
            return i
    return None


def result_vs_limit(doc: dict) -> list[Fault]:
    """Flag any result cell that violates its own acceptance-criterion cell."""
    faults: list[Fault] = []
    for page in doc.get("pages", []):
        for table in page.get("tables", []):
            if table.get("kind") != "grid":
                continue
            headers = [str(h or "").lower() for h in (table.get("headers") or [])]
            limit_col = _find_col(
                headers, ["limit", "acceptance", "specification", "criteria", "spec"]
            )
            result_col = _find_col(headers, ["result", "observed", "found", "obtained"])
            if limit_col is None or result_col is None:
                continue
            for row in table.get("rows") or []:
                if limit_col >= len(row) or result_col >= len(row):
                    continue
                limit = parse_limit(row[limit_col])
                value = parse_number(row[result_col])
                if limit is None or value is None or satisfies(value, limit):
                    continue
                label = (row[0] if row else "").strip()
                faults.append(
                    Fault(
                        title=f"Result out of specification{f' for {label}' if label else ''}",
                        detail=f"{label or 'A row'} reports {row[result_col]!r} against the acceptance criterion {row[limit_col]!r}.",
                        category=FlawCategory.SPEC_MISMATCH,
                        severity=Severity.HIGH,
                        tier=Tier.VERIFIED,
                        evidence_class=EvidenceClass.CODE_VERIFIED,
                        confidence=0.95,
                        evidence=f"{label}: result {row[result_col]} vs limit {row[limit_col]}".strip(
                            ": "
                        ),
                        page=table.get("page", 0),
                        table_ref=(table.get("title") or "")[:80],
                        source="oracle:result_vs_limit",
                    )
                )
    return faults


def _split_value_limit(text: str) -> tuple[float, tuple[str, float | None, float | None]] | None:
    """'0.5\\n(NMT 2.0)' -> (0.5, ('max', None, 2.0)); None if the cell isn't value+inline-limit.

    The measured value is the number BEFORE the parenthesis; the limit is inside it -- so this
    handles the very common CMC cell that carries both (e.g. '1.3 (0.9 -1.5)', '12601 (NLT 7000)').
    """
    match = re.search(r"\(([^)]*)\)", text or "")
    if not match:
        return None
    value = parse_number(text[: match.start()])
    limit = parse_limit(match.group(1))
    if value is None or limit is None:
        return None
    return value, limit


def value_vs_inline_limit(doc: dict) -> list[Fault]:
    """Flag a cell like 'X (NMT Y)' whose value X violates the limit embedded in the same cell."""
    faults: list[Fault] = []
    for page in doc.get("pages", []):
        for table in page.get("tables", []):
            if table.get("kind") != "grid":
                continue
            for row in table.get("rows") or []:
                if not row:
                    continue
                label = str(row[0] or "").strip()
                for cell in row:
                    parsed = _split_value_limit(str(cell or ""))
                    if parsed is None:
                        continue
                    value, limit = parsed
                    if satisfies(value, limit):
                        continue
                    faults.append(
                        Fault(
                            title=f"Result out of specification{f' -- {label}' if label else ''}",
                            detail=f"{label or 'A row'} reports {str(cell).strip()!r}, which violates its own stated limit.",
                            category=FlawCategory.SPEC_MISMATCH,
                            severity=Severity.HIGH,
                            tier=Tier.VERIFIED,
                            evidence_class=EvidenceClass.CODE_VERIFIED,
                            confidence=0.95,
                            evidence=str(cell).replace("\n", " ").strip(),
                            page=table.get("page", 0),
                            table_ref=(table.get("title") or "")[:80],
                            source="oracle:value_vs_inline_limit",
                        )
                    )
    return faults


_AET_RE = re.compile(
    r"\baet\b[^\d]{0,45}?(\d+(?:\.\d+)?)\s*[\u00b5\u03bcu]?\s*g\s*/\s*(g|patch)", re.I
)


def _page_text(page: dict) -> str:
    """All prose + cells on a page as one string, so a named quantity split across adjacent
    blocks (e.g. '...AET' | 'level of 22.727 ug/g') is rejoined. The 45-char window in the
    regex keeps matches local, so joining does not link an unrelated 'AET' to a far number."""
    parts: list[str] = []
    for b in page.get("blocks", []):
        if b.get("role") not in ("page_header", "page_footer"):
            parts.append(b.get("text") or "")
    for t in page.get("tables", []):
        if t.get("kind") == "key_value":
            for pr in t.get("pairs", []):
                parts.append(f"{pr.get('label', '')} {pr.get('value', '')}")
        else:
            for row in t.get("rows") or []:
                parts.append(" ".join(str(c or "") for c in row))
    return " ".join(parts)


def _collapse_near(values) -> list[float]:
    """Collapse values within ~1% (rounding drift) to distinct representatives."""
    keep: list[float] = []
    for v in sorted(set(values)):
        if not any(abs(v - k) <= max(0.001, 0.01 * k) for k in keep):
            keep.append(v)
    return keep


def cross_reference_consistency(doc: dict) -> list[Fault]:
    """Flag a named threshold stated with two different values in the SAME unit.

    v1 checks the AET (analytical evaluation threshold). ug/g and ug/patch are tracked
    separately, because the same AET is legitimately quoted in both units.
    """
    by_unit: dict[str, dict[float, int]] = {}
    for page in doc.get("pages", []):
        pn = page.get("page_number", 0)
        for m in _AET_RE.finditer(_page_text(page)):
            val = round(float(m.group(1)), 3)
            unit = "ug/" + m.group(2).lower()
            by_unit.setdefault(unit, {}).setdefault(val, pn)

    faults: list[Fault] = []
    for unit, val_page in by_unit.items():
        distinct = _collapse_near(val_page.keys())
        if len(distinct) < 2:
            continue
        listing = ", ".join(f"{v} {unit} (p.{val_page[v]})" for v in distinct)
        faults.append(
            Fault(
                title="AET stated with conflicting values across the document",
                detail=(
                    f"The analytical evaluation threshold (AET) appears with different values in "
                    f"{unit}: {listing}. A single threshold should carry one value."
                ),
                category=FlawCategory.SPEC_MISMATCH,
                severity=Severity.HIGH,
                tier=Tier.VERIFIED,
                evidence_class=EvidenceClass.CODE_VERIFIED,
                confidence=0.9,
                evidence=listing,
                source="oracle:cross_reference_aet",
            )
        )
    return faults


ORACLES = [result_vs_limit, value_vs_inline_limit, cross_reference_consistency]


def run_oracles(doc: dict) -> list[Fault]:
    faults: list[Fault] = []
    for check in ORACLES:
        try:
            faults.extend(check(doc))
        except Exception as exc:  # one bad check must not sink the battery
            log.warning("oracle_check_failed", oracle=check.__name__, error=str(exc)[:200])
            continue
    return faults
