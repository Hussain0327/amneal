"""regwatch CLI — `uv run regwatch <command>`."""

from __future__ import annotations

import typer
from config.settings import get_settings
from rich import print as rprint

from regwatch.common.logging import configure_logging
from regwatch.store.db import init_db

app = typer.Typer(no_args_is_help=True, add_completion=False, help="REGWATCH command line.")


@app.callback()
def _root() -> None:
    configure_logging()


@app.command("init-db")
def cmd_init_db() -> None:
    """Create SQLite tables and ensure data directories exist."""
    s = get_settings()
    s.ensure_dirs()
    init_db()
    rprint(f"[green]ok[/green] sqlite at {s.sqlite_path}")


@app.command("status")
def cmd_status() -> None:
    """Print provider + path settings (no secrets)."""
    s = get_settings()
    rprint(
        {
            "embedding_provider": s.embedding_provider,
            "llm_provider": s.llm_provider,
            "llm_model": s.llm_model,
            "data_dir": str(s.data_dir),
            "sqlite_path": str(s.sqlite_path),
            "chroma_dir": str(s.chroma_dir),
            "retrieval_top_k": s.retrieval_top_k,
            "refusal_score_threshold": s.refusal_score_threshold,
            "company_name": s.company_name,
        }
    )


@app.command("seed")
def cmd_seed() -> None:
    """Phase-1 seed: ingest the three verified seed products."""
    from regwatch.ingest.pipeline import ingest_listings
    from regwatch.ingest.psg_crawler import fetch_index_html, filter_listings, parse_listings

    init_db()
    seeds = ["albuterol", "beclomethasone", "romidepsin"]
    html = fetch_index_html()
    listings = filter_listings(parse_listings(html), normalized_names=seeds)
    rprint(f"[cyan]matched {len(listings)} listing(s)[/cyan]")
    stats = ingest_listings(listings)
    rprint(
        {
            "scanned": stats.scanned,
            "added": stats.added,
            "revised": stats.revised,
            "unchanged": stats.unchanged,
            "errors": stats.errors,
        }
    )
    raise typer.Exit(code=0 if stats.errors == 0 else 2)


if __name__ == "__main__":
    app()
