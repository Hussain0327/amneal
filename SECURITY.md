# Security Policy

REGWATCH is currently an internal proof-of-concept / pilot application. It is
not approved for direct external exposure or production use until the blocking
items in `docs/PROD_READINESS.md` are closed.

This is the current security policy. Older planning notes may describe missing
auth, missing rate limits, or the retired Streamlit UI; treat those as archived
history when they conflict with this file, `README.md`, `docs/ARCHITECTURE.md`,
`docs/PROD_READINESS.md`, or `docs/ROADMAP.md`. The current UI is a single
Next.js App Router shell (Streamlit is fully retired). Application
authentication, in-process rate limiting, and a dual-mode Postgres/pgvector
datastore path are all implemented in code; the remaining gaps are operational
(production identity boundary, distributed limiting, a provisioned managed
datastore, secrets management, and CI supply-chain scanning).

## Current Security Status

Do not deploy this service on a public network as-is.

Known launch blockers:

- Application authentication and authorization are enforced by FastAPI for all
  endpoints except `GET /health` (DB-backed opaque cookie sessions, sha256 at
  rest, bcrypt passwords, per-user ownership of chat history). Broad production
  exposure still needs an approved identity boundary: TLS, gateway controls, and
  either enterprise SSO/OIDC in front of the app or a formal decision accepting
  the app-layer cookie sessions for the pilot boundary. `AUTH_COOKIE_SECURE`
  defaults to false and must be set to true once TLS terminates in front of the
  app.
- Per-caller rate limits are enforced in-process for cost-bearing routes and
  login attempts. Multi-process or multi-replica production still needs
  distributed/gateway rate limiting and abuse controls.
- CORS is allow-listed for the deployed UI origin and local development, but
  CORS is not an authentication control.
- Local development uses `.env` files; production must use a secret manager or
  approved platform secret injection.
- The default local Compose stack uses demo-friendly defaults and is not a
  hardened production deployment.
- CI does not yet run dependency vulnerability audits or container image scans.

The application is designed for public FDA source material only. Do not submit
PHI, patient data, proprietary submission content, confidential business data,
or credentials into the UI, API, logs, tests, fixtures, issues, or commits.

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

- Never commit `.env`, `.env.local`, API keys, service account files, database
  dumps (SQLite or Postgres/pgvector), Chroma stores, raw FDA ingest caches, or
  logs containing request data.
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
- Production datastore controls: the dual-mode Postgres/pgvector path is
  implemented in code (`DATABASE_URL` switches the structured store to Postgres
  and vectors to pgvector in the same DB; `REQUIRE_DATABASE_URL=true` refuses a
  SQLite fallback; pgvector dimension checks fail fast; Postgres boot verifies
  the Alembic stamp equals head and refuses to start on mismatch), but a managed
  database/vector store is not yet provisioned. Still required: provisioning,
  encryption, backups, restore-drill testing, and least-privilege app DB creds.
- Alembic migrations run as a gated deploy step, not during application boot
  (app boot verification is the safety net, not the migration mechanism).
- Centralized logs, metrics, tracing, error reporting, and security alerts.
- Dependency audit and container image scanning in CI.
- Approved LLM/data-handling decision documented in `docs/DECISIONS.md`.
- Incident response and rollback procedures.

See `docs/PROD_READINESS.md` for the full production-readiness checklist and
`docs/ROADMAP.md` for the consolidated list of open security and operational
items.
