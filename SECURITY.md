# Security Policy

REGWATCH is currently an internal proof-of-concept / pilot application. It is
not approved for direct external exposure or production use until the blocking
items in `docs/PROD_READINESS.md` are closed.

This is the current security policy. Older planning notes may describe missing
auth, missing rate limits, or retired UI surfaces; treat those as archived
history when they conflict with this file, `README.md`, `docs/ARCHITECTURE.md`,
or `docs/PROD_READINESS.md`.

## Current Security Status

Do not deploy this service on a public network as-is.

Known launch blockers:

- Application authentication and authorization are enforced by FastAPI for all
  non-health endpoints, but broad production exposure still needs an approved
  identity boundary: TLS, gateway controls, and either enterprise SSO/OIDC in
  front of the app or a formal decision accepting the app-layer cookie sessions
  for the pilot boundary.
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
  dumps, Chroma stores, raw FDA ingest caches, or logs containing request data.
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
- Production datastore controls: managed database/vector store, encryption,
  backups, restore testing, and least-privilege access.
- Alembic migrations run as a gated deploy step, not during application boot.
- Centralized logs, metrics, tracing, error reporting, and security alerts.
- Dependency audit and container image scanning in CI.
- Approved LLM/data-handling decision documented in `docs/DECISIONS.md`.
- Incident response and rollback procedures.

See `docs/PROD_READINESS.md` for the full production-readiness checklist.
