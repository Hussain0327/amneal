# Go proxy rollout plan (strangler Step 3, deploy slice)

Status 2026-07-14: the proxy itself (`go/cmd/proxy`) is built, tested, and
gated in CI, but SHIPS INERT -- fly.toml, the Dockerfile, and deploy.yml are
untouched and no deployed behavior changes. This document is the plan for the
NEXT slice, the one that wires it into Fly.

Why split the slices: the 2026-06-18 incident class. Deploy-behavior changes
(boot guards, `release_command`, rolling machine replacement) have taken prod
down before; keeping "new code lands" and "traffic path changes" in separate,
individually revertable deploys means the flip commit is a one-file diff and
rollback is `git revert` + redeploy, never "which half broke".

## Current topology (fly.toml as of this slice)

- Single process group; image CMD runs uvicorn on :8000 (`API_PORT=8000`).
- `[http_service]` internal_port 8000, force_https, `min_machines_running = 2`,
  HTTP check on `/health` (served by FastAPI).
- `[deploy] release_command = "alembic upgrade head"` is the sole migration
  authority. Nothing in this plan touches it.
- No `kill_timeout` set, so Fly's default 5s SIGTERM->SIGKILL window applies.
- `TRUST_PROXY_HEADERS=true`: the app keys its login limiter on Fly-Client-IP
  with rightmost-XFF fallback (`main.py::_client_ip`). The proxy forwards
  both verbatim -- covered by Go tests; re-verify end-to-end at the flip.

## Target topology

Same Fly app, two process groups (Fly runs each group on separate machines):

```toml
[processes]
  app = "<current uvicorn command>"          # unchanged
  proxy = "/usr/local/bin/regwatch-proxy"

[env]
  # Only the proxy reads these; app-wide [env] is fine.
  UPSTREAM_URL = "http://app.process.amneal.internal:8000"
  PORT = "8080"

[http_service]
  processes = ["proxy"]   # the flip
  internal_port = 8080
  # checks move to GET /healthz (served by the proxy itself)
```

Notes:

- Dockerfile gains a `golang` build stage (CGO_ENABLED=0 static binary) that
  copies `/usr/local/bin/regwatch-proxy` into the runtime image. Both process
  groups share one image; an unexecuted binary is inert, so this can land and
  deploy ahead of any topology change.
- Process groups are SEPARATE machines, so the proxy cannot reach uvicorn on
  127.0.0.1. Upstream goes over 6PN private networking:
  `app.process.amneal.internal:8000`. Caveat: that DNS name resolves to ALL
  machines in the app group with no health awareness; if that round-robin is
  not acceptable at flip time, allocate a flycast address for the app group
  instead (private load balancing that honors health checks).
- Health checks after the flip: proxy group gets the `[http_service]` check on
  `/healthz` (local liveness, deliberately independent of the upstream so a
  Python restart does not recycle proxy machines). The app group keeps a
  `/health` machine check via `[checks]` since it no longer has a public
  service.
- `kill_timeout`: raise to >= 30s for the proxy group. The proxy drains
  in-flight requests (including SSE) for up to 20s on SIGTERM; under the
  default 5s window Fly would SIGKILL mid-drain and sever live streams.
- `/healthz` is intercepted by the proxy (exact path only). FastAPI defines
  `/health`, not `/healthz`, so nothing is shadowed today; if the Python app
  ever adds `/healthz` it will not be reachable through the proxy.
- `min_machines_running`/`fly scale count` apply per process group: keep 2 for
  proxy (it becomes the public edge) and 2 for app.

## Flip sequence (each step its own deploy, each independently revertable)

1. Dockerfile: add the Go build stage + binary. Deploy. Prod behavior
   unchanged (binary never executed); verify /health and a live query.
2. fly.toml: add `[processes]` (with `[http_service] processes = ["app"]`
   still pointing at uvicorn), `[env]` UPSTREAM_URL/PORT, app-group
   `[checks]`. Deploy. Proxy machines boot but take no public traffic;
   verify from inside the network (`fly ssh console` + curl
   `proxy.process.amneal.internal:8080/healthz`, then a proxied `/health`).
3. fly.toml: flip `[http_service]` to `processes = ["proxy"]`,
   `internal_port = 8080`, check path `/healthz`, `kill_timeout = 30`.
   Deploy. This is the only traffic-affecting commit.
4. Smoke immediately: `/health` (now proxied), login (Fly-Client-IP limiter
   semantics), `/query`, and `/query/stream` -- confirm tokens arrive
   incrementally, not in one buffered burst at stream end.
5. Rollback at any point: `git revert` the step-3 commit and redeploy;
   `[http_service]` routes straight to uvicorn again. Idle proxy machines are
   harmless. No schema involvement anywhere: `release_command` stays alembic
   throughout, so the Jun-18 boot-guard/migration failure mode is out of
   scope for this flip by construction.

## Out of scope for the flip

- No auth, routing, or header logic in Go beyond pass-through (that is
  Step 4, docs/POLYGLOT_TARGET_2026-07-10.md R4).
- `/query/stream` stays a byte-for-byte relay until CompleteQuery is proven
  (plan R3).
- Framework choices (chi, sqlc, golangci-lint lane) are Step 4 decisions;
  the proxy stays stdlib-only until then.
