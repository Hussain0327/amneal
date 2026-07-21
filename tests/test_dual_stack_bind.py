"""The phase-2 gate: `regwatch serve` must bind IPv4 AND IPv6, or not serve.

Why this file exists. The 2026-07-15 deploy (#106) failed because the app bound
IPv4-only while the Go proxy dialed it over IPv6, and NOTHING in the test suite
ever opened a socket to check which families the boot command actually served.
The bind family was reasoned about, never exercised. That gap is the bug. See
docs/GO_PROXY_ROLLOUT.md root cause 2.

Four properties, each of which has to hold for the phase-3 flip to be safe:

1. BOTH FAMILIES. IPv4 carries flyd's health checks and Fly Proxy's private-IPv4
   backhaul; IPv6 carries the phase-3 proxy's AAAA-only 6PN dials.
2. THE HOST LIST IS LOAD-BEARING (negative control). A both-families assertion
   alone is vacuous -- drop a host and it must go RED. Each mutant is asserted
   to lose exactly the family it dropped.
3. THE GUARD FAILS LOUD. asyncio silently skips a family it cannot bind, so an
   IPv6-less host would serve IPv4 only, pass flyd's IPv4 check, enter rotation
   healthy, and refuse every phase-3 proxy dial: #106 with no alarm.
4. FAIL-FAST WHILE BOOTING. Pre-ready connects must be REFUSED, never accepted
   or black-holed. Phase 3's rollout leans on this: Go's dialer treats a
   completed handshake as success and stops trying the other AAAA records, so a
   machine that accepts-then-stalls makes the proxy commit to a dead upstream
   instead of failing over in microseconds.

Assertions are on BEHAVIOUR (can a client of family X get a response?), never on
`ss`/`netstat`/`lsof` text: `tcp46` is a BSD-ism, and Linux `ss` prints `tcp6`
for a v6only listener and a dual-stack one alike, so a string assertion would
pass vacuously on the CI platform -- re-creating the exact blind spot above.
"""

from __future__ import annotations

import asyncio
import errno
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn

from regwatch.cli import _DUAL_STACK_HOSTS, _DualStackServer

_REPO_ROOT = Path(__file__).resolve().parents[1]


# Trivial ASGI app: the bind family is orthogonal to what is served, and this
# keeps the mechanism tests off the real app's multi-second import + migrations.
async def _tiny_app(scope: dict, receive: object, send: object) -> None:  # type: ignore[type-arg]
    assert scope["type"] == "http"
    await send({"type": "http.response.start", "status": 200, "headers": []})  # type: ignore[operator]
    await send({"type": "http.response.body", "body": b"ok"})  # type: ignore[operator]


