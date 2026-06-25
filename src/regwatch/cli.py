"""regwatch CLI — `uv run regwatch <command>`."""

from __future__ import annotations

from pathlib import Path

import typer
from config.settings import get_settings
from rich import print as rprint
from rich.console import Console

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


def _prompt_password() -> str:
    # Prompted, never a flag/argv: a password argument would leak into shell
    # history and `ps` output. The strength/breach policy is enforced HERE so
    # both provisioning paths (create-user, set-password) get it for free; a
    # weak or breached password is rejected with exit code 2 (the CLI's
    # convention for a bad input), never silently accepted.
    from regwatch.auth.passwords import validate_password_strength

    password = str(typer.prompt("Password", hide_input=True, confirmation_prompt=True))
    reason = validate_password_strength(password)
    if reason is not None:
        rprint(f"[red]error[/red] {reason}")
        raise typer.Exit(code=2)
    return password


def _require_user_row(email: str) -> str:
    """Normalize the email; exit 2 when no such user exists."""
    from sqlmodel import select

    from regwatch.store.db import session_scope
    from regwatch.store.models import User

    normalized = email.strip().lower()
    with session_scope() as s:
        row = s.scalars(select(User).where(User.email == normalized)).first()
        if row is None:
            rprint(f"[red]error[/red] no user with email {normalized!r}")
            raise typer.Exit(code=2)
    return normalized


@app.command("create-user")
def cmd_create_user(
    email: str = typer.Argument(..., help="Login email (stored lowercased)."),
    name: str = typer.Option(..., "--name", help="Display name."),
    role: str = typer.Option("analyst", "--role", help="analyst | admin"),
) -> None:
    """Provision a login. The password is prompted, never passed as an argument."""
    from sqlmodel import select

    from regwatch.auth.passwords import hash_password
    from regwatch.store.db import session_scope
    from regwatch.store.models import User

    if role not in {"analyst", "admin"}:
        rprint("[red]error[/red] role must be 'analyst' or 'admin'")
        raise typer.Exit(code=2)
    password = _prompt_password()
    init_db()
    normalized = email.strip().lower()
    with session_scope() as s:
        if s.scalars(select(User).where(User.email == normalized)).first() is not None:
            rprint(f"[red]error[/red] user {normalized!r} already exists")
            raise typer.Exit(code=2)
        s.add(
            User(
                email=normalized,
                password_hash=hash_password(password),
                display_name=name,
                role=role,
            )
        )
    rprint(f"[green]ok[/green] created {normalized} ({role})")


@app.command("list-users")
def cmd_list_users() -> None:
    """List users (never prints password hashes)."""
    from sqlmodel import select

    from regwatch.store.db import session_scope
    from regwatch.store.models import User

    init_db()
    with session_scope() as s:
        users = [
            {
                "id": u.id,
                "email": u.email,
                "display_name": u.display_name,
                "role": u.role,
                "is_active": u.is_active,
            }
            for u in s.scalars(select(User))
        ]
    rprint({"count": len(users), "users": users})


@app.command("set-password")
def cmd_set_password(email: str = typer.Argument(...)) -> None:
    """Set a user's password (prompted) and revoke their active sessions."""
    from sqlmodel import select

    from regwatch.auth.passwords import hash_password
    from regwatch.store.db import session_scope
    from regwatch.store.models import AuthSession, User

    init_db()
    normalized = _require_user_row(email)
    password = _prompt_password()
    with session_scope() as s:
        row = s.scalars(select(User).where(User.email == normalized)).one()
        row.password_hash = hash_password(password)
        s.add(row)
        for sess in s.scalars(select(AuthSession).where(AuthSession.user_id == row.id)):
            s.delete(sess)
    rprint(f"[green]ok[/green] password updated for {normalized} (sessions revoked)")


@app.command("deactivate-user")
def cmd_deactivate_user(email: str = typer.Argument(...)) -> None:
    """Deactivate a login and revoke their active sessions."""
    from sqlmodel import select

    from regwatch.store.db import session_scope
    from regwatch.store.models import AuthSession, User

    init_db()
    normalized = _require_user_row(email)
    with session_scope() as s:
        row = s.scalars(select(User).where(User.email == normalized)).one()
        row.is_active = False
        s.add(row)
        for sess in s.scalars(select(AuthSession).where(AuthSession.user_id == row.id)):
            s.delete(sess)
    rprint(f"[green]ok[/green] deactivated {normalized}")


@app.command("aliases")
def cmd_aliases(refresh: bool = typer.Option(False, "--refresh")) -> None:
    """Discover applicant-name aliases from Drugs@FDA (no guessing)."""
    from regwatch.watch.aliases import discover_applicant_aliases

    aliases = discover_applicant_aliases(refresh=refresh)
    rprint({"count": len(aliases), "aliases": aliases})


@app.command("seed")
def cmd_seed() -> None:
    """Phase-1 seed: ingest the verified seed PSGs, pinned by application number."""
    from regwatch.ingest.pipeline import ingest_listings
    from regwatch.ingest.psg_crawler import (
        SEED_APPL_NOS,
        fetch_index_html,
        filter_listings,
        parse_listings,
    )

    init_db()
    html = fetch_index_html()
    listings = filter_listings(parse_listings(html), appl_numbers=SEED_APPL_NOS)
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


