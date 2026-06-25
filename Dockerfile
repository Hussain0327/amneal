# syntax=docker/dockerfile:1

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
# build. Bump BOTH the digest and the trailing tag comment together; resolve the
# new digest with:  docker manifest inspect python:3.12-slim  (the index/list
# digest, not a per-arch child). Keep PYTHON_VERSION as documentation only.
ARG PYTHON_VERSION=3.12
FROM python:3.12-slim@sha256:6c4dd321d176d61ea848dc8c73a4f7dbae8f70e0ee48bb411ea2f045b599fa8e  # 3.12-slim
ARG INSTALL_LOCAL_EMBEDDINGS=false

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
RUN if [ "$INSTALL_LOCAL_EMBEDDINGS" = "true" ]; then \
        uv sync --frozen --extra llm --extra orchestration --extra local-embeddings --no-dev --no-install-project; \
    else \
        uv sync --frozen --extra llm --extra orchestration --no-dev --no-install-project; \
    fi

COPY README.md alembic.ini ./
COPY config ./config
COPY migrations ./migrations
COPY scripts ./scripts
COPY src ./src
COPY docker/dagster $DAGSTER_CONFIG_DIR
COPY docker/entrypoint.sh /usr/local/bin/regwatch-entrypoint

RUN chmod +x /usr/local/bin/regwatch-entrypoint \
    && if [ "$INSTALL_LOCAL_EMBEDDINGS" = "true" ]; then \
        uv sync --frozen --extra llm --extra orchestration --extra local-embeddings --no-dev; \
    else \
        uv sync --frozen --extra llm --extra orchestration --no-dev; \
    fi

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
CMD ["uvicorn", "regwatch.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
