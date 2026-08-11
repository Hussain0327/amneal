# syntax=docker/dockerfile:1

# Build stage for the Go reverse proxy (staged for the phase-3 "proxy" process
# group, docs/GO_PROXY_ROLLOUT.md; no fly.toml group execs it today).
# CGO_ENABLED=0 over a stdlib-only module yields a
# fully static binary, so the python:3.12-slim runtime stage below needs no
# extra libraries. Digest-pinned like the python base (mutable tag kept as
# documentation); bump by resolving the multi-arch INDEX digest:
#   docker manifest inspect golang:1.26.5-alpine
# Scan note: this puts a Go stdlib binary inside the Trivy-scanned API image,
# so a fixable Go stdlib CVE can now fail that gate; the remedy is bumping
# this digest, not .trivyignore.
FROM golang:1.26.5-alpine@sha256:0178a641fbb4858c5f1b48e34bdaabe0350a330a1b1149aabd498d0699ff5fb2 AS proxy-build
# The pinned toolchain alone must satisfy go.mod's `go` directive:
# GOTOOLCHAIN=local turns "pinned image too old" into a loud build failure
# instead of a silent mid-build network download of a different toolchain.
ENV GOTOOLCHAIN=local CGO_ENABLED=0
WORKDIR /src
# go.mod+go.sum before sources so the dependency-download layer stays cached
# across source edits. go.sum makes the download sum-VERIFIED. Since the
# step-4 auth cutover, cmd/proxy imports internal/api + internal/store, so
# pgx and x/crypto ARE linked into the shipped binary (still static,
# CGO-free); their Go-module CVEs now gate the Trivy scans like any other
# shipped dependency -- fix by bumping, never by ignoring.
COPY go/go.mod go/go.sum ./
RUN go mod download
COPY go/cmd ./cmd
COPY go/internal ./internal
RUN go build -trimpath -ldflags "-s -w" -o /usr/local/bin/regwatch-proxy ./cmd/proxy

