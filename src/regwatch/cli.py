"""regwatch CLI — `uv run regwatch <command>`."""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import typer
import uvicorn
from config.settings import get_settings
from rich import print as rprint
from rich.console import Console
from uvicorn.config import STARTUP_FAILURE

from regwatch.common.logging import configure_logging, get_logger
from regwatch.store.db import init_db

log = get_logger(__name__)

app = typer.Typer(no_args_is_help=True, add_completion=False, help="REGWATCH command line.")


@app.callback()
def _root() -> None:
    configure_logging()


# Phase 2 of the Go proxy rollout (docs/GO_PROXY_ROLLOUT.md). The app must
# accept BOTH address families on one port:
#   * IPv4, because flyd's health checks dial IPv4 and Fly Proxy's backhaul
#     reaches each VM over a private IPv4 address.
#   * IPv6, because the phase-3 Go proxy dials app.process.amneal.internal, a
#     6PN name that resolves AAAA-only.
# One host per family, NOT a single "::" socket: asyncio and uvloop create one
# socket per host and force IPV6_V6ONLY=1 on the AF_INET6 one (CPython
# base_events.py, "Disable IPv4/IPv6 dual stack support"). So the two never
# collide, the result does not depend on the ambient net.ipv6.bindv6only
# sysctl, and each family keeps its NATIVE peer address -- no ::ffff:-mapped
# address ever reaches request.client.host or the access log.
#
# Hardcoded on purpose. Sourcing this from Settings is exactly how phase 2
# would silently become a no-op: the dead API_HOST this commit deletes was
# pinned to "0.0.0.0", which binds IPv4-only, passes every IPv4 gate we have,
# and would only surface at the phase-3 flip.
_DUAL_STACK_HOSTS = ["0.0.0.0", "::"]
_REQUIRED_FAMILIES = frozenset({socket.AF_INET, socket.AF_INET6})


class _DualStackServer(uvicorn.Server):
    """A uvicorn Server that refuses to serve unless BOTH families are bound.

    asyncio silently DROPS a family it cannot bind rather than failing: a
    socket.socket() that raises (EAFNOSUPPORT) hits a bare ``continue``, and a
    bind raising EADDRNOTAVAIL is swallowed as "assume the family is not
    enabled" (CPython base_events.py). Only an all-families failure raises.

    So on a machine without working IPv6 the server comes up happily on IPv4
    alone: flyd's IPv4 check passes, the machine enters rotation looking
    healthy, and every phase-3 proxy 6PN dial gets refused. That is the
    2026-07-15 deploy #106 outage class with no alarm attached. Fail loudly at
    boot instead -- an unbound family is a broken machine, not a degraded one.
    """

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets=sockets)
        bound = {sock.family for server in self.servers for sock in (server.sockets or ())}
        missing = _REQUIRED_FAMILIES - bound
        if missing:
            log.error(
                "dual_stack_bind_incomplete",
                missing=sorted(f.name for f in missing),
                bound=sorted(f.name for f in bound),
                hosts=_DUAL_STACK_HOSTS,
            )
            # CLOSE the partial bind before exiting -- do not just drain the
            # lifespan and rely on process death to release the socket. A
            # half-bound machine still serving IPv4 is the dangerous state (it
            # passes flyd's IPv4 check and enters rotation while refusing every
            # proxy 6PN dial), so it must be unreachable even if some future
            # caller swallows SystemExit. Server.shutdown() closes every
            # listener, drains connections, and runs the lifespan shutdown.
            await self.shutdown()
            # uvicorn's own bind-failure exit code, so the boot fails with a
            # stable, platform-independent status.
            sys.exit(STARTUP_FAILURE)
        # Log the ACTUAL bind, via structlog rather than uvicorn's logger.
        # uvicorn's own "Uvicorn running on ..." line is not dependable here:
        # when init-db runs in-process, alembic's fileConfig() disables every
        # existing logger (its default), silently killing uvicorn's error and
        # access logs for the rest of the boot. structlog writes straight to
        # stdout and survives that. `fly logs` is the only live signal during
        # the phase-2 deploy, so this line is load-bearing, and reporting the
        # families we actually bound beats echoing back the ones we asked for.
        log.info(
            "dual_stack_bind_ok",
            bound=sorted(f.name for f in bound),
            addresses=[
                str(sock.getsockname()[:2])
                for server in self.servers
                for sock in (server.sockets or ())
            ],
        )


