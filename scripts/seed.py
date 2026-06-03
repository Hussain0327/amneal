"""Phase-1 seed: ingest the three verified seed products (spec §16).

  - Albuterol Sulfate (inhalation aerosol, metered)
  - Beclomethasone Dipropionate (inhalation aerosol, metered)
  - Romidepsin (injection)

Run via:
    uv run python scripts/seed.py
or:
    uv run regwatch seed
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make `regwatch` importable when running as a plain script.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from rich import print as rprint

from regwatch.common.logging import configure_logging, get_logger
from regwatch.ingest.pipeline import ingest_listings
from regwatch.ingest.psg_crawler import fetch_index_html, filter_listings, parse_listings

SEED_NAMES = ["albuterol", "beclomethasone", "romidepsin"]


def main() -> int:
    configure_logging()
    log = get_logger("seed")
    log.info("seed_start", names=SEED_NAMES)

    html = fetch_index_html()
    all_listings = parse_listings(html)
    seed_listings = filter_listings(all_listings, normalized_names=SEED_NAMES)

    rprint(
        f"[cyan]found {len(seed_listings)} seed listings out of {len(all_listings)} total[/cyan]"
    )
    for r in seed_listings:
        rprint(
            f"  [yellow]{r.normalized_name}[/yellow] "
            f"({r.dosage_form or '-'}; {r.route or '-'}) "
            f"appl={r.appl_no} type={r.psg_type} date={r.recommended_date}"
        )

    if not seed_listings:
        rprint("[red]no seed listings matched; aborting[/red]")
        return 1

    stats = ingest_listings(seed_listings)
    rprint(
        {
            "scanned": stats.scanned,
            "added": stats.added,
            "revised": stats.revised,
            "unchanged": stats.unchanged,
            "errors": stats.errors,
        }
    )
    return 0 if stats.errors == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
