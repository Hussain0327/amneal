# syntax=docker/dockerfile:1

ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim
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
    API_HOST=0.0.0.0 \
    API_PORT=8000

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN if [ "$INSTALL_LOCAL_EMBEDDINGS" = "true" ]; then \
        uv sync --frozen --extra llm --extra local-embeddings --no-dev --no-install-project; \
    else \
        uv sync --frozen --extra llm --no-dev --no-install-project; \
    fi

COPY README.md alembic.ini ./
COPY config ./config
COPY migrations ./migrations
COPY scripts ./scripts
COPY src ./src
COPY docker/entrypoint.sh /usr/local/bin/regwatch-entrypoint

RUN chmod +x /usr/local/bin/regwatch-entrypoint \
    && if [ "$INSTALL_LOCAL_EMBEDDINGS" = "true" ]; then \
        uv sync --frozen --extra llm --extra local-embeddings --no-dev; \
    else \
        uv sync --frozen --extra llm --no-dev; \
    fi

EXPOSE 8000

ENTRYPOINT ["regwatch-entrypoint"]
CMD ["uvicorn", "regwatch.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