def _build_server(hosts: list[str], port: int) -> _DualStackServer:
    """Build the API server. Split out so tests can drive mutant host lists."""
    config = uvicorn.Config(
        "regwatch.api.main:app",
        host=hosts,  # type: ignore[arg-type]  # uvicorn types host as str; asyncio takes an iterable
        port=port,
        # The in-process login-spray limiter (common/ratelimit.py) and the DB
        # pool assume ONE process. workers>1 would also route the bind through
        # uvicorn's bind_socket(), which never sets IPV6_V6ONLY and inherits
        # the ambient sysctl instead.
        workers=1,
    )
    return _DualStackServer(config)


@app.command("serve")
def cmd_serve(port: int = typer.Option(8000, "--port")) -> None:
    """Serve the API on IPv4 AND IPv6 (the production boot command)."""
    server = _build_server(_DUAL_STACK_HOSTS, port)
    # Must run on the MAIN thread: uvicorn's capture_signals() silently no-ops
    # off-thread, which would drop SIGTERM handling and break the graceful
    # drain that fly.toml's kill_timeout=30 bounds. Nothing may follow run():
    # capture_signals re-raises the captured signal, so the process dies BY the
    # signal and any trailing cleanup would be dead code that reviews as live.
    server.run()


@app.command("init-db")
def cmd_init_db() -> None:
    """Bootstrap/verify the Postgres schema and ensure data directories exist."""
    s = get_settings()
    s.ensure_dirs()
    init_db()
    rprint("[green]ok[/green] postgres schema at head")


@app.command("status")
def cmd_status() -> None:
    """Print provider + path settings (no secrets)."""
    s = get_settings()
    rprint(
        {
            "embedding_provider": s.embedding_provider,
            "active_embedding_profile": s.active_embedding_profile,
            "embedding_shadow_profile": s.embedding_shadow_profile,
            "qwen_embedding_model": s.qwen_embedding_model,
            "qwen_embedding_dimension": s.qwen_embedding_dimension,
            "llm_provider": s.llm_provider,
            "llm_model": s.llm_model,
            "databricks_llm_model": s.databricks_llm_model,
            "data_dir": str(s.data_dir),
            "database": "postgres" if s.database_url else "UNSET (refuses to boot)",
            "retrieval_top_k": s.retrieval_top_k,
            "refusal_score_threshold": s.refusal_score_threshold,
            "company_name": s.company_name,
        }
    )


@app.command("embedding-profile-register")
def cmd_embedding_profile_register(
    serving_runtime_version: str = typer.Option(
        ...,
        "--serving-runtime-version",
        help="Immutable serving runtime/deployment version, e.g. vllm-0.10.2.",
    ),
    provider: str = typer.Option("qwen3", "--provider"),
    model: str = typer.Option("", "--model", help="Defaults to QWEN_EMBEDDING_MODEL."),
    revision: str = typer.Option("", "--revision", help="Defaults to QWEN_EMBEDDING_REVISION."),
    dimension: int = typer.Option(
        0,
        "--dimension",
        help="Defaults to QWEN_EMBEDDING_DIMENSION.",
    ),
    dtype: str = typer.Option("float32", "--dtype"),
    normalization: str = typer.Option("l2", "--normalization"),
    preprocessing_version: str = typer.Option("", "--preprocessing-version"),
    chunking_version: str = typer.Option("", "--chunking-version"),
) -> None:
    """Register one immutable Qwen embedding profile and print its ID."""
    from dataclasses import asdict

    from regwatch.process.chunker import CHUNKING_VERSION
    from regwatch.process.embedder import QWEN3_DOCUMENT_PREPROCESSING_VERSION
    from regwatch.store.embedding_profiles import EmbeddingProfileSpec
    from regwatch.store.vector_store import register_embedding_profile

    settings = get_settings()
    spec = EmbeddingProfileSpec(
        provider=provider,
        model=model or settings.qwen_embedding_model,
        revision=revision or settings.qwen_embedding_revision,
        dimension=dimension or settings.qwen_embedding_dimension,
        dtype=dtype,
        normalization=normalization,
        query_instruction_version=settings.qwen_embedding_query_instruction_version,
        preprocessing_version=(preprocessing_version or QWEN3_DOCUMENT_PREPROCESSING_VERSION),
        chunking_version=chunking_version or CHUNKING_VERSION,
        serving_runtime_version=serving_runtime_version,
    )
    init_db()
    profile = register_embedding_profile(spec)
    rprint({"profile": asdict(profile)})


