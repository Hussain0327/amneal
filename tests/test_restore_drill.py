"""Unit-ish checks for scripts/restore_drill.sh — the production-ref hard guard.

No network and no Postgres anywhere in this file. The contract under test:

* A target URL referencing the production Supabase project ref must make the
  script exit nonzero (the reserved exit code 4) BEFORE any subprocess or
  network call — proven with a stub ``uv`` on PATH that records every
  invocation: on a refused run the stub must never have executed. The match
  happens on the percent-DECODED URL, so an encoded ref cannot slip past.
* A bare non-loopback IP host is refused too (no textual ref for the guard to
  vet); loopback stays allowed for local docker rehearsals.
* A staging URL must pass the guard and reach the underlying migrate script
  with ``--truncate`` and the exact target URL.
* Missing DATABASE_URL / missing snapshot are usage errors (exit 2), distinct
  from the guard's exit 4.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "restore_drill.sh"

PROD_REF = "xvhbfmoynibkcghazzxc"  # must match PROD_PROJECT_REF in the script
PROD_POOLER_URL = (
    f"postgresql://postgres.{PROD_REF}:pw@aws-0-us-east-1.pooler.supabase.com:5432/postgres"
)
PROD_DIRECT_URL = f"postgresql://postgres:pw@db.{PROD_REF}.supabase.co:5432/postgres"
STAGING_URL = (
    "postgresql://postgres.stgrefnotprod123:pw@aws-0-us-east-1.pooler.supabase.com:5432/postgres"
)

GUARD_EXIT_CODE = 4
USAGE_EXIT_CODE = 2


def _make_uv_stub(tmp_path: Path) -> tuple[Path, Path]:
    """A fake ``uv`` that records its argv and exits 0. Returns (bindir, log)."""
    bindir = tmp_path / "stub-bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "uv-invocations.txt"
    stub = bindir / "uv"
    stub.write_text(f'#!/usr/bin/env bash\nprintf \'%s\\n\' "$@" >> "{log}"\nexit 0\n')
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return bindir, log


def _make_snapshot(tmp_path: Path) -> Path:
    snap = tmp_path / "snapshot"
    snap.mkdir(exist_ok=True)
    (snap / "regwatch.db").write_bytes(b"")
    (snap / "chroma").mkdir(exist_ok=True)
    return snap


def _run(
    *,
    url: str | None,
    tmp_path: Path,
    snapshot: Path | None = None,
    skip_embed: bool = False,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Invoke the drill script with a stub ``uv`` first on PATH."""
    bindir, log = _make_uv_stub(tmp_path)
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    if url is not None:
        env["DATABASE_URL"] = url
    if skip_embed:
        env["DRILL_SKIP_EMBED"] = "1"
    args = ["bash", str(SCRIPT)]
    if snapshot is not None:
        args.append(str(snapshot))
    proc = subprocess.run(args, env=env, capture_output=True, text=True, timeout=60)
    return proc, log


