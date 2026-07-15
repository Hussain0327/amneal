# Go proxy rollout (strangler Step 3): the traffic-flip runbook

Status 2026-07-15: FLIP REVERTED -- this tree is PHASE 1 of a three-phase
re-plan. The one-deploy flip (PR #93 -> deploy run #106) was structurally
undeployable and never took effect: prod stayed on the uvicorn-direct
topology throughout. The proxy binary (`go/cmd/proxy`, merged inert in PR #84)
still ships in the image; the phase-1 config executes it nowhere -- though one
stray v34 proxy machine is still running it out of rotation until this change
deploys (see Residue below). Two root causes follow; the flip re-lands via
phase 2 (dual-stack listener) then phase 3 (the flip).

## Incident record: deploy #106 structural deadlock (2026-07-15)

PR #93 (merged 2026-07-15 15:07 UTC as 3813acd) flipped the public edge to the
Go proxy in one deploy. CI passed and deploy run #106 started 15:14 UTC
(deploy.yml workflow_run -> scripts/fly-deploy.sh, attempt 1/3 at 15:18). The
image built and release_command "alembic upgrade head" was a clean no-op (DB
already at 0014; release machine d894556b905258 destroyed 15:20:45). At
15:20:45 flyctl printed 'Process groups have changed. This will: * create 2
"proxy" machines' and at 15:20:46 created machine 2870755add5228 [proxy]
(v34), then blocked on its end-to-end GET /health check -- exactly the
admission gate this runbook described. The check could never pass: the proxy
dials app.process.amneal.internal:8000, a 6PN name that resolves AAAA-only to
the two still-live pre-flip app machines (2862452b517008 and 6835429c657118,
v33), and those machines still ran the image CMD bind 0.0.0.0:8000
(IPv4-only). Every dial was refused; the proxy logged

    proxy: upstream error: GET /health: dial tcp [fdaa:...:bc4f:2]:8000: connect: connection refused

every 30s and its Fly check sat critical with "502 Bad Gateway / upstream
unavailable". The "::" rebind that was meant to fix the dial was in the SAME
deploy, sequenced AFTER the gate: flyctl creates and health-gates new-group
machines BEFORE updateExistingMachines rolls the app group, so the deploy was
waiting on a check whose precondition only the blocked remainder of that same
deploy could create. At 15:26:18 flyctl aborted ("timeout reached waiting for
health checks to pass for machine 2870755add5228"); fly-deploy.sh correctly
classified the failure non-transient and did not retry -- no retry of a
deadlocked config can ever pass. The second proxy machine was never created
and the app machines were never rolled.

Blast radius: zero public impact -- the v33 app machines kept their
per-machine public service registration and https://amneal.fly.dev/health
stayed 200 throughout. Residue: release v34 marked failed, plus one stray
proxy machine (2870755add5228, check critical, out of edge rotation: public
/healthz returned 404, so no edge traffic ever reached it). Phase 1's deploy
destroys it (verify below).

Why review missed it: the IPv6 analysis and the deploy-ordering analysis were
each individually correct in the previous revision of this runbook. The
topology section proved an IPv4-only 0.0.0.0 listener refuses every 6PN dial;
the deploy-order section verified against flyctl source that new-group
machines are created and health-gated before existing groups roll, and even
noted that app.process.amneal.internal "ALREADY resolves" to the pre-flip
machines. The two facts were never composed: the end-to-end admission check
was evaluated against the post-roll state (apps on "::") instead of the
at-admission state (apps still on 0.0.0.0), and the "a proxy that cannot
reach uvicorn fails the deploy RIGHT HERE, zero prod impact" framing cast a
gate that structurally could not pass as a safety property.

And the failure was a mercy: had the deadlock not aborted the deploy, root
cause 2 below was waiting. The app-group roll onto "--host ::" would have
produced IPv6-ONLY listeners that fail their own IPv4 health checks --
wedging the roll -- and once the proxy held the public edge with every
upstream still (or again) unreachable, the result would have been a total
public outage instead of a failed-but-harmless deploy.

## Root cause 1: new-group machines are created and health-gated FIRST

flyctl deployMachinesApp order (verified in flyctl master, and live by #106):
release command -> destroy machines of REMOVED groups -> create machines for
NEW groups, each blocking on smoke + health checks -> updateExistingMachines
(rolling). A new group's admission check therefore always runs against the
OLD state of every other group.

Generalized lesson, on paper so it outlives operators: any admission check on
a NEW machine group runs against the OLD state of every other group. If the
same deploy must change that old state for the check to pass, the deploy
deadlocks by construction. Split it: land the state change in deploy N, the
dependent group in deploy N+1.

## Root cause 2: "--host ::" is IPv6-ONLY under single-process uvicorn

The previous revision claimed "::" gives a dual-stack listener, citing
uvicorn's bind_socket() (which indeed never sets IPV6_V6ONLY). That code path
is real but NOT the one that runs: bind_socket() executes only under
--reload or --workers>1 (uvicorn main.py). The deployed single-process path
is Server.run -> loop.create_server(host="::"), and BOTH asyncio (CPython
base_events.py: "Disable IPv4/IPv6 dual stack support", sets
IPV6_V6ONLY=True) and uvloop force v6only per-socket -- overriding Linux's
bindv6only=0 default. Verified empirically on the pinned stack (uvicorn
0.51.0 + uvloop 0.22.1 + CPython 3.12, 2026-07-15): "--host ::" -> curl -4
REFUSED, curl -6 200, listener tcp6; "--host :: --workers 2" (the
bind_socket path) -> both 200, listener tcp46.

Why IPv4 must keep working on app machines:

- flyd's HTTP health checks dial IPv4. Live proof: the v33 machines listen
  via an AF_INET-only socket (0.0.0.0) and their checks PASS -- a connection
  accepted by an AF_INET socket is necessarily IPv4.
- Fly Proxy's backhaul is private IPv4: "Fly Proxy reaches services through a
  private IPv4 address on each VM, so the process should listen on
  0.0.0.0:<port>" (fly.io/docs/networking/app-services/).

So a "--host ::" app machine drops out of edge rotation and fails deploy
gating, while a "--host 0.0.0.0" machine refuses the proxy's 6PN IPv6 dials.
Single-process uvicorn cannot satisfy both families via --host at all. The
flip REQUIRES the phase-2 dual-stack listener.

## The re-plan: three phases, each independently deployable

### Phase 1 (this change): pure un-flip

fly.toml returns the public edge to uvicorn -- the proven v28-v33
uvicorn-direct topology, with ONE deliberate runtime delta (kill_timeout,
below):

- `[processes]` keeps ONLY `app = "uvicorn regwatch.api.main:app --host
  0.0.0.0 --port 8000"` (mirrors the image CMD; pins the group name). The
  proxy group line is deleted.
- `[http_service]`: `processes = ["app"]`, `internal_port = 8000`; keeps
  force_https, `auto_stop_machines = false`, `min_machines_running = 2`, and
  the GET /health check (30s/10s/30s).
- The `[checks.app_health]` block is deleted: the restored service check hits
  the same endpoint at the same cadence AND gates edge rotation, which a
  top-level machine check never does -- a strict superset.
- `[env] UPSTREAM_URL` stays, and IS inert: only the Go proxy's
  ConfigFromEnv (go/internal/proxy/proxy.go) reads it; no Python code does.
  Kept so the phase-3 diff is config-minimal.
- `kill_timeout = 30` stays and is NOT inert -- it is phase 1's one runtime
  delta vs v28-v33, and it is strictly safer. uvicorn's graceful shutdown
  waits INDEFINITELY for open connections (`timeout_graceful_shutdown` is
  unset repo-wide, so `asyncio.wait_for(..., timeout=None)`), which makes
  Fly's kill_timeout the ONLY bound on the drain. v28-v33 carried no
  kill_timeout at all = Fly's 5s default; #93 introduced 30 but never rolled
  the app machines, so phase 1 ships it to them for the first time. Effect:
  in-flight requests and SSE streams get 30s to drain on deploy instead of
  being hard-killed at 5s. It also means phase 3 needs no fly.toml change for
  the proxy's 20s drain.
- Dockerfile (proxy build stage), docker/entrypoint.sh (regwatch-proxy
  init-db exemption), and tests/test_entrypoint_guard.py stay: the binary
  ships inert, ready for phase 3.

Expected deploy (watch it live):

1. release_command runs (no-op unless the commit carries a migration).
2. 'Process groups have changed. This will: * destroy 1 "proxy" machine' --
   SINGULAR: only one of #106's two planned proxy machines was ever created.
   Removed-group destruction is forced (no health/state wait) and precedes
   everything else; the stray serves no traffic, so this is publicly
   invisible.
3. Rolling in-place update of the 2 app machines, one at a time, each gated
   by the restored /health service check (~40s per machine historically,
   against a 5-minute per-machine wait budget) while the other keeps serving.
   Zero planned public downtime.

Phase 1 exit criteria (ALL rows, read-only, in order):

| # | command | pass looks like |
| - | ------- | --------------- |
| 1 | `fly status` | exactly 2 `app` machines, ZERO `proxy` machines; both started, 1/1 checks passing, BOTH on the same new VERSION. Use the per-machine VERSION column, NOT the header Image line -- the header tracks the latest release EVEN WHEN IT FAILED (after #106 it showed the failed v34 image while the machines ran v33) |
| 2 | `fly releases` | the phase-1 release is the newest row, status complete |
| 3 | `curl -fsS https://amneal.fly.dev/health` | 200, `"status": "ok"`, db ok |
| 4 | `curl -s -o /dev/null -w '%{http_code}' https://amneal.fly.dev/healthz` | 404 -- uvicorn has no /healthz route, proving the edge terminates at uvicorn. (After phase 3 this same probe must flip to 200 `ok`, becoming the flip proof.) |

Row 1's zero-proxy-machines requirement is mandatory, not hygiene: a stray
proxy machine is only out of rotation while its end-to-end check fails.
Today that is guaranteed by the 0.0.0.0 bind; the moment phase 2 lands, a
surviving stray's check would START PASSING and it would silently serve a
share of public traffic through the un-flipped-away Go path. If flyctl ever
fails to prune it: `fly machine destroy <id> --force`, then re-verify.

### Phase 2 (next PR): a VERIFIED dual-stack listener

Goal: app machines accept BOTH families on :8000 -- IPv4 for flyd checks and
Fly Proxy backhaul, IPv6 for the proxy's 6PN dials -- with a single uvicorn
worker (the login-spray limiter and other in-process state assume one
process).

Recommended mechanism: pre-bind one AF_INET6 socket on `[::]:8000` with
`IPV6_V6ONLY=0` and hand it to uvicorn, which uses an inherited/passed socket
verbatim (no v6only applied): either `uvicorn --fd <n>` behind a tiny
launcher, or programmatic `Server(config).run(sockets=[sock])` via a
`regwatch serve` entrypoint. Rejected alternatives: `--workers 2` (flips onto
the dual-stack bind_socket path but doubles memory on 1GB machines and
splits in-process limiter state); flycast for the app group (re-plumbs
service exposure; revisit only if app machines ever autostop); proxy+app on
one machine (changes the approved architecture).

Phase 2 must ship with:

- a CI test that boots the launcher and asserts BOTH `curl -4 127.0.0.1:PORT`
  and `curl -6 [::1]:PORT` succeed (this exact gap -- never exercising the
  bind family -- let root cause 2 merge);
- fly.toml `[processes].app` updated to the launcher command;
- live verification after its deploy: checks still passing proves the IPv4
  path; for the IPv6 path, from one app machine dial the other over 6PN:
  `fly ssh console -s -C "curl -fsS -6 'http://[<other-machine-6PN-addr>]:8000/health'"`
  (get addresses from `fly machine status`). Record both results here.

Phase 2's deploy is otherwise routine: same topology, same checks, rolling
update. If it wedges, revert it -- the 0.0.0.0 config is always deployable.

### Phase 3 (the flip): staged diff, preconditions, watch matrix

Preconditions (hard gates, in order):

1. Phase 2 deployed AND its live both-family verification recorded above.
2. Per-machine proof that EVERY app machine runs the dual-stack build:
   `fly status` per-machine VERSION column (not the header), and
   `fly machine status <id>` showing the phase-2 launcher in `init.cmd`.
   The proxy's admission dial hits app.process.amneal.internal, which
   resolves every RUNNING app machine with no health filtering -- a single
   leftover v6only/IPv4-only machine re-creates a #106-class wedge.
3. The standing pre-merge checklist below, plus a low-traffic window with
   `fly logs` + `fly status` + this runbook open BEFORE merging.

The staged fly.toml diff (against the phase-2 tree; the `[processes].app`
line will by then be the phase-2 launcher command -- flip only the lines
shown):

    [processes]
       app = "<phase-2 launcher command>"
    +  proxy = "regwatch-proxy"

    [http_service]
    -  processes = ["app"]
    -  internal_port = 8000
    +  processes = ["proxy"]
    +  internal_port = 8080
       (force_https, auto_stop_machines, min_machines_running, and the
       [[http_service.checks]] GET /health block stay byte-identical: the
       check becomes end-to-end through the proxy)

    +# The app group loses its public service with the flip, and with it the
    +# only health check gating its rolling replacement -- an unchecked group
    +# rolls on "machine started" (the 2026-06-18 incident class). This
    +# machine check restores the exact pre-flip cadence, aimed straight at
    +# uvicorn on 8000. flyd dials it over IPv4 -- phase 2 is what makes that
    +# pass. Top-level checks never affect routing (the app group is private
    +# after the flip); this is deploy gating only.
    +[checks]
    +  [checks.app_health]
    +    processes = ["app"]
    +    type = "http"
    +    port = 8000
    +    method = "GET"
    +    path = "/health"
    +    interval = "30s"
    +    timeout = "10s"
    +    grace_period = "30s"

What the flip deploy does (watch it live):

1. release_command runs (unchanged; no-op without a migration).
2. flyctl creates BOTH proxy machines itself ('This will: * create 2 "proxy"
   machines' -- sized by --ha defaulting to true; #106 planned exactly this),
   sequentially, EACH blocking on smoke checks + the end-to-end /health
   admission check. With phase 1+2 landed, that check dials dual-stack app
   machines over 6PN and passes. A proxy that still cannot reach uvicorn
   fails the deploy here, with the still-serving app machines untouched --
   #106 proved that failure mode is publicly invisible.
3. The app group then rolls -- NOT a no-op: each machine's config change
   (public service registration removed, [checks.app_health] added, new
   image ref) is a real stop/update/start, one at a time, gated by
   [checks.app_health].
4. Double-serve window, by design: proxy machines start taking public
   traffic the moment their check passes, while old-config app machines keep
   their own public registration until each is rolled. Both paths serve the
   same API bytes. NO instant exists with zero registered healthy public
   servers.
5. Expected transient signatures (NOT rollback triggers by themselves):
   - isolated proxy `upstream error` lines / single 502s while an app
     machine restarts. GETs (including the admission check) self-heal: the
     Go dialer tries every resolved AAAA within its 5s budget and the
     transport retries idempotent requests on dead pooled connections.
     Non-idempotent requests (POSTs) and in-flight SSE on the restarting
     machine are NOT retried and can each fail once.
   - a brief 503 window ONLY under adverse timing or a flyctl whose ordering
     differs from the verified one.
   Decision rule: public 503s persisting beyond ~2 minutes after the first
   proxy machine shows `started`, or a proxy /health check that never goes
   green, IS the rollback trigger.
6. After the deploy goes green, verify-then-remediate the fleet size:
   `fly status` MUST show 2 proxy + 2 app machines. Current flyctl creates
   both proxy machines itself; ONLY if fewer exist (flyctl version drift),
   run `fly scale count proxy=2 app=2` (idempotent at 2/2). One proxy
   machine alone is a single point of failure for the entire public edge --
   do not leave the deploy in that state.

Watched-flip verification matrix (run every row, in order):

| # | check | how | pass looks like |
| - | ----- | --- | --------------- |
| 1 | both groups healthy | `fly status` | 2x `app` + 2x `proxy`, all started, all checks passing |
| 2 | proxy holds the edge | `curl -fsS https://<public-host>/healthz` | `ok` -- uvicorn has no /healthz route, so this proves the flip (it returned 404 through phases 1-2) |
| 3 | end-to-end health | `curl -fsS https://<public-host>/health` | 200, `"status": "ok"`, db ok -- full edge -> proxy -> 6PN -> uvicorn -> DB path |
| 4 | auth + cookie flow | login via the frontend (or `curl -c` the login endpoint), then hit an authed route with the cookie | authed 200; cookie round-trips through the proxy |
| 5 | streaming | authed `curl -N -X POST https://<public-host>/query/stream ...` | `event: token` frames arrive INCREMENTALLY, not one buffered burst (FlushInterval -1 doing its job) |
| 6 | Fly-Client-IP reaches the app | one login from a known client IP, then read the recorded client IP in `fly logs` / the audit trail | the RECORDED IP equals your real client IP -- NOT a `fdaa:*` proxy-machine address |
| 7 | no proxy errors | `fly logs` | no sustained `upstream error:` lines during the smoke window |

Row 6 is the limiter-semantics check: `_client_ip` under
`TRUST_PROXY_HEADERS=true` must keep seeing the edge-attested client IP
through the new proxy hop. If the proxy mangled Fly-Client-IP or appended to
X-Forwarded-For, every caller would collapse into ONE bucket (the proxy
machine's own address) and the per-IP spray guard would be gutted.

Read the recorded IP -- do NOT try to prove this by counting to a 429. The
real limits are 10 attempts/email/minute and 30/IP/minute
(src/regwatch/common/ratelimit.py), and `allow()` trips at `>= limit`, so
tripping the per-IP bucket (the one that actually proves IP keying) takes a
31st attempt using DISTINCT emails -- otherwise the per-email cap fires at 11
and proves nothing about IP keying. Worse, the limiter is in-process and
`min_machines_running = 2` splits the window across machines (~2x effective,
so budget up to ~60 attempts), which makes any count-based probe
nondeterministic -- and post-flip the fan-out is across 2 proxy machines to 2
app machines. The log read is deterministic and takes one request.

## Rollback (phase 3)

- Mid-deploy abort at proxy admission: nothing to roll back. Exactly like
  #106, the app machines were never touched and keep serving; blast radius
  zero. scripts/fly-deploy.sh fail-fasts (health-check failures are
  deliberately non-transient) -- verified in #106. NOTE this holds only
  because flyctl does not echo the failing check's BODY into deploy output:
  that body is literally "502 Bad Gateway / upstream unavailable", and
  TRANSIENT_ERROR_RE (scripts/fly-deploy.sh) matches
  `50[234] (bad gateway|...)` case-insensitively against the whole captured
  output. If a flyctl upgrade starts printing check output on failure, a
  deadlocked flip would retry 3x (~17 min of wedged deploy, and a stray proxy
  machine per attempt) instead of failing fast. If that ever happens, narrow
  that regex branch. Destroy any stray proxy machines (below).
- Revert after a successful flip: `git revert` the flip commit, push to
  main, let CI + deploy.yml ship it (or `bash scripts/fly-deploy.sh` from
  the reverted checkout in an emergency). EXPECT A HARD PUBLIC OUTAGE WINDOW
  (~1-2 min): flyctl destroys the (service-bearing!) proxy machines FIRST,
  and only then rolls app machines back onto the public-service config;
  between proxy-destroy and the first app machine passing its restored
  /health check, ZERO machines advertise the public service. This is the
  revert working as designed. Do NOT interrupt it, second-deploy over it, or
  improvise `fly machine` commands mid-roll (the documented incident
  amplifier, 2026-06-18/07-07). Watch `fly status` and loop
  `curl -fsS https://<public-host>/health` until 200.
- Break-glass, only if the window must be shorter: `fly deploy --strategy
  immediate` from the reverted checkout replaces all machines at once,
  skipping health gates. Acceptable ONLY because the target is the
  battle-tested phase-1/2 topology. (A staged two-deploy de-flip -- move the
  service back first, remove the group second -- can SOMETIMES shrink the
  window but cross-group update order is not contractual; treat it as
  best-effort, never the emergency path.)
- Leftover proxy machines -- verify, never assume, and MORE dangerous than
  before phase 2: with the apps permanently dual-stack, a surviving proxy
  machine's end-to-end check KEEPS PASSING, so it keeps serving a share of
  public traffic through the reverted-away Go path indefinitely (pre-phase-2
  it at least failed out of rotation within a check cycle). After ANY
  aborted or reverted flip deploy, `fly status` MUST show zero proxy
  machines; destroy survivors immediately and re-verify:

      fly machine list
      fly machine destroy <proxy-machine-id> --force   # per proxy machine

- The image keeps the (unexecuted) proxy binary after any fly.toml revert;
  harmless, no action.

## Pre-merge checklist (all mandatory, every phase)

- [ ] CI fully green on the PR. `docker-build` is the load-bearing job: the
      dev host has no docker, so CI is the ONLY place the Dockerfile (golang
      stage + `COPY --from`) is ever built. Never merge on a red or skipped
      docker-build.
- [ ] Trivy note: the API image contains the Go proxy binary (since #93), so
      the API image scan can fail on a FIXABLE Go stdlib CVE. The remedy is
      bumping the pinned `golang:` digest in the Dockerfile, not
      `.trivyignore`.
- [ ] `go-proxy` CI lane green (gofmt, vet, tests) and the full python gate
      (pytest incl. `tests/test_entrypoint_guard.py`, mypy, ruff, black).
- [ ] `fly config validate` passes against the new fly.toml, run from an
      AUTHED shell (it hits the Fly API; CI cannot run it).
- [ ] Merging to main auto-deploys via CI -> deploy.yml (workflow_run, no
      path filters) once CI is green -- treat every merge of these files as
      a deploy.

## Standing facts (unchanged by the un-flip)

`docker/entrypoint.sh` init-db stamp-guard matrix (locked by
tests/test_entrypoint_guard.py):

| command | init-db |
| ------- | ------- |
| `alembic ...` (release_command) | skipped -- must be able to move the stamp |
| `regwatch-proxy` (also path-qualified) | skipped -- proxy boots DB-independent; a proxy machine crash-looping on the stamp guard while holding the public port is the 2026-06-18/07-07 incident class |
| `uvicorn ...` (app group) | runs, then exports `REGWATCH_DB_INITIALIZED=1` |

Header semantics: the proxy forwards `Fly-Client-IP` and `X-Forwarded-*`
byte-for-byte (Go tests cover it), so `_client_ip` under
`TRUST_PROXY_HEADERS=true` keeps keying the login limiter on the
edge-attested client IP, before and after the flip.
`release_command = "alembic upgrade head"` stays the sole migration
authority in every phase.

Known doc drift (follow-up, NOT this slice): docs/DEPLOY.md's one-time
bootstrap fly.toml example has drifted materially from the live config -- DO
NOT bootstrap a DR/staging app from it; copy the committed fly.toml instead.
Verified by diffing the example against phase-1 fly.toml (2026-07-15), it
omits `[deploy] release_command` (the sole migration authority -- an app
bootstrapped without it walks straight into the 2026-06-18 boot-guard crash
loop), `env.REQUIRE_DATABASE_URL` (the B1 SQLite-fallback guard -- which
DEPLOY.md's own prose simultaneously claims fly.toml sets, so that doc
contradicts its own example), `env.TRUST_PROXY_HEADERS` (the limiter guard
tests/test_login_ratelimit_ip.py exists to protect), `env.SENTRY_ENVIRONMENT`,
`build.args.INSTALL_ORCHESTRATION`, `[processes]` and `kill_timeout`; and it
sets `min_machines_running = 1`, discarding the CD-2 rolling-deploy floor.
(Its app name and CORS origin also differ, legitimately -- it is a
bootstrap-a-different-app example.) Refresh it after phase 3 lands; until
then the real fly.toml plus this runbook are the source of truth.

## Out of scope for this slice (unchanged)

- No auth, routing, or header logic in Go beyond byte-for-byte pass-through
  (Step 4, docs/POLYGLOT_TARGET_2026-07-10.md R4).
- `/query/stream` stays a transparent relay until CompleteQuery is proven
  (plan R3).
- Framework choices (chi, sqlc, golangci-lint lane) remain Step 4 decisions;
  the proxy stays stdlib-only until then.