def _free_port() -> int:
    """Ask the kernel for a free IPv4 port, then release it.

    TOCTOU by construction: the port is only reserved until this returns, and
    the subprocess rows do not bind it for another ~2s. The race is ACCEPTED,
    not handled -- pytest here is single-process, so the only stealer would be
    an unrelated process grabbing this exact ephemeral port. If it ever loses,
    the failure is loud and self-describing (the server exits STARTUP_FAILURE
    and the test reports "`regwatch serve` exited early" with the bind error),
    not a silent pass. Do not paper over it with a retry that would also mask a
    genuine bind regression.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module", autouse=True)
def _require_ipv6_loopback() -> None:
    """HARD-FAIL, never skip, when ::1 is unusable.

    A skip here is indistinguishable from the bug: this file is the only gate on
    half of phase 2, and a silently-skipped gate is how root cause 2 merged
    green in the first place. An environment without IPv6 loopback is infra to
    fix, not a condition to tolerate.
    """
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
            sock.bind(("::1", 0))
    except OSError as exc:  # pragma: no cover - infra failure, not a code path
        pytest.fail(
            f"environment lacks IPv6 loopback ({exc!r}). This gate must run, not "
            "skip: it is the only check that the app serves the IPv6 family the "
            "phase-3 Go proxy dials."
        )


_REFUSED_ERRNOS = frozenset(
    {errno.ECONNREFUSED, errno.EADDRNOTAVAIL, errno.ENETUNREACH, errno.EAFNOSUPPORT}
)


def _probe(family: int, port: int, *, timeout: float = 5.0) -> str:
    """Speak HTTP/1.0 over `family`; return 'ok' | 'refused' | 'timeout'.

    The refused/timeout distinction is the point, not an implementation detail:
    'refused' is a fast RST (what phase 3's dial failover needs) and 'timeout'
    is a bound-but-dead port (what silently defeats it).
    """
    host = "127.0.0.1" if family == socket.AF_INET else "::1"
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        try:
            sock.connect((host, port))
        except ConnectionRefusedError:
            return "refused"
        except TimeoutError:
            return "timeout"
        except OSError as exc:
            if exc.errno in _REFUSED_ERRNOS:
                return "refused"
            raise
        try:
            sock.sendall(b"GET /health HTTP/1.0\r\n\r\n")
            data = sock.recv(64)
        except TimeoutError:
            return "timeout"
        except ConnectionResetError:
            return "refused"
        return "ok" if data.startswith(b"HTTP/") else "timeout"
    finally:
        sock.close()


async def _aprobe(family: int, port: int, *, timeout: float = 5.0) -> str:
    """_probe from an async test.

    MUST go through a thread: the in-process harness shares this event loop with
    the server, and a blocking connect/recv on the loop thread would stop
    asyncio from ever accepting the connection -- every probe would "time out"
    against a perfectly healthy listener.
    """
    return await asyncio.to_thread(_probe, family, port, timeout=timeout)


class _Harness:
    """Drive uvicorn's real startup()/shutdown() without capture_signals().

    Server.run() installs signal handlers and blocks; inside pytest we want
    neither. _serve() does exactly this sequence before main_loop(), so this
    exercises the production bind path (config.load -> lifespan -> create_server)
    faithfully. Bypassing main_loop is the only difference.
    """

    def __init__(self, server: uvicorn.Server) -> None:
        self.server = server

    async def __aenter__(self) -> uvicorn.Server:
        config = self.server.config
        if not config.loaded:
            config.load()
        self.server.lifespan = config.lifespan_class(config)
        await self.server.startup()
        return self.server

    async def __aexit__(self, *exc: object) -> None:
        if self.server.started:
            await self.server.shutdown()


def _build(
    hosts: list[str], port: int, *, cls: type[uvicorn.Server] = uvicorn.Server
) -> uvicorn.Server:
    config = uvicorn.Config(_tiny_app, host=hosts, port=port, log_config=None)  # type: ignore[arg-type]
    return cls(config)


@pytest.mark.parametrize(
    ("hosts", "expect_v4", "expect_v6"),
    [
        pytest.param(["0.0.0.0", "::"], "ok", "ok", id="both-hosts-serve-both-families"),
        pytest.param(["0.0.0.0"], "ok", "refused", id="mutant-v4-only-loses-ipv6"),
        pytest.param(["::"], "refused", "ok", id="mutant-v6-only-loses-ipv4"),
    ],
)
async def test_host_list_is_what_binds_each_family(
    hosts: list[str], expect_v4: str, expect_v6: str
) -> None:
    """Negative control: every entry in _DUAL_STACK_HOSTS is load-bearing.

    The mutants are what make the first row non-vacuous. Note the v6-only mutant
    proves root cause 2 directly: asyncio forces IPV6_V6ONLY=1 on the AF_INET6
    socket, so "::" alone REFUSES IPv4 -- which is why `--host ::` could never
    have been the fix, and why we bind one socket per family instead.
    """
    port = _free_port()
    async with _Harness(_build(hosts, port)):
        assert await _aprobe(socket.AF_INET, port) == expect_v4
        assert await _aprobe(socket.AF_INET6, port) == expect_v6


async def test_dual_stack_hosts_constant_covers_both_families() -> None:
    """The shipped constant must be the row proven above, not drift off it."""
    assert _DUAL_STACK_HOSTS == ["0.0.0.0", "::"]


async def test_guard_refuses_to_serve_when_a_family_is_missing() -> None:
    """_DualStackServer must exit rather than serve one family.

    Simulates an IPv6-less host by handing the guard a v4-only bind. Plain
    uvicorn.Server serves that happily (proven above) -- the guard is the only
    thing standing between it and a machine that looks healthy to flyd while
    refusing every phase-3 proxy dial.
    """
    port = _free_port()
    server = _build(["0.0.0.0"], port, cls=_DualStackServer)
    with pytest.raises(SystemExit) as excinfo:
        async with _Harness(server):
            pass
    # uvicorn's own STARTUP_FAILURE, so the exit code is stable across platforms.
    assert excinfo.value.code == 3
    assert await _aprobe(socket.AF_INET, port) == "refused", "guard exited but left IPv4 serving"


async def test_guard_serves_when_both_families_bind() -> None:
    """The guard must not fire on the real host list (no false positive)."""
    port = _free_port()
    async with _Harness(_build(_DUAL_STACK_HOSTS, port, cls=_DualStackServer)):
        assert await _aprobe(socket.AF_INET, port) == "ok"
        assert await _aprobe(socket.AF_INET6, port) == "ok"


@pytest.fixture
def _serve_env(tmp_path: Path) -> dict[str, str]:
    """Hermetic env for booting the REAL app: echo providers, the disposable
    TEST_DATABASE_URL Postgres (subprocesses don't inherit monkeypatch)."""
    import os

    env = dict(os.environ)
    env.update(
        {
            "PYTHONUNBUFFERED": "1",
            "EMBEDDING_PROVIDER": "echo",
            "LLM_PROVIDER": "echo",
            "REGWATCH_ALLOW_TEST_PROVIDERS": "1",
            "RATE_LIMIT_PER_MINUTE": "0",
            "OPENAI_API_KEY": "",
            "ANTHROPIC_API_KEY": "",
            "SENTRY_DSN": "",
            "DATABASE_URL": os.environ.get("TEST_DATABASE_URL", ""),
            "PGTZ": "UTC",
            "DATA_DIR": str(tmp_path),
            "RAW_PDF_DIR": str(tmp_path / "raw"),
            "PROCESSED_DIR": str(tmp_path / "processed"),
        }
    )
    return env


@pytest.fixture
def _serve_proc(_serve_env: dict[str, str]) -> Iterator[tuple[subprocess.Popen[bytes], int]]:
    """Boot the REAL `regwatch serve` in a subprocess. Always reaped.

    Invokes the installed console script -- the same entry the image CMD and
    fly.toml [processes].app run -- not a python -m shim, so the packaging is
    under test too.
    """
    port = _free_port()
    regwatch_bin = Path(sys.executable).parent / "regwatch"
    assert regwatch_bin.exists(), f"console script missing at {regwatch_bin}"
    proc = subprocess.Popen(
        [str(regwatch_bin), "serve", "--port", str(port)],
        cwd=_REPO_ROOT,
        env=_serve_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        yield proc, port
    finally:
        # pytest-timeout's thread method does NOT kill children; do it here.
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()
            proc.wait(timeout=10)


@pytest.mark.timeout(120)
def test_real_serve_command_binds_both_families(
    _serve_proc: tuple[subprocess.Popen[bytes], int],
) -> None:
    """End-to-end: the actual shipped command, the actual app, both families.

    Everything above uses a trivial ASGI app. This is the row that proves the
    thing prod runs serves IPv4 and IPv6.
    """
    proc, port = _serve_proc
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if proc.poll() is not None:  # pragma: no cover
            output = proc.stdout.read().decode() if proc.stdout else ""
            pytest.fail(f"`regwatch serve` exited early ({proc.returncode}):\n{output}")
        if _probe(socket.AF_INET, port, timeout=1.0) == "ok":
            break
        time.sleep(0.25)
    else:  # pragma: no cover
        pytest.fail("`regwatch serve` never became ready")

    assert _probe(socket.AF_INET, port) == "ok"
    assert _probe(socket.AF_INET6, port) == "ok"


@pytest.mark.timeout(120)
def test_real_serve_refuses_connections_until_ready(
    _serve_proc: tuple[subprocess.Popen[bytes], int],
) -> None:
    """Pre-ready connects must be REFUSED -- never accepted, never black-holed.

    Asserted over the whole boot rather than at one sampled instant, so it does
    not race: every probe before the first success must be a fast refusal. A
    'timeout' here means the port was bound before the app could answer, which
    is the property that would silently disarm phase 3's dial failover.
    """
    proc, port = _serve_proc
    outcomes: list[str] = []
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if proc.poll() is not None:  # pragma: no cover
            output = proc.stdout.read().decode() if proc.stdout else ""
            pytest.fail(f"`regwatch serve` exited early ({proc.returncode}):\n{output}")
        outcome = _probe(socket.AF_INET, port, timeout=2.0)
        outcomes.append(outcome)
        if outcome == "ok":
            break
        time.sleep(0.05)
    else:  # pragma: no cover
        pytest.fail(f"`regwatch serve` never became ready; outcomes={set(outcomes)}")

    pre_ready = outcomes[:-1]
    assert pre_ready, "app booted before the first probe; the window was never observed"
    assert set(pre_ready) == {"refused"}, (
        "connects during boot must be refused, not accepted-and-stalled. "
        f"saw {sorted(set(pre_ready))}. A bound-but-not-serving port makes Go's "
        "dialer treat a dead machine as a live upstream (docs/GO_PROXY_ROLLOUT.md)."
    )