@app.command("embedding-profile-list")
def cmd_embedding_profile_list() -> None:
    """List immutable embedding profiles."""
    from dataclasses import asdict

    from regwatch.store.vector_store import list_embedding_profiles

    init_db()
    profiles = [asdict(profile) for profile in list_embedding_profiles()]
    rprint({"count": len(profiles), "profiles": profiles})


@app.command("embedding-profile-coverage")
def cmd_embedding_profile_coverage(profile_id: str = typer.Argument(...)) -> None:
    """Show durable backfill coverage for one profile."""
    from dataclasses import asdict

    from regwatch.store.vector_store import (
        profile_embedding_coverage,
        profile_hnsw_index_ready,
    )

    init_db()
    coverage = profile_embedding_coverage(profile_id)
    rprint(
        {
            **asdict(coverage),
            "pending_chunks": coverage.pending_chunks,
            "complete": coverage.complete,
            "index_ready": profile_hnsw_index_ready(profile_id),
        }
    )


@app.command("embedding-profile-backfill")
def cmd_embedding_profile_backfill(
    profile_id: str = typer.Argument(...),
    batch_size: int = typer.Option(
        128,
        "--batch-size",
        min=1,
        max=512,
        help="Chunks per durable checkpoint.",
    ),
    limit: int = typer.Option(
        0,
        "--limit",
        min=0,
        help="Maximum chunks this run; 0 means all pending chunks.",
    ),
) -> None:
    """Backfill pending chunks with durable, resumable checkpoints."""
    from dataclasses import asdict

    from regwatch.process.embedder import (
        embed_documents,
        get_embedding_provider_for_profile,
    )
    from regwatch.store.vector_store import (
        get_embedding_profile,
        pending_profile_chunks,
        profile_embedding_coverage,
        upsert_profile_embeddings,
    )

    init_db()
    profile = get_embedding_profile(profile_id)
    provider = get_embedding_provider_for_profile(profile)
    processed = 0
    while limit == 0 or processed < limit:
        page_size = batch_size if limit == 0 else min(batch_size, limit - processed)
        pending = pending_profile_chunks(profile_id, limit=page_size)
        if not pending:
            break
        embeddings = embed_documents(provider, [chunk.text for chunk in pending])
        upsert_profile_embeddings(
            profile_id,
            [chunk.chunk_id for chunk in pending],
            embeddings,
            [chunk.content_hash for chunk in pending],
        )
        processed += len(pending)
        rprint({"profile_id": profile_id, "embedded_this_run": processed})

    coverage = profile_embedding_coverage(profile_id)
    rprint(
        {
            "processed": processed,
            "coverage": asdict(coverage),
            "pending_chunks": coverage.pending_chunks,
            "complete": coverage.complete,
        }
    )


@app.command("embedding-profile-index")
def cmd_embedding_profile_index(
    profile_id: str = typer.Argument(...),
    concurrently: bool = typer.Option(
        True,
        "--concurrently/--no-concurrently",
        help="Use a lock-safe concurrent production index build.",
    ),
) -> None:
    """Build or verify the profile-specific HNSW index."""
    from dataclasses import asdict

    from regwatch.store.vector_store import (
        ensure_profile_hnsw_index,
        profile_embedding_coverage,
    )

    init_db()
    coverage = profile_embedding_coverage(profile_id)
    if not coverage.complete:
        rprint(
            f"[yellow]warning[/yellow] profile is incomplete "
            f"({coverage.embedded_chunks}/{coverage.total_chunks}); "
            "it cannot be activated yet"
        )
    index = ensure_profile_hnsw_index(profile_id, concurrently=concurrently)
    rprint({"index": asdict(index)})


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
    from regwatch.whitepaper.populator import (
        SpineResolutionError,
        WhitepaperBuildTimeoutError,
        build_whitepaper,
    )

    init_db()
    try:
        result = build_whitepaper(rld, appl)
    except SpineResolutionError as exc:
        rprint(f"[red]could not resolve spine[/red] {exc.detail}")
        raise typer.Exit(code=2) from exc
    except WhitepaperBuildTimeoutError as exc:
        # The API-oriented default deadline also applies here; a CLI run with
        # no client waiting can lift it via WHITEPAPER_BUILD_TIMEOUT_S=0.
        rprint(f"[red]build deadline exceeded[/red] {exc.detail}")
        rprint(
            "set WHITEPAPER_BUILD_TIMEOUT_S=0 to disable the deadline for CLI runs "
            "(exit may linger while the abandoned fetch drains its per-call HTTP timeouts)"
        )
        raise typer.Exit(code=3) from exc

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
