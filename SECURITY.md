# Security Policy

REGWATCH is currently an internal proof-of-concept / pilot application. It is
not approved for direct external exposure or production use until the blocking
items in `docs/PROD_READINESS.md` are closed.

This is the current security policy. Older planning notes may describe missing
auth, missing rate limits, or the retired Streamlit UI; treat those as archived
history (`docs/archive/`) when they conflict with this file, `README.md`,
`docs/ARCHITECTURE.md`, `docs/PROD_READINESS.md`, or `docs/ROADMAP.md`. The
current UI is a single Next.js App Router shell (Streamlit is fully retired).
Application authentication, in-process rate limiting, and Postgres+pgvector as
the only datastore are all implemented and deployed; the remaining gaps are
operational (enterprise identity boundary, distributed limiting, least-privilege
DB credentials, a rehearsed restore drill, and secrets policy).

## Current Security Status

The pilot **is** deployed on a public network — behind Fly's edge with
`force_https = true`, an allow-listed CORS origin, and login required on every
product endpoint. It is scoped to internal pilot users and is not approved for
external or general availability.

Known launch blockers:

- Authentication and authorization are enforced on every **product** endpoint.
  Since the polyglot step-4 cutover the **Go edge** owns cookie sessions
  (DB-backed opaque tokens, sha256 at rest, bcrypt passwords, per-user ownership
  of chat history) — not FastAPI. The only unauthenticated routes are the
  operational probes: `GET /health`, `GET /ready`, `GET /metrics` (bearer-gated
  when `METRICS_TOKEN` is set, world-readable when it is not), and the Go edge's
  own `GET /healthz`. Broad production exposure still needs an approved identity
  boundary: enterprise SSO/OIDC in front of the app, or a formal decision
  accepting the app-layer cookie sessions as the pilot boundary.
- Per-caller rate limits are enforced in-process for cost-bearing routes and
  login attempts. Both runtimes keep **separate** in-memory buckets and the proxy
  runs on more than one machine, so the effective fleet ceiling is a multiple of
  the configured rate until limiting is distributed or the gateway owns it.
- CORS is allow-listed for the deployed UI origin and local development, but
  CORS is not an authentication control.
- Local development uses `.env` files; production uses Fly/Vercel platform
  secrets. An approved secret-manager policy with rehearsed rotation is open.
- The default local Compose stack uses demo-friendly defaults and is not a
  hardened production deployment.
- No container resource limits are set in `compose.yaml` or `fly.toml`.

The application is designed for public FDA source material only. Do not submit
PHI, patient data, confidential business data, or credentials into the UI, API,
logs, tests, fixtures, issues, or commits.

**Deficiency-analysis uploads are a scoped exception.** `POST /deficiency/analyze`
accepts a submission PDF from any authenticated user. Per `docs/DECISIONS.md`
(Jul 30-31 2026), uploads are expected to be **public documents** during the
pilot, and the app treats every upload as sensitive regardless: parsing and all
inference stay in-tenant, the PDF bytes are never persisted (only a sha256), and
the temp file is deleted when the run ends. The PHI/credentials prohibition above
is absolute and applies to these uploads too.

## Supported Versions

There are no generally supported production releases yet.

| Version / branch | Supported for security fixes |
| --- | --- |
| `main` | Yes, for internal pilot work |
| Tagged production releases | Not available yet |
| Local demo branches | No |

Security fixes should target `main` unless a production release branch is
created later.

## Reporting a Vulnerability

Report suspected vulnerabilities through the approved Amneal internal security
or IT intake process. If no formal intake is available for this pilot, contact
the repository owner or project maintainer directly through an internal private
channel.

Do not disclose vulnerabilities in public issues, public chat, screenshots, or
external ticket systems. Do not include real secrets, API keys, PHI, proprietary
data, or full production logs in the report.

Include:

- A short description of the issue and affected component.
- The affected commit, branch, deployment, or environment.
- Steps to reproduce in a local or authorized test environment.
- Expected impact, including whether data exposure, mutation, cost abuse, or
  service disruption is possible.
- Sanitized logs, request IDs, screenshots, or proof-of-concept requests.
- Any temporary mitigation already applied.

Expected handling for internal reports:

- Acknowledge receipt within 2 business days.
- Triage severity and owner within 5 business days.
- Provide a remediation plan or accepted-risk decision within 10 business days
  for high or critical findings.
- Rotate affected credentials immediately if exposure is suspected.

## Authorized Testing

Security testing is allowed only against local development, approved staging,
or explicitly authorized pilot environments.

Allowed testing includes:

- Authentication and authorization checks.
- CORS and browser-origin validation.
- Input validation and prompt-injection testing using non-sensitive data.
- Dependency, static-analysis, and container scanning.
- API abuse testing at low volume in non-production environments.

Do not perform:

- Testing against public FDA systems beyond normal documented application use.
- High-volume load tests without written approval.
- Attempts to access, exfiltrate, or modify data outside the authorized test
  environment.
- Social engineering, phishing, physical attacks, or third-party service abuse.

## Secret Handling

- Never commit `.env`, `.env.local`, API keys, service account files, Postgres
  dumps, raw FDA ingest caches, uploaded submission PDFs, or logs containing
  request data.
- Use `.env.example` only for empty placeholders and documented configuration.
- For production, use an approved secret manager or platform-managed
  environment variables.
- Rotate any credential that may have been printed, committed, pasted into a
  ticket, or sent to an LLM/chat tool.

## Production Security Requirements

Before any external or broad internal production launch, REGWATCH needs:

- An approved production identity boundary: enterprise gateway/SSO or a signed
  acceptance of the app-layer cookie-session model for the launch scope.
- Distributed rate limiting and abuse controls for LLM-backed routes.
- A documented CORS allowlist for the deployed UI origin.
- Production datastore controls: managed Postgres+pgvector is provisioned and is
  the only datastore since R5. `DATABASE_URL` is mandatory and the app refuses to
  boot without it; pgvector dimension checks fail fast; boot verifies the Alembic
  stamp equals head and refuses to start on mismatch. **Still required:**
  least-privilege application DB credentials and a rehearsed restore drill —
  neither has been done.
- Centralized logs, metrics, tracing, error reporting, and security alerts.
  Structured logging, audit rows, Sentry, `/health`, `/ready` and `/metrics`
  counters ship today; latency and cost metrics are recorded but not exported,
  and there is no tracing.
- Incident response and rollback procedures.

See `docs/PROD_READINESS.md` for the full production-readiness checklist and
`docs/ROADMAP.md` for the consolidated list of open security and operational
items.