# ---------------------------------------------------------------------------
# The hard guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        PROD_POOLER_URL,
        PROD_DIRECT_URL,
        PROD_POOLER_URL.replace(PROD_REF, PROD_REF.upper()),
        # Percent-encoded ref in the pooler username: SQLAlchemy's make_url
        # decodes it back to the production tenant, so the guard must too.
        PROD_POOLER_URL.replace(f"postgres.{PROD_REF}", f"postgres.%78{PROD_REF[1:]}"),
    ],
    ids=["pooler-username", "direct-host", "case-insensitive", "percent-encoded"],
)
def test_guard_refuses_production_ref_before_any_invocation(tmp_path: Path, url: str) -> None:
    snapshot = _make_snapshot(tmp_path)  # valid snapshot: only the guard can refuse
    proc, log = _run(url=url, tmp_path=tmp_path, snapshot=snapshot)
    assert proc.returncode == GUARD_EXIT_CODE, proc.stderr
    assert "REFUSING" in proc.stderr
    assert PROD_REF in proc.stderr
    # Load-bearing: the stub `uv` (first on PATH) never ran — the refusal
    # happened before any subprocess, hence before any network call.
    assert not log.exists(), f"uv was invoked despite the guard: {log.read_text()!r}"
    # The password must never be echoed.
    assert ":pw@" not in proc.stdout + proc.stderr


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://postgres:pw@192.0.2.7:5432/postgres",
        "postgresql://postgres:pw@[2001:db8::7]:5432/postgres",
    ],
    ids=["ipv4", "ipv6-bracketed"],
)
def test_guard_refuses_bare_nonloopback_ip_hosts(tmp_path: Path, url: str) -> None:
    """A raw-IP target (e.g. the production direct host resolved out-of-band)
    carries no project ref the guard could match — refuse it outright."""
    snapshot = _make_snapshot(tmp_path)
    proc, log = _run(url=url, tmp_path=tmp_path, snapshot=snapshot)
    assert proc.returncode == GUARD_EXIT_CODE, proc.stderr
    assert "REFUSING" in proc.stderr
    assert "bare IP" in proc.stderr
    assert not log.exists(), f"uv was invoked despite the guard: {log.read_text()!r}"
    assert ":pw@" not in proc.stdout + proc.stderr


def test_loopback_ip_target_passes_the_guard(tmp_path: Path) -> None:
    """Local docker rehearsals stay possible: loopback cannot be production."""
    url = "postgresql://postgres:pw@127.0.0.1:5499/postgres"
    proc, log = _run(url=url, tmp_path=tmp_path, snapshot=_make_snapshot(tmp_path))
    assert proc.returncode == 0, proc.stderr
    argv = log.read_text().splitlines()
    assert argv[argv.index("--database-url") + 1] == url
    assert "--truncate" in argv


def test_missing_database_url_is_a_usage_error(tmp_path: Path) -> None:
    proc, log = _run(url=None, tmp_path=tmp_path, snapshot=_make_snapshot(tmp_path))
    assert proc.returncode == USAGE_EXIT_CODE
    assert "DATABASE_URL is not set" in proc.stderr
    assert not log.exists()


def test_missing_snapshot_is_a_usage_error_after_the_guard(tmp_path: Path) -> None:
    missing = tmp_path / "no-such-snapshot"
    proc, log = _run(url=STAGING_URL, tmp_path=tmp_path, snapshot=missing)
    assert proc.returncode == USAGE_EXIT_CODE
    assert "regwatch.db not found" in proc.stderr
    assert not log.exists()


# ---------------------------------------------------------------------------
# Pass-through to the migrate script
# ---------------------------------------------------------------------------


def test_staging_url_reaches_migrate_script_with_truncate(tmp_path: Path) -> None:
    snapshot = _make_snapshot(tmp_path)
    proc, log = _run(url=STAGING_URL, tmp_path=tmp_path, snapshot=snapshot)
    assert proc.returncode == 0, proc.stderr
    argv = log.read_text().splitlines()
    assert argv[:2] == ["run", "python"]
    assert argv[2].endswith("scripts/migrate_to_supabase.py")
    assert "--truncate" in argv
    assert "--skip-embed" not in argv
    assert argv[argv.index("--database-url") + 1] == STAGING_URL
    assert argv[argv.index("--sqlite") + 1] == str(snapshot / "regwatch.db")
    assert argv[argv.index("--chroma") + 1] == str(snapshot / "chroma")
    # The wrapper's own output never leaks the password.
    assert ":pw@" not in proc.stdout + proc.stderr


def test_skip_embed_drill_needs_no_chroma_dir(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "regwatch.db").write_bytes(b"")  # deliberately no chroma/
    proc, log = _run(url=STAGING_URL, tmp_path=tmp_path, snapshot=snapshot, skip_embed=True)
    assert proc.returncode == 0, proc.stderr
    argv = log.read_text().splitlines()
    assert "--skip-embed" in argv
    assert "--truncate" in argv
