"""PSG crawler.

The FDA PSG index is server-side rendered (verified by inspection: a browser
User-Agent gets a 200 with all ~2,400 rows in the initial HTML; the page uses
client-side DataTables only for paging/filtering — there is no backing JSON
endpoint to call instead, so we scrape the rendered table).

Akamai bot protection on accessdata.fda.gov rejects generic User-Agents with
503 Service Unavailable; we use a browser UA. We are polite: low concurrency,
retries with exponential backoff, on-disk PDF caching, idempotent DB upserts.

Table column order (per row inspection):
  0: <a href=PDF>active ingredient</a>
  1: PDF URL (text)
  2: PSG type ("Draft" or "Final")
  3: route ("Oral", "Inhalation", ...)
  4: dosage form ("Tablet", "Aerosol, Metered", ...)
  5: RLD/RS application number(s) — anchors to Orange Book
  6: <td data-sort="YYYY/MM">recommended date MM/DD/YYYY</td>
"""

from __future__ import annotations

import hashlib
import re
import string
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

import httpx
from config.settings import get_settings
from selectolax.parser import HTMLParser, Node
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from regwatch.common.logging import get_logger
from regwatch.common.text_normalize import canonical_name as norm_name
from regwatch.common.text_normalize import stripped_name

log = get_logger(__name__)

PSG_INDEX_URL = "https://www.accessdata.fda.gov/scripts/cder/psg/index.cfm"
# The default index page only server-renders a ~70-row "recent" slice. The full
# catalog (~1,800 PSGs) is reachable only through the A-Z letter routes
# (event=Home.Letter&searchLetter=X). Letters with no drugs (e.g. J, Y) fall back
# to the default slice, so callers must union and de-dupe across letters.
PSG_LETTER_URL = PSG_INDEX_URL + "?event=Home.Letter&searchLetter={letter}"
PDF_URL_TEMPLATE = "https://www.accessdata.fda.gov/drugsatfda_docs/psg/PSG_{appl_no}.pdf"
APPL_FROM_PDF = re.compile(r"PSG_(\d+)\.pdf", re.IGNORECASE)

# Akamai rejects empty / scraper UAs with 503. Use a current-looking browser UA.
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)

_lock = Lock()
_last_fetch_at: float = 0.0


def _polite_pause() -> None:
    """Enforce a minimum interval between requests to fda.gov."""
    s = get_settings()
    interval = s.crawl_min_interval_ms / 1000.0
    global _last_fetch_at
    with _lock:
        now = time.monotonic()
        delta = now - _last_fetch_at
        if delta < interval:
            time.sleep(interval - delta)
        _last_fetch_at = time.monotonic()


@dataclass
class PsgListing:
    """A single PSG row as scraped from the index page."""

    appl_no: str
    active_ingredient: str
    normalized_name: str
    stripped_name: str
    psg_type: str  # "draft" | "final"
    route: str | None
    dosage_form: str | None
    rld_or_rs_numbers: list[str]
    recommended_date: str | None  # ISO YYYY-MM-DD
    pdf_url: str
    source_url: str


