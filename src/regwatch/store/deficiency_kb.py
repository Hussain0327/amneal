"""Deficiency precedent knowledge base: one table, embedded once, searched read-only.

Upstream DefPredict retrieved precedents from a local FAISS index built over
BAAI/bge-m3 vectors. That index was process-local and its embedding space is
incompatible with everything regwatch runs, so the vendored pipeline searches
this Postgres table instead. Vectors are produced by the Databricks Qwen3
endpoint (the in-tenant inference plane), which keeps uploaded-document text
inside the D1 boundary -- OpenAI embeddings are not an option here even though
the legacy chunk space still uses them, because precedent queries are derived
from customer submission content.

The dimension is fixed at 1024 (the ``regwatch-embed`` Qwen3-0.6B serving
endpoint). This table is NOT part of the chunk embedding-profile machinery on
purpose: profile backfill counts scan ``chunk`` rows, and adding a second
corpus there while the profile flip is in flight would join every profile's
backfill denominator and block activation.

Expected scale is ~500 rows (the ANDA deficiency roadmap spreadsheet), so
search is a plain exact scan -- no vector index, nothing to toggle. Rows are
write-once via ``add_entries``, the loader seam. That seam has no caller in
``src/``, ``tests/`` or ``scripts/`` today, so the table stays empty and
``precedents.py`` short-circuits at ``kb_count() == 0`` -- a tracked gap, not
an oversight (docs/ROADMAP.md, "the precedent KB gap"). There is no update
path.

Registered in ``SQLModel.metadata`` (Core ``Table``, same as
``embedding_profiles``) so the fresh-Postgres ``create_all`` + stamp-head
bootstrap stays converged with migration 0019 replay on existing databases.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy import Table
from sqlmodel import SQLModel

from regwatch.common.logging import get_logger

log = get_logger(__name__)

# Dimension of workspace.default.regwatch-embed (Qwen3-Embedding-0.6B).
KB_EMBEDDING_DIM = 1024

deficiency_kb_table = Table(
    "deficiency_kb",
    SQLModel.metadata,
    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
    sa.Column("anda_number", sa.String(), nullable=False, server_default=""),
    sa.Column("product_name", sa.String(), nullable=False, server_default=""),
    sa.Column("deficiency_text", sa.Text(), nullable=False),
    sa.Column("deficiency_type", sa.String(), nullable=True),
    sa.Column("embedding", Vector(KB_EMBEDDING_DIM), nullable=False),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    ),
    sa.CheckConstraint(
        "length(deficiency_text) > 0",
        name="ck_deficiency_kb_text_nonempty",
    ),
)


@dataclass(frozen=True)
class KbMatch:
    anda_number: str
    product_name: str
    deficiency_text: str
    deficiency_type: str | None
    score: float  # 1 - cosine_distance/2, in [0, 1] -- same convention as chunk search


def kb_count() -> int:
    from regwatch.store.db import get_engine

    with get_engine().connect() as conn:
        return int(conn.execute(sa.text("SELECT count(*) FROM deficiency_kb")).scalar_one())


def search_similar(query_vec: list[float], *, top_k: int) -> list[KbMatch]:
    """Exact cosine scan over the KB. Raises on dimension mismatch (clear error
    beats a silent empty result); callers in the detection path treat any
    exception as best-effort-absent precedents."""
    if len(query_vec) != KB_EMBEDDING_DIM:
        raise ValueError(
            f"query vector has {len(query_vec)} dims; deficiency_kb stores "
            f"vector({KB_EMBEDDING_DIM})"
        )
    if top_k <= 0:
        return []
    from regwatch.store.db import get_engine

    vec_literal = "[" + ",".join(f"{v:.8f}" for v in query_vec) + "]"
    sql = sa.text("""
        SELECT anda_number, product_name, deficiency_text, deficiency_type,
               1 - (embedding <=> CAST(:qvec AS vector)) / 2 AS score
        FROM deficiency_kb
        ORDER BY embedding <=> CAST(:qvec AS vector)
        LIMIT :top_k
        """)
    with get_engine().connect() as conn:
        rows = conn.execute(sql, {"qvec": vec_literal, "top_k": top_k}).mappings().all()
    return [
        KbMatch(
            anda_number=r["anda_number"],
            product_name=r["product_name"],
            deficiency_text=r["deficiency_text"],
            deficiency_type=r["deficiency_type"],
            score=float(r["score"]),
        )
        for r in rows
    ]


def add_entries(
    entries: list[tuple[str, str, str, str | None, list[float]]],
) -> int:
    """Insert (anda_number, product_name, deficiency_text, deficiency_type,
    embedding) rows. Loader + test seam; validates dimensions up front so a
    bad batch fails whole, not half-inserted."""
    if not entries:
        return 0
    for _, _, text_value, _, vec in entries:
        if not text_value:
            raise ValueError("deficiency_text must be non-empty")
        if len(vec) != KB_EMBEDDING_DIM:
            raise ValueError(
                f"embedding has {len(vec)} dims; deficiency_kb stores vector({KB_EMBEDDING_DIM})"
            )
    from regwatch.store.db import get_engine

    rows = [
        {
            "anda_number": anda,
            "product_name": product,
            "deficiency_text": text_value,
            "deficiency_type": dtype,
            "embedding": vec,
        }
        for anda, product, text_value, dtype, vec in entries
    ]
    with get_engine().begin() as conn:
        conn.execute(deficiency_kb_table.insert(), rows)
    log.info("deficiency_kb_loaded", rows=len(rows))
    return len(rows)
