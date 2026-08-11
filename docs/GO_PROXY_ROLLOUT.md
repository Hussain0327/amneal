# Go proxy rollout (strangler Step 3): the traffic-flip runbook

> **DONE. Last updated: 2026-08-11.**
> All three phases shipped in July 2026. The Go proxy holds the public edge
> today and `fly.toml` is the live description of the topology. This file is
> kept because code comments and tests cite it by path: `Dockerfile`,
> `fly.toml`, `compose.yaml`, `docker/entrypoint.sh`, `scripts/fly-deploy.sh`,
> `go/internal/proxy/proxy.go`, `go/cmd/proxy/main.go`,
> `tests/test_dual_stack_bind.py`, `tests/test_boot_command_drift.py`,
> `tests/test_entrypoint_guard.py`, `tests/test_fly_deploy_retry.py`.
> What is left below is the part people still hit: the two root causes, the
> alternatives that keep getting re-proposed, and the revert warning.

Where it landed: two Fly process groups in one app. `proxy` holds the public
port 8080 and relays to the `app` group over 6PN. The app group runs
`regwatch serve`, a dual-stack uvicorn on 8000, and is private. The public
`GET /health` check is end to end through the proxy, so a proxy with a dead
upstream falls out of rotation instead of serving 502s while looking healthy.