@app.command("ingest-all")
def cmd_ingest_all(
    limit: int = typer.Option(0, "--limit", help="Max PSGs to ingest (0 = the whole catalog)."),
    final_only: bool = typer.Option(False, "--final-only", help="Skip draft guidances."),
    route: str = typer.Option("", "--route", help="Only this route, e.g. 'Oral' or 'Inhalation'."),
    extract: bool = typer.Option(
        True,
        "--extract/--no-extract",
        help="Run per-PSG LLM BE extraction (paid). --no-extract is free/local and "
        "still makes the Ask path work for every drug.",
    ),
) -> None:
    """Ingest the FULL FDA PSG catalog so the corpus can answer any published drug.

    This is the same pipeline as `seed`, without the 5-product pin. Idempotent:
    re-running skips PSGs whose content is unchanged, so it is safely resumable.
    """
    from regwatch.ingest.pipeline import ingest_listings
    from regwatch.ingest.psg_crawler import fetch_all_listings

    init_db()
    listings = fetch_all_listings()
    if final_only:
        listings = [x for x in listings if x.psg_type == "final"]
    if route:
        listings = [x for x in listings if (x.route or "").lower() == route.lower()]
    if limit > 0:
        listings = listings[:limit]
    rprint(
        f"[cyan]ingesting {len(listings)} PSG listing(s)[/cyan] "
        f"(extract={'on' if extract else 'off'})"
    )
    stats = ingest_listings(listings, extract=extract)
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


@app.command("whitepaper")
def cmd_whitepaper(
    appl: str = typer.Option(..., "--appl", help="NDA/ANDA number, e.g. 020503 or 'NDA 020503'."),
    rld: str = typer.Option(..., "--rld", help="RLD name (proprietary or active ingredient)."),
    json_path: str = typer.Option("", "--json", help="Write the full result JSON to this path."),
    docx_path: str = typer.Option(
        "", "--docx", help="Write the filled Word document to this path."
    ),
) -> None:
    """Populate the CRA White Paper for an RLD + application number (cited cells)."""
    import json as _json

    from rich.table import Table

    from regwatch.whitepaper.docx_writer import write_whitepaper_docx
    from regwatch.whitepaper.populator import SpineResolutionError, build_whitepaper

    init_db()
    try:
        result = build_whitepaper(rld, appl)
    except SpineResolutionError as exc:
        rprint(f"[red]could not resolve spine[/red] {exc.detail}")
        raise typer.Exit(code=2) from exc

    spine = result["spine"]
    rprint(
        f"[cyan]{spine['application_type']} {spine['application_number']}[/cyan] "
        f"{spine['ingredient'] or '(unknown ingredient)'} "
        f"(setid={spine['setid'] or 'n/a'})"
    )
    for warning in result["warnings"]:
        rprint(f"[yellow]warning[/yellow] {warning}")

    table = Table(title="White paper — per-section summary")
    table.add_column("Section")
    table.add_column("cells", justify="right")
    table.add_column("populated", justify="right")
    table.add_column("analyst", justify="right")
    table.add_column("verified-absent", justify="right")
    for section in result["sections"]:
        cells = section["cells"]
        pop = sum(1 for c in cells if c["status"] == "populated")
        ana = sum(1 for c in cells if c["status"] == "analyst_input_required")
        absent = sum(1 for c in cells if c["status"] == "verified_absent")
        table.add_row(section["title"], str(len(cells)), str(pop), str(ana), str(absent))
    Console().print(table)

    if json_path:
        Path(json_path).write_text(_json.dumps(result, indent=2), encoding="utf-8")
        rprint(f"[green]ok[/green] wrote JSON to {json_path}")
    if docx_path:
        data = write_whitepaper_docx(result, template_path=s_template_path())
        Path(docx_path).write_bytes(data)
        rprint(f"[green]ok[/green] wrote DOCX to {docx_path}")


def s_template_path() -> Path:
    return get_settings().whitepaper_template_path


@app.command("watch")
def cmd_watch(
    extract: bool = typer.Option(
        True,
        "--extract/--no-extract",
        help="Run per-PSG LLM BE extraction (paid) for matched PSGs. --no-extract "
        "is free/local and still detects changes and emits alerts.",
    ),
) -> None:
    """Run the Watch pipeline: crawl → match watchlist → ingest matches → alert → digest.

    Alerts are emitted ONLY for matched PSGs this run actually ingested as
    added or revised (INV-4): an unchanged PSG never alerts, so re-running
    twice in a row writes an empty second digest instead of duplicates.
    """
    from regwatch.watch.run import run_watch

    init_db()
    result = run_watch(extract=extract)
    rprint(
        {
            "listings": result.listings,
            "matched": result.matched,
            "added": result.stats.added,
            "revised": result.stats.revised,
            "unchanged": result.stats.unchanged,
            "errors": result.stats.errors,
            "alerts": len(result.alerts),
            "digest": str(result.digest_path),
        }
    )
    raise typer.Exit(code=0 if result.stats.errors == 0 else 2)


if __name__ == "__main__":
    app()
