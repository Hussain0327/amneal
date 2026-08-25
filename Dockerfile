# syntax=docker/dockerfile:1

# Build stage for the Go reverse proxy (staged for the phase-3 "proxy" process
# group, docs/GO_PROXY_ROLLOUT.md; no fly.toml group execs it today).
# CGO_ENABLED=0 over a stdlib-only module yields a
# fully static binary, so the python:3.12-slim runtime stage below needs no
# extra libraries. Digest-pinned like the python base (mutable tag kept as
# documentation); bump by resolving the multi-arch INDEX digest:
#   docker manifest inspect golang:1.26.6-alpine
# Scan note: this puts a Go stdlib binary inside the Trivy-scanned API image,
# so a fixable Go stdlib CVE can now fail that gate; the remedy is bumping
# this digest, not .trivyignore.
FROM golang:1.26.6-alpine@sha256:af8d6740070b8906d12eae1c3e3ea0957fb63f492051ea05e354c38ef9fe88df AS proxy-build
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

# One slim flavor: no torch/sentence-transformers. Run with
# INGEST_EMBEDDING_PROVIDER=openai, LLM_PROVIDER=openai, OPENAI_API_KEY, and a
# PostgreSQL/pgvector DATABASE_URL. The OpenAI SDK ships via the `llm` extra.
# Retrieval serves an OpenAI text-embedding-3-large profile at 1024 dimensions.
# See docs/DEPLOY.md.
# EMBEDDING_PROVIDER is deliberately NOT defaulted in the image (2026-08-14
# postmortem): every process must be told its providers explicitly or refuse
# to boot. Smoke tests pass EMBEDDING_PROVIDER=echo explicitly.
# Digest-pinned base: the tag is mutable (Debian rebuilds republish 3.12-slim),
# so pin the exact multi-arch index digest for a reproducible, tamper-evident
# build. Bump the digest; resolve with:  docker manifest inspect python:3.12-slim
# (the index/list digest, not a per-arch child). Keep PYTHON_VERSION as
# documentation only. The SAME digest is used for both Python stages below so
# the .venv built in one runs unchanged in the other.
ARG PYTHON_VERSION=3.12

# Python build stage (root, throwaway). Everything that needs a compiler or
# uv happens here; only /app/.venv crosses into the runtime stage. Two stages
# instead of one (2026-08-25): the old single-stage image ended with
# `chown -R regwatch:regwatch /app`, and overlayfs copies every file it
# re-owns into a NEW layer -- so every commit shipped a second ~290 MB copy of
# the .venv on top of the cached one, bloating the image and the CI layer
# cache alike. Here ownership is set by COPY --chown at copy time, which adds
# no layer, and build-essential never enters the shipped image at all.
FROM python:3.12-slim@sha256:6c4dd321d176d61ea848dc8c73a4f7dbae8f70e0ee48bb411ea2f045b599fa8e AS python-build

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:${PATH}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app

# Lockfile first so the dependency layer stays cached across source edits.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --extra llm --no-dev --no-install-project

# uv installs the project itself EDITABLE (hatchling backend), so this second
# sync only adds a .pth pointing at /app/src plus the dist-info: the .venv
# bytes stay identical across source-only commits and the runtime stage's
# COPY of it keeps hitting cache.
COPY README.md alembic.ini ./
COPY config ./config
COPY migrations ./migrations
COPY scripts ./scripts
COPY src ./src
RUN uv sync --frozen --extra llm --no-dev

# Runtime stage: what ships and what Trivy scans.
FROM python:3.12-slim@sha256:6c4dd321d176d61ea848dc8c73a4f7dbae8f70e0ee48bb411ea2f045b599fa8e

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:${PATH}" \
    DATA_DIR=/app/data \
    RAW_PDF_DIR=/app/data/raw \
    PROCESSED_DIR=/app/data/processed \
    WHITEPAPER_TEMPLATE_PATH=/app/data/templates/cra_white_paper_template.docx

RUN apt-get update \
    # The base digest is pinned, so Debian security updates only reach the
    # image through an explicit upgrade; the Trivy gate fails on any FIXED
    # HIGH/CRITICAL CVE, which is exactly the set this keeps at zero.
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Run as an unprivileged user (defense in depth: a compromised process cannot
# write outside its own tree or escalate via root). Fixed uid/gid 1001 so the
# identity is stable across rebuilds. Everything the app writes lives under
# /app (DATA_DIR=/app/data and its children; the entrypoint runs `mkdir -p`
# there and `regwatch init-db` as this user), so /app and /app/data are
# created and handed over HERE, non-recursively, before anything is copied
# in; the copies below carry their ownership via --chown. On a compose
# bind-mount (./data:/app/data) the host owns the mount, so the developer's
# dir must be writable by uid 1001 there. Pre-creating /app/data makes the
# writable root explicit even on a fresh (ephemeral) Fly disk.
RUN groupadd --system --gid 1001 regwatch \
    && useradd --system --uid 1001 --gid 1001 --home-dir /app --no-create-home regwatch \
    && mkdir -p "$DATA_DIR" \
    && chown regwatch:regwatch /app "$DATA_DIR"

WORKDIR /app

COPY --from=python-build --chown=regwatch:regwatch /app/.venv ./.venv
COPY --chown=regwatch:regwatch README.md alembic.ini ./
COPY --chown=regwatch:regwatch config ./config
COPY --chown=regwatch:regwatch migrations ./migrations
COPY --chown=regwatch:regwatch scripts ./scripts
COPY --chown=regwatch:regwatch src ./src
COPY --chmod=755 docker/entrypoint.sh /usr/local/bin/regwatch-entrypoint

# Static proxy binary. Ships inert: the phase-3 "proxy" process group
# (docs/GO_PROXY_ROLLOUT.md) will exec it through the entrypoint; no fly.toml
# group runs it today. Copied after the python layers on purpose: a Go-only
# change must not invalidate the .venv layer above.
COPY --from=proxy-build /usr/local/bin/regwatch-proxy /usr/local/bin/regwatch-proxy

# The CRA White Paper Word template is gitignored (internal artifact) and is
# deliberately NOT baked into the image. WHITEPAPER_TEMPLATE_PATH (set above)
# defaults to a path under the mounted /app/data volume, so an operator drops
# the official .docx at data/templates/cra_white_paper_template.docx to enable
# real-template fill. Absent it, POST /whitepaper/runs/{id}/docx still returns a
# structurally-equivalent document stamped "(generated without the official CRA
# template file)". See docs/DEPLOY.md and src/regwatch/whitepaper/docx_writer.py.
EXPOSE 8000

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