def _to_iso_date(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.strip()
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", raw)
    if m:
        mm, dd, yyyy = m.groups()
        return f"{yyyy}-{mm.zfill(2)}-{dd.zfill(2)}"
    return None


def _td_text(td: Node) -> str:
    """Return clean text content of a <td>."""
    return " ".join((td.text() or "").split())


def _td_application_numbers(td: Node) -> list[str]:
    """Extract RLD/RS application numbers from the linked Orange Book anchors."""
    nums: list[str] = []
    for a in td.css("a"):
        title = a.attributes.get("title") or ""
        if title.isdigit():
            nums.append(title)
    if not nums:
        # Sometimes the cell carries plain text only.
        text = _td_text(td)
        for tok in re.findall(r"\b\d{5,6}\b", text):
            if tok not in nums:
                nums.append(tok)
    return nums


def fetch_index_html(*, client: httpx.Client | None = None, url: str = PSG_INDEX_URL) -> str:
    """Fetch a PSG index page HTML (200 expected when UA is browser-like).

    `url` defaults to the landing page; pass a letter route to page the catalog.
    """
    s = get_settings()
    owned = False
    if client is None:
        client = httpx.Client(
            timeout=s.http_timeout_s,
            headers={"User-Agent": BROWSER_UA, "Accept": "text/html"},
            follow_redirects=True,
        )
        owned = True
    try:
        _polite_pause()
        resp = _fetch(client, url)
        resp.raise_for_status()
        return resp.text
    finally:
        if owned:
            client.close()


def fetch_all_listings(*, client: httpx.Client | None = None) -> list[PsgListing]:
    """Enumerate the COMPLETE PSG catalog by walking the A-Z letter routes.

    The landing page only renders a recent slice (~70 rows); iterating
    `event=Home.Letter&searchLetter=A..Z` and unioning by the parser's de-dupe
    key recovers the full database (~1,800 PSGs / ~1,200 distinct drugs).
    """
    s = get_settings()
    owned = False
    if client is None:
        client = httpx.Client(
            timeout=s.http_timeout_s,
            headers={"User-Agent": BROWSER_UA, "Accept": "text/html"},
            follow_redirects=True,
        )
        owned = True
    try:
        merged: dict[tuple[str, str | None, str | None, str], PsgListing] = {}
        for letter in string.ascii_uppercase:
            html = fetch_index_html(client=client, url=PSG_LETTER_URL.format(letter=letter))
            for row in parse_listings(html):
                key = (row.appl_no, row.route, row.dosage_form, row.psg_type)
                merged.setdefault(key, row)
        log.info("psg_catalog_enumerated", listings=len(merged))
        return list(merged.values())
    finally:
        if owned:
            client.close()


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
    retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
    reraise=True,
)
def _fetch(client: httpx.Client, url: str) -> httpx.Response:
    resp = client.get(url)
    if resp.status_code >= 500 or resp.status_code in (429,):
        raise httpx.HTTPStatusError("retryable", request=resp.request, response=resp)
    return resp


def parse_listings(html: str) -> list[PsgListing]:
    """Parse the rendered PSG index HTML into structured rows."""
    tree = HTMLParser(html)
    rows: list[PsgListing] = []
    seen: set[tuple[str, str | None, str | None, str]] = set()

    for tr in tree.css("tr.drugData"):
        tds = tr.css("td")
        if len(tds) < 7:
            continue

        # Column 0: <a href=PDF>name</a>
        a = tds[0].css_first("a[href*='PSG_']")
        if a is None:
            continue
        href = a.attributes.get("href") or ""
        m = APPL_FROM_PDF.search(href)
        if not m:
            continue
        appl_no = m.group(1)
        ingredient_raw = (a.attributes.get("title") or a.text() or "").strip()

        psg_type_raw = _td_text(tds[2]).lower()
        psg_type = "draft" if "draft" in psg_type_raw else "final"

        route = _td_text(tds[3]) or None
        dosage_form = _td_text(tds[4]) or None
        rld_rs = _td_application_numbers(tds[5])

        # The date column has data-sort="YYYY/MM" but the visible text is the canonical
        # MM/DD/YYYY value we want.
        recommended_date = _to_iso_date(_td_text(tds[6]))

        canon = norm_name(ingredient_raw)
        strip = stripped_name(ingredient_raw)

        # De-dupe (some PSGs may appear twice across the table).
        key = (appl_no, route, dosage_form, psg_type)
        if key in seen:
            continue
        seen.add(key)

        rows.append(
            PsgListing(
                appl_no=appl_no,
                active_ingredient=ingredient_raw,
                normalized_name=canon,
                stripped_name=strip,
                psg_type=psg_type,
                route=route,
                dosage_form=dosage_form,
                rld_or_rs_numbers=rld_rs,
                recommended_date=recommended_date,
                pdf_url=(
                    href if href.startswith("http") else PDF_URL_TEMPLATE.format(appl_no=appl_no)
                ),
                source_url=PSG_INDEX_URL,
            )
        )

    log.info("psg_index_parsed", count=len(rows))
    return rows


# The verified seed corpus, pinned by FDA application number so the ingest is
# deterministic and reproducible (spec §16). Selection is by appl_no, NOT by name,
# so FDA naming quirks (e.g. "albuterol" matching "levalbuterol") can never change
# the corpus. Drug names below are for reference only. Romidepsin is intentionally
# absent — it has no PSG and is carried as a watchlist must-refuse case instead.
SEED_APPL_NOS = [
    "020503",  # albuterol sulfate — inhalation aerosol, metered
    "214070",  # albuterol sulfate; budesonide — inhalation aerosol (combo)
    "207921",  # beclomethasone dipropionate — inhalation aerosol, metered
    "020911",  # beclomethasone dipropionate — inhalation aerosol, metered
    "021730",  # levalbuterol tartrate — inhalation aerosol, metered
]