# Two build flavors, gated by INSTALL_LOCAL_EMBEDDINGS:
#   * slim (default, production): no torch/sentence-transformers. Run with
#     EMBEDDING_PROVIDER=openai + OPENAI_API_KEY (+ DATABASE_URL for
#     Postgres/pgvector). The openai SDK ships via the `llm` extra, so the slim
#     image is fully embedding-capable. Production runs this flavor against
#     Databricks Lakebase Postgres, and embeds through the active Qwen3 profile
#     rather than through EMBEDDING_PROVIDER. See docs/DEPLOY.md.
#   * local ingest: --build-arg INSTALL_LOCAL_EMBEDDINGS=true, then run with
#     EMBEDDING_PROVIDER=local-bge-small.
# The in-image EMBEDDING_PROVIDER=echo default below is for empty-corpus smoke
# tests only; the API refuses to boot an echo provider against a seeded corpus.
# Digest-pinned base: the tag is mutable (Debian rebuilds republish 3.12-slim),
# so pin the exact multi-arch index digest for a reproducible, tamper-evident
# build. Bump the digest; resolve with:  docker manifest inspect python:3.12-slim
# (the index/list digest, not a per-arch child). Keep PYTHON_VERSION as
# documentation only.
ARG PYTHON_VERSION=3.12
FROM python:3.12-slim@sha256:6c4dd321d176d61ea848dc8c73a4f7dbae8f70e0ee48bb411ea2f045b599fa8e
ARG INSTALL_LOCAL_EMBEDDINGS=false

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:${PATH}" \
    DATA_DIR=/app/data \
    RAW_PDF_DIR=/app/data/raw \
    PROCESSED_DIR=/app/data/processed \
    EMBEDDING_PROVIDER=echo \
    WHITEPAPER_TEMPLATE_PATH=/app/data/templates/cra_white_paper_template.docx

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Run as an unprivileged user (defense in depth: a compromised process cannot
# write outside its own tree or escalate via root). Fixed uid/gid 1001 so the
# identity is stable across rebuilds. Everything the app writes lives under
# /app (the .venv, DATA_DIR=/app/data and its children); ownership is set after
# the build copies, just before USER below.
RUN groupadd --system --gid 1001 regwatch \
    && useradd --system --uid 1001 --gid 1001 --home-dir /app --no-create-home regwatch

RUN pip install --no-cache-dir uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
# Word-splitting on $EXTRAS is intentional (POSIX sh flag accumulation); the
# values are fixed literals, never user input.
RUN EXTRAS="--extra llm" \
    && if [ "$INSTALL_LOCAL_EMBEDDINGS" = "true" ]; then EXTRAS="$EXTRAS --extra local-embeddings"; fi \
    && uv sync --frozen $EXTRAS --no-dev --no-install-project

COPY README.md alembic.ini ./
COPY config ./config
COPY migrations ./migrations
COPY scripts ./scripts
COPY src ./src
COPY docker/entrypoint.sh /usr/local/bin/regwatch-entrypoint

# Compilers and kernel headers are build-only. Leaving build-essential in the
# runtime image shipped linux-libc-dev (and its fixable kernel CVEs) even though
# neither the Python app nor the static Go proxy uses it.
RUN chmod +x /usr/local/bin/regwatch-entrypoint \
    && EXTRAS="--extra llm" \
    && if [ "$INSTALL_LOCAL_EMBEDDINGS" = "true" ]; then EXTRAS="$EXTRAS --extra local-embeddings"; fi \
    && uv sync --frozen $EXTRAS --no-dev \
    && apt-get purge -y --auto-remove build-essential \
    && rm -rf /var/lib/apt/lists/*

# Static proxy binary. Ships inert: the phase-3 "proxy" process group
# (docs/GO_PROXY_ROLLOUT.md) will exec it through the entrypoint; no fly.toml
# group runs it today. Copied after the python layers on purpose: a Go-only
# change must not invalidate the uv dependency cache above.
COPY --from=proxy-build /usr/local/bin/regwatch-proxy /usr/local/bin/regwatch-proxy

# The CRA White Paper Word template is gitignored (internal artifact) and is
# deliberately NOT baked into the image. WHITEPAPER_TEMPLATE_PATH (set above)
# defaults to a path under the mounted /app/data volume, so an operator drops
# the official .docx at data/templates/cra_white_paper_template.docx to enable
# real-template fill. Absent it, POST /whitepaper/runs/{id}/docx still returns a
# structurally-equivalent document stamped "(generated without the official CRA
# template file)". See docs/DEPLOY.md and src/regwatch/whitepaper/docx_writer.py.
EXPOSE 8000

# Hand /app (the .venv + everything the entrypoint writes under DATA_DIR) to the
# unprivileged user, then drop privileges. The entrypoint runs `mkdir -p` under
# DATA_DIR and `regwatch init-db` as this user, so /app must be writable by it;
# pre-creating /app/data makes the writable root explicit even on a fresh
# (ephemeral) Fly disk. On a compose bind-mount (./data:/app/data) the host owns
# the mount, so the developer's dir must be writable by uid 1001 there.
RUN mkdir -p "$DATA_DIR" \
    && chown -R regwatch:regwatch /app
USER regwatch

ENTRYPOINT ["regwatch-entrypoint"]
# On Fly, fly.toml [processes].app supersedes this CMD but has the same argv --
# tests/test_boot_command_drift.py enforces that now, so this is a contract and
# no longer a comment nobody checks.
#
# `regwatch serve` binds ONE SOCKET PER FAMILY (docs/GO_PROXY_ROLLOUT.md phase
# 2). Do NOT "simplify" it back to a uvicorn --host flag: "--host ::" is
# IPv6-ONLY under single-process uvicorn (asyncio/uvloop force IPV6_V6ONLY=1 on
# the loop.create_server path), so it refuses flyd's IPv4 health checks and Fly
# Proxy's private-IPv4 backhaul; "--host 0.0.0.0" is IPv4-only and is exactly
# what refused the Go proxy's 6PN dials in the 2026-07-15 failed deploy. One
# --host cannot serve both families. See root cause 2 in the runbook.
CMD ["regwatch", "serve"]
