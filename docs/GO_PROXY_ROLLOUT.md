# Go proxy rollout (strangler Step 3): the traffic-flip runbook

Status 2026-07-14: AS-BUILT. The proxy itself (`go/cmd/proxy`) merged inert in
PR #84; THIS change wires it into Fly and flips the public port to it:

- `fly.toml` gains `[processes]` (`app` = the old uvicorn CMD with ONE
  deliberate change, `--host ::` instead of `0.0.0.0` -- 6PN is IPv6-only,
  see the topology section; `proxy` = `regwatch-proxy`); `[http_service]`
  fronts the proxy group on 8080; the app group keeps deploy gating via a
  machine check (`[checks.app_health]`); `kill_timeout = 30` covers the
  proxy's 20s SIGTERM drain.
- `Dockerfile` gains a digest-pinned golang stage that builds the static
  binary into the shared image at `/usr/local/bin/regwatch-proxy`.
- `docker/entrypoint.sh` exempts `regwatch-proxy` from the init-db stamp
  guard (`tests/test_entrypoint_guard.py` locks the skip/run matrix).

Uvicorn keeps serving exactly what it served before; it just stops holding
the public port (and binds `::` instead of `0.0.0.0` so the proxy can reach
it over 6PN).

## Why this is the risk slice

Both prod incidents were deploy-topology incidents: 2026-06-18 (boot-guard
crash loop + lock pileup) and 2026-07-07 (pre-migrated DB vs boot guard
killing machines on restart). This change alters WHO holds the public port,
so it ships as ONE commit that is one `git revert` away from the old
topology, and the deploy is watched end-to-end (matrix below).

Deviations from the earlier plan in this file, on purpose:

- One deploy, not three. The staged sequence (binary first, dormant groups
  second, flip third) optimized for intermediate observability, but each
  intermediate state was itself a novel topology to debug. A single flip
  commit keeps exactly one revertable unit; the compensating control is the
  watched-deploy matrix.
- The edge health check stays `GET /health` END-TO-END (edge -> proxy -> 6PN
  -> uvicorn -> DB), NOT the proxy-local `/healthz`. Edge rotation must gate
  on the whole path: a proxy whose upstream is unreachable has to fall out of
  rotation, not stay "healthy" while serving 502s. `/healthz` remains a
  local-liveness debug endpoint only.
- No flycast address for the app group yet. `app.process.amneal.internal`
  resolves to RUNNING app machines with no health filtering; that is
  acceptable while `auto_stop_machines = false` keeps them up and
  `[checks.app_health]` gates them during rolls. Revisit flycast if app
  machines ever autostop or the group grows.
- `[processes].app` is deliberately NOT string-identical to the image CMD:
  the host is `::` instead of `0.0.0.0` (module, port, and semantics
  unchanged). Reason in the topology section below; the image CMD keeps
  `0.0.0.0` for local docker/compose runs, and on Fly `[processes]`
  supersedes CMD.

## Topology after this commit

Two process groups, one image, SEPARATE machines per group (Fly semantics):

| group | command | port | public? | health gate |
| ----- | ------- | ---- | ------- | ----------- |
| proxy | `regwatch-proxy` (via entrypoint) | 8080 (proxy PORT default; must match `internal_port`) | yes, `[http_service]` | edge check `GET /health` through the proxy (end-to-end) |
| app | `uvicorn regwatch.api.main:app --host :: --port 8000` | 8000 | no | machine check `[checks.app_health]` `GET /health`, same cadence as the old edge check |

