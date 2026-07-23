"""immutable embedding profiles and additive parallel chunk vectors

Adds no profile rows, performs no backfill, builds no HNSW index, and does not
touch the active ``chunk.embedding`` column.  Candidate embeddings live in a
profile-keyed table so multiple vector spaces can coexist during evaluation
without ever participating in one query.

The embedding column has no dimension typmod.  A trigger checks each row
against its immutable profile dimension; operators later create one partial
expression HNSW index per chosen profile.  Dimensions through 2,000 cast to
``vector(d)``.  Native 2,560-dimension profiles retain their full-precision
value here and use a ``halfvec(2560)`` expression index plus full-precision
reranking.

Revision ID: 0015_embedding_profiles
Revises: 0014_psg_version_unique_hash
Create Date: 2026-07-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "0015_embedding_profiles"
down_revision: str | None = "0014_psg_version_unique_hash"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _prepare_vector_and_legacy_chunk(bind: sa.engine.Connection) -> str:
    """Make the migration independently replayable on an Alembic-only DB.

    Historically ``chunk`` and pgvector were bootstrapped by ``init_db()``,
    outside Alembic.  A release-command migration therefore can legitimately
    reach revision 0014 without either object.  Install the extension and
    create the *unchanged* legacy chunk shape only when absent; an existing
    active chunk table is never altered.
    """
    vector_schema = bind.execute(
        sa.text(
            "SELECT n.nspname FROM pg_extension e "
            "JOIN pg_namespace n ON n.oid = e.extnamespace "
            "WHERE e.extname = 'vector'"
        )
    ).scalar()
    if vector_schema is None:
        has_extensions_schema = (
            bind.execute(
                sa.text("SELECT 1 FROM pg_namespace WHERE nspname = 'extensions'")
            ).scalar()
            is not None
        )
        if has_extensions_schema:
            bind.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions"))
        else:
            bind.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
        vector_schema = bind.execute(
            sa.text(
                "SELECT n.nspname FROM pg_extension e "
                "JOIN pg_namespace n ON n.oid = e.extnamespace "
                "WHERE e.extname = 'vector'"
            )
        ).scalar()
    if vector_schema not in {"public", "extensions"}:
        raise RuntimeError(
            "pgvector must be installed in the public or extensions schema; "
            f"found {vector_schema!r}"
        )

    # pgvector's SQLAlchemy type renders as unqualified VECTOR. Include the
    # detected extension schema for the profile table DDL below.
    bind.execute(
        sa.text("SELECT set_config('search_path', :path, true)"),
        {"path": f'public,"{vector_schema}"'},
    )

    if bind.execute(sa.text("SELECT to_regclass('public.chunk')")).scalar() is None:
        qualified_vector = f'"{vector_schema}".vector'
        qualified_cosine_ops = f'"{vector_schema}".vector_cosine_ops'
        bind.execute(sa.text(f"""
                CREATE TABLE public.chunk (
                    id TEXT PRIMARY KEY,
                    doc_id INTEGER,
                    version_id INTEGER,
                    page INTEGER,
                    section_path TEXT,
                    normalized_name TEXT,
                    dosage_form TEXT,
                    route TEXT,
                    source_url TEXT,
                    psg_type TEXT,
                    appl_no TEXT,
                    short_name TEXT,
                    text TEXT,
                    embedding {qualified_vector}(1536)
                )
                """))
        bind.execute(
            sa.text(
                "CREATE INDEX ix_chunk_embedding_hnsw ON public.chunk "
                f"USING hnsw (embedding {qualified_cosine_ops}) "
                "WITH (m = 16, ef_construction = 64)"
            )
        )
        for index_name, column_name in (
            ("ix_chunk_normalized_name", "normalized_name"),
            ("ix_chunk_doc_id", "doc_id"),
            ("ix_chunk_version_id", "version_id"),
            ("ix_chunk_appl_no", "appl_no"),
        ):
            bind.execute(sa.text(f'CREATE INDEX "{index_name}" ON public.chunk ("{column_name}")'))
    return str(vector_schema)


def embedding_profile_support_sql(vector_schema: str) -> tuple[str, ...]:
    """Idempotent trigger DDL shared with the fresh-Postgres bootstrap."""
    if vector_schema not in {"public", "extensions"}:
        raise ValueError(f"unsupported pgvector extension schema: {vector_schema!r}")
    vector_dims = f'"{vector_schema}".vector_dims'
    return (
        """
        CREATE OR REPLACE FUNCTION public.reject_embedding_profile_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION
                'embedding_profile rows are immutable; register a new profile instead'
                USING ERRCODE = '55000';
            RETURN NULL;
        END;
        $$
        """,
        f"""
        CREATE OR REPLACE FUNCTION public.validate_chunk_embedding_profile()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            expected_dimension integer;
        BEGIN
            SELECT dimension INTO expected_dimension
            FROM public.embedding_profile
            WHERE profile_id = NEW.profile_id;
            IF expected_dimension IS NULL THEN
                RAISE EXCEPTION 'unknown embedding profile %', NEW.profile_id
                    USING ERRCODE = '23503';
            END IF;
            IF {vector_dims}(NEW.embedding) <> expected_dimension THEN
                RAISE EXCEPTION
                    'embedding has % dimensions; profile % requires %',
                    {vector_dims}(NEW.embedding), NEW.profile_id, expected_dimension
                    USING ERRCODE = '22023';
            END IF;
            RETURN NEW;
        END;
        $$
        """,
        """
        CREATE OR REPLACE FUNCTION public.invalidate_chunk_embeddings_on_text_change()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            DELETE FROM public.chunk_embedding WHERE chunk_id = NEW.id;
            RETURN NEW;
        END;
        $$
        """,
        "DROP TRIGGER IF EXISTS embedding_profile_immutable ON public.embedding_profile",
        """
        CREATE TRIGGER embedding_profile_immutable
        BEFORE UPDATE OR DELETE ON public.embedding_profile
        FOR EACH ROW EXECUTE FUNCTION public.reject_embedding_profile_mutation()
        """,
        "DROP TRIGGER IF EXISTS chunk_embedding_profile_dimension ON public.chunk_embedding",
        """
        CREATE TRIGGER chunk_embedding_profile_dimension
        BEFORE INSERT OR UPDATE ON public.chunk_embedding
        FOR EACH ROW EXECUTE FUNCTION public.validate_chunk_embedding_profile()
        """,
        "DROP TRIGGER IF EXISTS chunk_text_invalidates_profile_embeddings ON public.chunk",
        """
        CREATE TRIGGER chunk_text_invalidates_profile_embeddings
        AFTER UPDATE OF text ON public.chunk
        FOR EACH ROW
        WHEN (OLD.text IS DISTINCT FROM NEW.text)
        EXECUTE FUNCTION public.invalidate_chunk_embeddings_on_text_change()
        """,
    )


def upgrade() -> None:
    bind = op.get_bind()
    # The application datastore is Postgres-only.  A small SQLite migration
    # harness still replays the chain to verify older portable table shapes;
    # pgvector types, extension catalogs, triggers, and HNSW are unavailable
    # there, so this additive profile migration is intentionally a no-op.
    if bind.dialect.name != "postgresql":
        return
    vector_schema = _prepare_vector_and_legacy_chunk(bind)
    op.create_table(
        "embedding_profile",
        sa.Column("profile_id", sa.String(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("revision", sa.String(), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column("dtype", sa.String(), nullable=False),
        sa.Column("normalization", sa.String(), nullable=False),
        sa.Column("query_instruction_version", sa.String(), nullable=False),
        sa.Column("preprocessing_version", sa.String(), nullable=False),
        sa.Column("chunking_version", sa.String(), nullable=False),
        sa.Column("serving_runtime_version", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "profile_id ~ '^ep_[0-9a-f]{32}$'",
            name="ck_embedding_profile_id",
        ),
        sa.CheckConstraint(
            "fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_embedding_profile_fingerprint",
        ),
        sa.CheckConstraint(
            "dimension BETWEEN 1 AND 16000",
            name="ck_embedding_profile_dimension",
        ),
        sa.PrimaryKeyConstraint("profile_id"),
        sa.UniqueConstraint("fingerprint", name="uq_embedding_profile_fingerprint"),
    )
    op.create_table(
        "chunk_embedding",
        sa.Column("profile_id", sa.String(), nullable=False),
        sa.Column("chunk_id", sa.String(), nullable=False),
        sa.Column("embedding", Vector(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "embedded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_chunk_embedding_content_hash",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["embedding_profile.profile_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["chunk_id"], ["chunk.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("profile_id", "chunk_id"),
    )
    op.create_index(
        "ix_chunk_embedding_chunk_id",
        "chunk_embedding",
        ["chunk_id"],
    )
    for statement in embedding_profile_support_sql(vector_schema):
        bind.execute(sa.text(statement))


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP TRIGGER IF EXISTS chunk_text_invalidates_profile_embeddings ON chunk")
    op.execute("DROP TRIGGER IF EXISTS chunk_embedding_profile_dimension ON chunk_embedding")
    op.execute("DROP TRIGGER IF EXISTS embedding_profile_immutable ON embedding_profile")
    op.execute("DROP FUNCTION IF EXISTS public.invalidate_chunk_embeddings_on_text_change()")
    op.execute("DROP FUNCTION IF EXISTS public.validate_chunk_embedding_profile()")
    op.execute("DROP FUNCTION IF EXISTS public.reject_embedding_profile_mutation()")
    op.drop_index("ix_chunk_embedding_chunk_id", table_name="chunk_embedding")
    op.drop_table("chunk_embedding")
    op.drop_table("embedding_profile")
