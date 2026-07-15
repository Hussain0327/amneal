# syntax=docker/dockerfile:1

# Build stage for the Go reverse proxy (fly.toml [processes] group "proxy",
# docs/GO_PROXY_ROLLOUT.md). CGO_ENABLED=0 over a stdlib-only module yields a
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
# go.mod before sources so the dependency layer (a no-op while the module is
# stdlib-only) stays cached across source edits once deps arrive.
COPY go/go.mod ./
RUN go mod download
COPY go/cmd ./cmd
COPY go/internal ./internal
RUN go build -trimpath -ldflags "-s -w" -o /usr/local/bin/regwatch-proxy ./cmd/proxy

# Two build flavors, gated by INSTALL_LOCAL_EMBEDDINGS:
#   * slim (default, production): no torch/sentence-transformers. Run with
#     EMBEDDING_PROVIDER=openai + OPENAI_API_KEY (+ DATABASE_URL for
#     Postgres/pgvector on Supabase) — the openai SDK ships via the `llm`
#     extra, so the slim image is fully embedding-capable. See docs/DEPLOY.md.
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
# Dagster ships only when asked for (compose sets true for its dagster-*
# services). Prod structurally cannot run it (the CMD is uvicorn-only, Fly has
# no dagster process, and the GitHub Actions cron is the sole scheduler), so
# baking the orchestration closure into the default image was pure dead weight
# (size, build time, and Trivy/pip-audit CVE surface for unreachable code).
ARG INSTALL_ORCHESTRATION=false

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:${PATH}" \
    DATA_DIR=/app/data \
    CHROMA_DIR=/app/data/chroma \
    SQLITE_PATH=/app/data/regwatch.db \
    RAW_PDF_DIR=/app/data/raw \
    PROCESSED_DIR=/app/data/processed \
    EMBEDDING_PROVIDER=echo \
    WHITEPAPER_TEMPLATE_PATH=/app/data/templates/cra_white_paper_template.docx \
    DAGSTER_CONFIG_DIR=/app/dagster_config \
    DAGSTER_HOME=/app/data/dagster/home \
    API_HOST=0.0.0.0 \
    API_PORT=8000

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
    && if [ "$INSTALL_ORCHESTRATION" = "true" ]; then EXTRAS="$EXTRAS --extra orchestration"; fi \
    && uv sync --frozen $EXTRAS --no-dev --no-install-project

COPY README.md alembic.ini ./
COPY config ./config
COPY migrations ./migrations
COPY scripts ./scripts
COPY src ./src
COPY docker/dagster $DAGSTER_CONFIG_DIR
COPY docker/entrypoint.sh /usr/local/bin/regwatch-entrypoint

RUN chmod +x /usr/local/bin/regwatch-entrypoint \
    && EXTRAS="--extra llm" \
    && if [ "$INSTALL_LOCAL_EMBEDDINGS" = "true" ]; then EXTRAS="$EXTRAS --extra local-embeddings"; fi \
    && if [ "$INSTALL_ORCHESTRATION" = "true" ]; then EXTRAS="$EXTRAS --extra orchestration"; fi \
    && uv sync --frozen $EXTRAS --no-dev

# Static proxy binary (fly.toml [processes] group "proxy" execs it through the
# entrypoint; inert on app machines). Copied after the python layers on
# purpose: a Go-only change must not invalidate the uv dependency cache above.
COPY --from=proxy-build /usr/local/bin/regwatch-proxy /usr/local/bin/regwatch-proxy

# The CRA White Paper Word template is gitignored (internal artifact) and is
# deliberately NOT baked into the image. WHITEPAPER_TEMPLATE_PATH (set above)
# defaults to a path under the mounted /app/data volume, so an operator drops
# the official .docx at data/templates/cra_white_paper_template.docx to enable
# real-template fill. Absent it, POST /whitepaper/docx still returns a
# structurally-equivalent document stamped "(generated without the official CRA
# template file)" — see docs/DEPLOY.md and src/regwatch/whitepaper/docx_writer.py.
EXPOSE 8000 4000

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
# On Fly this CMD never runs: fly.toml [processes].app supersedes it and binds
# --host :: instead, because the Go proxy reaches uvicorn over IPv6-only 6PN.
# 0.0.0.0 stays here for local docker/compose runs on IPv4-only bridges.
CMD ["uvicorn", "regwatch.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