def filter_listings(
    rows: list[PsgListing],
    *,
    normalized_names: list[str] | None = None,
    appl_numbers: list[str] | None = None,
) -> list[PsgListing]:
    """Restrict listings to specific application numbers and/or drug names.

    `appl_numbers` is the precise, deterministic selector (exact application-number
    match) and is preferred for reproducible seeding. `normalized_names` matches a
    seed term as a WHOLE WORD in the canonical or stripped (salt-free) name — so
    "beclomethasone" still matches "beclomethasone dipropionate", but "albuterol"
    no longer pulls "levalbuterol" (which it did under the old substring match).
    """
    if not normalized_names and not appl_numbers:
        return rows
    appl_set = set(appl_numbers or [])
    name_terms = [n.lower().strip() for n in (normalized_names or []) if n.strip()]

    def _name_hit(r: PsgListing) -> bool:
        for term in name_terms:
            pat = rf"\b{re.escape(term)}\b"
            if re.search(pat, r.normalized_name) or re.search(pat, r.stripped_name):
                return True
        return False

    return [r for r in rows if r.appl_no in appl_set or _name_hit(r)]


class PdfTooLargeError(RuntimeError):
    """A fetched PDF body exceeded the configured byte cap (DoS/OOM guard)."""


class PdfInvalidError(RuntimeError):
    """A fetched body was not a PDF (missing the %PDF header)."""


class _RetryableHTTP(Exception):
    """Internal: a 5xx/429 worth retrying (mirrors _fetch's retry trigger)."""


def _looks_like_pdf(data: bytes) -> bool:
    # The PDF spec tolerates some leading bytes before %PDF; check the first ~1KB.
    return b"%PDF-" in data[:1024]


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
    retry=retry_if_exception_type((httpx.TransportError, _RetryableHTTP)),
    reraise=True,
)
def _stream_capped(client: httpx.Client, url: str, max_bytes: int) -> bytes:
    """GET `url`, returning the body but aborting once it exceeds max_bytes.

    Streams so an oversized (or lying-Content-Length) body is cut off before it
    is fully buffered — the OOM guard. Retries 5xx/429 exactly like _fetch; a 4xx
    is surfaced via raise_for_status and is NOT retried (it is not in the retry
    set). max_bytes<=0 disables the cap. The response is always closed (the
    `with` block) on every path, including the cap/again abort.
    """
    with client.stream("GET", url) as resp:
        if resp.status_code >= 500 or resp.status_code == 429:
            raise _RetryableHTTP(f"retryable status {resp.status_code}")
        resp.raise_for_status()
        if max_bytes > 0:
            declared = resp.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise PdfTooLargeError(
                    f"PDF Content-Length {declared} exceeds cap {max_bytes} bytes ({url})"
                )
        chunks: list[bytes] = []
        total = 0
        for chunk in resp.iter_bytes():
            total += len(chunk)
            if max_bytes > 0 and total > max_bytes:
                raise PdfTooLargeError(f"PDF body exceeded cap {max_bytes} bytes ({url})")
            chunks.append(chunk)
        return b"".join(chunks)


def download_pdf(url: str, *, client: httpx.Client | None = None) -> tuple[Path, bytes, str]:
    """Fetch a PSG PDF, cache to disk, return (path, bytes, sha256 hex).

    Validates at the boundary: the body is byte-capped while streaming
    (PdfTooLargeError) and must carry a %PDF header (PdfInvalidError) — so a
    server error page or an oversized blob never reaches the parser. Callers
    (ingest_listing) already degrade these to a logged 'error' for that listing.
    """
    s = get_settings()
    s.ensure_dirs()
    owned = False
    if client is None:
        client = httpx.Client(
            timeout=s.http_timeout_s,
            headers={"User-Agent": BROWSER_UA, "Accept": "application/pdf,*/*"},
            follow_redirects=True,
        )
        owned = True
    try:
        _polite_pause()
        data = _stream_capped(client, url, s.pdf_max_bytes)
        if not _looks_like_pdf(data):
            raise PdfInvalidError(f"fetched body is not a PDF (no %PDF header): {url}")
        digest = hashlib.sha256(data).hexdigest()
        appl_match = APPL_FROM_PDF.search(url)
        appl_no = appl_match.group(1) if appl_match else digest[:12]
        path = s.raw_pdf_dir / f"PSG_{appl_no}_{digest[:8]}.pdf"
        if not path.exists():
            path.write_bytes(data)
        return path, data, digest
    finally:
        if owned:
            client.close()