Phase 1 (PR #94, release v35) un-flipped back to uvicorn-direct. Phase 2
(PR #95, release v36) added the dual-stack listener. Phase 3 flipped the edge
to the proxy. Before all that, PR #93 tried to do it in one deploy and
deadlocked. That incident is why the rest of this file exists.

## Incident: deploy #106 structural deadlock (2026-07-15)

PR #93 flipped the public edge to the Go proxy in one deploy. CI passed. The
image built, `alembic upgrade head` was a clean no-op, and then flyctl created
the first proxy machine and blocked on its end-to-end `GET /health` check.

That check could never pass. The proxy dials
`app.process.amneal.internal:8000`, a 6PN name that resolves AAAA-only to the
still-live pre-flip app machines, and those machines were running the image CMD
bind `0.0.0.0:8000`, which is IPv4 only. Every dial was refused:

    proxy: upstream error: GET /health: dial tcp [fdaa:...:bc4f:2]:8000: connect: connection refused

The `::` rebind that would have fixed the dial was in the same deploy, sequenced
after the gate. flyctl creates and health-gates new-group machines before it
rolls the existing group, so the deploy was waiting on a check whose
precondition only the blocked remainder of that same deploy could create.
flyctl aborted after six minutes. `scripts/fly-deploy.sh` correctly classified
the failure as non-transient and did not retry: no retry of a deadlocked config
can ever pass.

Blast radius was zero. The old app machines kept their public service
registration and `https://amneal.fly.dev/health` stayed 200 throughout. The
residue was a failed release and one stray proxy machine.

Why review missed it: the IPv6 analysis and the deploy-ordering analysis were
each correct on their own, in the same document, and were never composed. The
admission check was evaluated against the post-roll state instead of the state
it would actually run against.

The abort was a mercy. Root cause 2 was waiting behind it: the roll onto
`--host ::` would have produced IPv6-only listeners that fail their own IPv4
health checks, and once the proxy held the public edge with every upstream
unreachable, that would have been a full public outage instead of a failed
deploy.

## Root cause 1: new-group machines are created and health-gated FIRST

flyctl `deployMachinesApp` order, verified in flyctl master and live by #106:
release command, then destroy machines of removed groups, then create machines
for new groups (each blocking on smoke and health checks), then roll existing
machines.

The lesson, on paper so it outlives operators: **any admission check on a NEW
machine group runs against the OLD state of every other group.** If the same
deploy has to change that old state for the check to pass, the deploy deadlocks
by construction. Split it: land the state change in deploy N, the dependent
group in deploy N+1.

## Root cause 2: `--host ::` is IPv6-only under single-process uvicorn

The claim that `::` gives a dual-stack listener cites uvicorn's `bind_socket()`,
which indeed never sets `IPV6_V6ONLY`. That code path is real but it is not the
one that runs: `bind_socket()` executes only under `--reload` or `--workers>1`.
The deployed single-process path is `Server.run` into
`loop.create_server(host="::")`, and both asyncio (CPython `base_events.py`,
"Disable IPv4/IPv6 dual stack support") and uvloop force `IPV6_V6ONLY=True` per
socket, overriding the Linux `bindv6only=0` default.

Measured on the pinned stack (uvicorn 0.51.0, uvloop 0.22.1, CPython 3.12,
2026-07-15): `--host ::` gives `curl -4` REFUSED, `curl -6` 200, listener
`tcp6`. Adding `--workers 2` (the `bind_socket` path) gives both 200 and a
`tcp46` listener.

IPv4 has to keep working on app machines for two reasons:

- flyd's HTTP health checks dial IPv4. Live proof: the pre-flip machines listened
  on an AF_INET-only socket and their checks passed, and a connection accepted by
  an AF_INET socket is necessarily IPv4.
- Fly Proxy's backhaul is private IPv4. Fly's own docs: "Fly Proxy reaches
  services through a private IPv4 address on each VM, so the process should
  listen on 0.0.0.0:<port>".

So `--host ::` drops a machine out of edge rotation and fails deploy gating,
while `--host 0.0.0.0` refuses the proxy's 6PN IPv6 dials. Single-process
uvicorn cannot serve both families through `--host` at all.

## The fix that shipped: `regwatch serve`

`regwatch serve` is a subcommand on the existing typer app
(`src/regwatch/cli.py`). It builds `uvicorn.Config(host=["0.0.0.0", "::"])` and
runs a `_DualStackServer`. asyncio and uvloop create one socket per host and
force `IPV6_V6ONLY=1` on the AF_INET6 one. That buys three things:

- the two sockets cannot collide, and the result does not depend on the ambient
  `net.ipv6.bindv6only` sysctl. It is asserted per socket, not inherited.
- each family keeps its native peer address, so no `::ffff:`-mapped address ever
  reaches `request.client.host`, the login limiter, or the access log.
- it is strictly additive. The AF_INET socket is byte for byte what shipped
  before; IPv6 is added beside it.

The bind list is hardcoded, not read from Settings. The dead `API_HOST` and
`API_PORT` settings were deleted: a launcher that "helpfully" honoured them
would bind IPv4-only, pass every IPv4 gate, ship green, and surface only at the
flip.

`_DualStackServer` also carries a guard. asyncio silently skips a family it
cannot bind, and only an all-families failure raises. Without the guard an
IPv6-less machine would serve IPv4 only, pass flyd's IPv4 check, enter rotation
looking healthy, and refuse every proxy dial: #106 again, with no alarm. The
guard catches the partial bind and exits `STARTUP_FAILURE` (3).

### The tests that hold this

- `tests/test_dual_stack_bind.py` asserts on behaviour (can a client of family X
  get a response?), never on `ss` or `netstat` text. `tcp46` is a BSD-ism and
  Linux `ss` prints `tcp6` for a v6only and a dual-stack listener alike, so a
  string assertion would pass vacuously in CI and re-create exactly the blind
  spot that let root cause 2 merge. It carries a negative control (each mutant
  host list must lose exactly the family it dropped), a real `regwatch serve`
  end-to-end row, and a boot-window fail-fast row. It hard-fails rather than
  skips when `::1` is unavailable, because a skipped gate here is
  indistinguishable from the bug. Mutation-verified 2026-07-16: reverting the
  bind to `["0.0.0.0"]` turns 4 rows red.
- `tests/test_boot_command_drift.py`: `fly.toml` `[processes].app`, the image
  CMD, and compose's `command` must be the same argv. That mirror used to be a
  comment with no test, and this repo has already shipped a boot command that
  quietly outlived the config it claimed to mirror.

## Rejected alternatives, with the evidence, so they are not re-proposed

**Pre-binding sockets and passing `run(sockets=[...])`**, in both the one-socket
(`IPV6_V6ONLY=0`) and two-socket variants. A socket bound by a launcher is bound
before uvicorn imports the app (`config.load()` runs inside `Server._serve`,
measured at 2.4s for this app) and before lifespan startup, and a bound port
stops sending RST. Measured, a mid-boot IPv4 connect goes from `ECONNREFUSED in
0.0004s` to one of two worse states:

- bound but not yet listening: the SYN is dropped, and the dial burns its
  per-address budget (about 2.5s of the proxy's 5s with prod's 2 AAAA).
- bound and listening but the app is not ready: the handshake completes from the
  backlog (measured: connect in 0.0003s, first byte 1.54s later; one variant hung
  47s). This is the dangerous one. A completed handshake IS dial success, so
  address iteration stops and the proxy commits to a dead upstream instead of
  trying the healthy machine.

Both are strictly worse than today's instant RST for zero gain. Pre-importing
the app shrinks the window but cannot close it, since lifespan is inside it by
construction. `regwatch serve` sidesteps the class: asyncio binds and listens
inside `create_server`, after the import and lifespan, so a booting machine
refuses in microseconds.

**`uvicorn --fd <n>`.** `config.fd` is a single int, so it structurally cannot
carry two sockets; `server.py` hardcodes `socket.fromfd(fd, AF_UNIX, ...)`; and
it leaks the listener across drain (measured: new connections connect then hang
during shutdown, where the `sockets=` path refuses in 0.00s).

**`--workers 2`.** `bind_socket()` sets only `SO_REUSEADDR` and never sets
`IPV6_V6ONLY`, so its dual-stack-ness is inherited from a kernel sysctl nobody
in this repo controls. A `bindv6only=1` host would silently produce a v6only
listener with green CI. It also doubles memory (measured app-import RSS 161.9 MB
against a 1 GB machine) and splits limiter state. Note that the old "splits
in-process limiter state" argument is weaker than it claimed, because
`min_machines_running = 2` already splits it. The sysctl argument is the one that
disqualifies it.

**Flycast for the app group.** Killed by explicit docs, not silence. Flycast
requires the app to bind 0.0.0.0, which uvicorn already does, so the premise was
sound. It fails on exposure. Flycast needs a `[[services]]` block, and "If you
have public IP addresses assigned to your app, then services in fly.toml are
exposed to the public internet" (fly.io/docs/networking/flycast/). Public
Anycast and private Flycast share one services table, there is no private-only
scoping key in the documented schema, omitting the ports block is invalid rather
than private, and the only documented remedy is an app-wide `fly ips release`.
App `amneal` holds the public ingress IPs and must keep them, so an app-group
service on flycast:8000 would also publish plaintext uvicorn on port 8000:
force_https bypassed, proxy bypassed, and `TRUST_PROXY_HEADERS=true` applied to
traffic that never crossed Fly's edge, which makes the limiter's Fly-Client-IP
spoofable. Also `flycast:80` would route to whichever group owns
`[http_service]`, which is the proxy, so the proxy dialing it re-enters itself.
Do not re-propose Flycast unless this app is split into two apps, and argue that
on its own merits.

**Proxy and app on one machine.** Changes the approved architecture.

## Operating notes that outlive the rollout

**Fleet size after any deploy.** `fly status` must show 2 proxy + 2 app
machines. Current flyctl creates both proxy machines itself; only if fewer exist
(flyctl version drift) run `fly scale count proxy=2 app=2`, which is idempotent
at 2/2. One proxy machine alone is a single point of failure for the whole
public edge. Check this on every green deploy, not just suspicious ones: a
transient failure between the two proxy-machine creations makes
`scripts/fly-deploy.sh` retry, the retry sees `proxy` as an existing one-machine
group, rolls it without re-applying `--ha` sizing, and reports success with one
proxy machine holding the entire edge. Nothing automated checks fleet size.

**Transient signatures during a rolling deploy, which are not by themselves
rollback triggers.** Isolated proxy `upstream error` lines and single 502s while
an app machine restarts. A refused dial is skipped fast (measured 0.0014s) and
the dialer moves to the next AAAA. The transport also retries idempotent
requests on dead pooled connections. Non-idempotent requests (POSTs) and
in-flight SSE on the restarting machine are not retried and can each fail once.

Precisely, measured against the proxy's own dialer (`net.Dialer{Timeout: 5s}`,
`go/internal/proxy/proxy.go`) with a stub resolver:

- a dial error is NOT retried at the request level. Dial errors are returned
  before the transport's retry loop, and `shouldRetryRequest` is gated on
  `!pc.isReused()`, so only a dead pooled connection is retried.
- the 5s budget is split per address, not shared. `partialDeadline` (Go stdlib
  `net/dial.go`) gives each address `timeRemaining/addrsRemaining` with a 2s
  floor. With the 2 AAAA prod actually has, a stalled machine costs at most
  about 2.5s and the other machine is still dialed. Measured with n=2, first
  address black-holed and second live: 2.5027s, 2 attempts, err=nil. Refused
  dials cost about 0.2ms, so with n=4 all four are dialed in under 1ms.

The rollback trigger is public 503s persisting beyond about 2 minutes after the
first proxy machine shows `started`, or a proxy `/health` check that never goes
green.

**Reverting the flip has a hard public outage window of 1 to 2 minutes.** flyctl
destroys the service-bearing proxy machines first, and only then rolls app
machines back onto a public-service config. Between those two moments, zero
machines advertise the public service. That is the revert working as designed.
Do not interrupt it, do not deploy over it, and do not improvise `fly machine`
commands mid-roll, which is the documented incident amplifier from 2026-06-18
and 2026-07-07. Watch `fly status` and loop
`curl -fsS https://<public-host>/health` until 200. If the window must be
shorter, `fly deploy --strategy immediate` from the reverted checkout replaces
all machines at once and skips health gates.

**Leftover proxy machines are more dangerous now than before phase 2.** With the
app machines permanently dual-stack, a surviving proxy machine's end-to-end
check keeps passing, so it keeps serving a share of public traffic through a
reverted-away path indefinitely. After any aborted or reverted flip deploy,
`fly status` must show zero proxy machines. Destroy survivors and re-verify:

    fly machine list
    fly machine destroy <proxy-machine-id> --force   # per proxy machine

**Deploy retry classification.** `TRANSIENT_ERROR_RE` in
`scripts/fly-deploy.sh` used to match a bare `50[234] (bad gateway|...)` against
the whole captured output. The failing check's body is literally "502 Bad
Gateway / upstream unavailable", so a flyctl upgrade that started echoing check
output would have made a deadlocked flip retry three times instead of failing
fast. Closed 2026-07-16: that 50x branch is now host-anchored, matching only
when a Fly control-plane or builder host (api.machines.dev, api.fly.io,
registry.fly.io) precedes the 50x status on the same line. A check body carries
no such host. Regression-tested by `check-body-502` and `check-echo-502-mixed`
in `tests/test_fly_deploy_retry.py`, and flyctl is pinned to 0.4.71 in
`deploy.yml` so its echo behavior cannot drift silently.

## Standing facts

`docker/entrypoint.sh` init-db stamp-guard matrix, locked by
`tests/test_entrypoint_guard.py`:

| command | init-db |
| ------- | ------- |
| `alembic ...` (release_command) | skipped. It must be able to move the stamp |
| `regwatch-proxy` (also path-qualified) | skipped. The proxy boots DB-independent; a proxy machine crash-looping on the stamp guard while holding the public port is the 2026-06-18 / 2026-07-07 incident class |
| `regwatch serve` (the app group) | runs, then exports `REGWATCH_DB_INITIALIZED=1`. `$1` is `regwatch`, which matches no skip branch, so it takes the default. entrypoint.sh needed no change for phase 2 |
| `uvicorn ...` (the pre-phase-2 app command) | identical: dispatch is on `$1` alone. Kept as a regression row |

Header semantics: the proxy forwards `Fly-Client-IP` and `X-Forwarded-*` byte
for byte (Go tests cover it), so `_client_ip` under `TRUST_PROXY_HEADERS=true`
keeps keying the login limiter on the edge-attested client IP.

If you ever need to prove that by hand, read the recorded IP in `fly logs` or
the audit trail. Do NOT try to prove it by counting to a 429. The limits are 10
attempts per email per minute and 30 per IP per minute
(`src/regwatch/common/ratelimit.py`), `allow()` trips at `>= limit`, and the
per-IP bucket only fires on a 31st attempt using distinct emails. The limiter is
also in-process and split across machines, which makes any count-based probe
nondeterministic. The log read is deterministic and takes one request.

`release_command = "alembic upgrade head"` is the sole migration authority.

## Pre-merge checklist for anything touching this topology

- [ ] CI fully green. `docker-build` is the load-bearing job: the dev host has
      no docker, so CI is the only place the Dockerfile (golang stage plus
      `COPY --from`) is ever built. Never merge on a red or skipped
      docker-build.
- [ ] Trivy note: the API image contains the Go proxy binary, so the API image
      scan can fail on a fixable Go stdlib CVE. The remedy is bumping the pinned
      `golang:` digest in the Dockerfile, not `.trivyignore`.
- [ ] `go-proxy` CI lane green (gofmt, vet, tests) and the full Python gate
      (pytest including `tests/test_entrypoint_guard.py`, mypy, ruff, black).
- [ ] `fly config validate` passes against the new `fly.toml`, run from an
      authed shell. It hits the Fly API, so CI cannot run it.
- [ ] Merging to main auto-deploys via CI into `deploy.yml` once CI is green.
      Treat every merge of these files as a deploy.