The app bind is `::`, NOT the pre-flip `0.0.0.0`: `app.process.amneal.internal`
is a 6PN name, and 6PN is IPv6-only, so an IPv4-only listener would refuse
every proxy->app dial -- including the end-to-end `/health` check that admits
proxy machines into rotation. With `0.0.0.0` the flip would deploy into a
total public outage (the exact incident class this runbook exists for).
Uvicorn selects AF_INET6 for any host containing `:` and never sets
IPV6_V6ONLY (verified in the pinned uvicorn's `bind_socket`), so on Linux
(`bindv6only=0` default) `::` is a dual-stack listener: native IPv6 for the
proxy path, IPv4-mapped for whichever family flyd dials `[checks.app_health]`
with. Nothing in this repo exercised 6PN before this change, and the Go tests
(httptest on 127.0.0.1) structurally cannot -- the deploy's own end-to-end
health check plus matrix row 3 are the first live proof.

`docker/entrypoint.sh` init-db stamp-guard matrix (tested):

| command | init-db |
| ------- | ------- |
| `alembic ...` (release_command) | skipped -- must be able to move the stamp |
| `regwatch-proxy` (also path-qualified) | skipped -- proxy boots DB-independent; a proxy machine crash-looping on the stamp guard while holding the public port is the 2026-06-18/07-07 incident class |
| `uvicorn ...` (app group) | runs, then exports `REGWATCH_DB_INITIALIZED=1` |

Header semantics are unchanged: the proxy forwards `Fly-Client-IP` and
`X-Forwarded-*` byte-for-byte (Go tests cover it), so `_client_ip` under
`TRUST_PROXY_HEADERS=true` keeps keying the login limiter on the
edge-attested client IP. `release_command = "alembic upgrade head"` is
untouched and remains the sole migration authority.

## Pre-merge checklist (all mandatory)

- [ ] CI fully green on the PR. `docker-build` is the load-bearing job: the
      dev host has no docker, so CI is the ONLY place this Dockerfile (golang
      stage + `COPY --from`) has ever been built. Do not merge on a red or
      skipped docker-build, ever.
- [ ] Trivy note: the API image now contains a Go stdlib binary, so the API
      image scan (ci.yml docker-build + deploy.yml re-scan) can newly fail on
      a FIXABLE Go stdlib CVE -- previously a web-image-only failure class.
      The remedy is bumping the pinned `golang:` digest in the Dockerfile and
      rebuilding, not `.trivyignore`.
- [ ] `go-proxy` CI lane green (gofmt, vet, tests) and the full python gate
      (pytest incl. `tests/test_entrypoint_guard.py`, mypy, ruff, black).
- [ ] `fly config validate` passes against the new `fly.toml`.
- [ ] Pick a low-traffic window and have `fly logs`, `fly status`, and this
      runbook open BEFORE merging: merging to main auto-deploys via
      deploy.yml -> `scripts/fly-deploy.sh` once CI is green.

Known doc drift (follow-up, NOT this slice): docs/DEPLOY.md's one-time
bootstrap fly.toml example still shows the pre-flip single-group topology
(public port on uvicorn, no `[processes]`/`[checks]`). Bootstrapping a
new/DR/staging app from it silently omits the proxy layer; until it is
refreshed, the real `fly.toml` plus this runbook are the source of truth.

## What the first deploy does (watch it live)

1. deploy.yml runs `scripts/fly-deploy.sh` (bounded retry on transient Fly
   errors only). `release_command` runs `alembic upgrade head` first --
   unchanged, and a no-op unless the commit also carries a migration.
2. Fly CREATES at least one machine (typically exactly one) for the new
   `proxy` group -- `min_machines_running = 2` is only an autostop floor, it
   creates nothing -- and BLOCKS until that machine passes smoke checks,
   reaches `started`, and passes its end-to-end `/health` check. That check
   dials `app.process.amneal.internal`, which ALREADY resolves: the pre-flip
   machines belong to the default group, which Fly names `app`. So a proxy
   that cannot reach uvicorn over 6PN fails the deploy RIGHT HERE, with the
   still-serving old machines untouched -- zero prod impact. (Order verified
   against flyctl master `deployMachinesApp`: new-group machine creation and
   its health-check wait run BEFORE `updateExistingMachines`.)
3. The existing 2 machines -- kept in the `app` group by name, see step 2 --
   are updated in place (rolling, one at a time, gated by
   `[checks.app_health]`): their command flips to the `::` bind and their
   public service definition moves off them.
4. During the roll the edge is served by a mix of old-config app machines
   (uvicorn directly) and the new proxy machine(s); both serve the same API
   bytes. Two transient signatures are EXPECTED and are not, by themselves,
   rollback triggers:
   - isolated proxy `upstream error` / single 502s while an app machine
     restarts (the Go proxy only auto-retries idempotent requests on dead
     pooled connections; a POST caught mid-restart can 502 once);
   - a brief public 503 ("no healthy instances") window if the timing goes
     adverse (or under an older/newer flyctl that orders the roll
     differently than verified above).
   Decision rule, so it lives on paper and not in the operator's head:
   public 503s persisting beyond ~2 minutes after the proxy machine shows
   `started`, or its `/health` check never going green, IS the rollback
   trigger (see Rollback).
5. IMMEDIATELY after the deploy goes green:

       fly scale count proxy=2 app=2

   Until this runs, one proxy machine is a single point of failure for the
   entire public edge. Do not leave the deploy in that state; if scaling
   fails, treat it as a rollback trigger.

## Watched-deploy verification matrix (run every row, in order)

| # | check | how | pass looks like |
| - | ----- | --- | --------------- |
| 1 | both groups healthy | `fly status` | 2x `app` + (after scaling) 2x `proxy`, all `started`, all checks passing |
| 2 | proxy holds the edge | `curl -fsS https://<public-host>/healthz` | `ok` -- served by the proxy itself; uvicorn has no `/healthz` route, so this proves the flip happened |
| 3 | end-to-end health | `curl -fsS https://<public-host>/health` | 200, `"status": "ok"`, `db.ok: true` -- full edge -> proxy -> 6PN -> uvicorn -> DB path. Also the manual proof of 6PN reachability (proxy dialing uvicorn's `::` bind over IPv6): no pre-prod environment exercises that hop |
| 4 | auth + cookie flow | login via the frontend (or `curl -c` the login endpoint), then hit an authed route with the cookie | authed 200; cookie round-trips through the proxy |
| 5 | streaming | authed `curl -N -X POST https://<public-host>/query/stream ...` | `event: token` frames arrive INCREMENTALLY, not one buffered burst at stream end (FlushInterval -1 doing its job) |
| 6 | Fly-Client-IP reaches the app | from one network, 5 bad logins then a 6th; from a second network (phone hotspot), 1 bad login. Or read app logs for the recorded client IP | 6th attempt 429s while the other network still gets 401 -- limiter keys on the real client IP, not the proxy machine's |
| 7 | no proxy errors | `fly logs` | no `upstream error:` lines from the proxy during the smoke window |

Row 6 is the limiter-semantics check: if the proxy mangled `Fly-Client-IP` or
appended to `X-Forwarded-For`, every caller would collapse into one bucket
(both networks would 429 together) or the fallback would break entirely.

## Rollback

- `git revert` the flip commit, push to main, let CI + deploy.yml ship it
  (or `bash scripts/fly-deploy.sh` from the reverted checkout in an
  emergency). `[http_service]` points back at uvicorn:8000 and the
  single-group topology is restored. No schema involvement in either
  direction: `release_command` is untouched by flip and revert alike, so the
  Jun-18 migration failure mode is out of scope by construction.
- EXPECT A HARD PUBLIC OUTAGE WINDOW (~1-2 min) DURING THE REVERT DEPLOY.
  Current flyctl destroys the machines of a process group that vanished from
  the config FIRST, and only then rolls the app machines back onto the
  service-bearing config (verified in flyctl master `deployMachinesApp`:
  destroying `machinesToRemove` precedes `updateExistingMachines`; Fly docs
  concur that the next deploy destroys a deleted group's machines). Between
  proxy-destroy and the first app machine passing its restored `/health`
  check -- stop, image pull, init-db, uvicorn boot, check pass -- ZERO
  machines advertise the public service: the edge hard-503s and in-flight
  requests/SSE streams are severed. This is the revert WORKING AS DESIGNED.
  Do NOT interrupt it, second-deploy over it, or improvise `fly machine`
  commands mid-roll (improvised machine surgery mid-deploy is this repo's
  documented incident amplifier, 2026-06-18/07-07). Instead watch
  `fly status` and loop `curl -fsS https://<public-host>/health` until 200.
- Break-glass, only if the window must be shorter: `fly deploy --strategy
  immediate` from the reverted checkout replaces all machines at once,
  skipping health-gating. Acceptable ONLY here because the reverted config
  is the battle-tested pre-flip topology.
- Leftover proxy machines -- verify, do not assume: current flyctl
  auto-destroys them (above), but older flyctl versions may not prune. After
  the revert deploy, `fly status` MUST show zero `proxy` machines; that
  check is part of the rollback, not optional hygiene. A surviving proxy
  machine is NOT inert: fly-proxy routes on per-machine service
  registration, not on fly.toml, so a survivor still advertises the public
  http_service on 8080 and keeps taking a share of edge traffic through the
  reverted-away Go path -- until its end-to-end check fails (the reverted
  app group's `0.0.0.0` bind is unreachable over 6PN, so post-revert it
  serves 502s for up to a check cycle before falling out of rotation, and
  stays one config-drift away from serving again). If the rollback reason
  was a proxy bug, a survivor is still exercising that bug on live traffic.
  Destroy immediately, then re-verify with `fly status`:

      fly machine list
      fly machine destroy <proxy-machine-id> --force   # per proxy machine

- The image keeps the (now unexecuted) proxy binary after a revert of
  fly.toml alone; that is harmless and needs no separate action.

## Out of scope for this slice (unchanged)

- No auth, routing, or header logic in Go beyond byte-for-byte pass-through
  (Step 4, docs/POLYGLOT_TARGET_2026-07-10.md R4).
- `/query/stream` stays a transparent relay until CompleteQuery is proven
  (plan R3).
- Framework choices (chi, sqlc, golangci-lint lane) remain Step 4 decisions;
  the proxy stays stdlib-only until then.
