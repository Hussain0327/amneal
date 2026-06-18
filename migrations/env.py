"""Alembic environment for REGWATCH."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from config.settings import get_settings
from sqlalchemy import create_engine, engine_from_config, pool
from sqlalchemy.engine import make_url
from sqlmodel import SQLModel

from regwatch.store import models  # noqa: F401  (register SQLModel tables)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = SQLModel.metadata


def _database_url() -> str:
    """Resolve the migration target URL.

    Precedence: an explicitly configured sqlalchemy.url (set by store/db.py or
    a caller-supplied Config) > DATABASE_URL from settings (Postgres) > the
    SQLite file. Postgres never replays the SQLite-era migration history —
    store/db.py only ever stamps it — but `alembic stamp` still runs this env.
    """
    configured = config.get_main_option("sqlalchemy.url")
    if configured:
        return configured
    s = get_settings()
    if s.database_url:
        return s.database_url
    return f"sqlite:///{s.sqlite_path.as_posix()}"


def run_migrations_offline() -> None:
    url = _database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # Batch ops only make sense on SQLite (no transactional ALTER TABLE);
        # Postgres gets plain DDL.
        render_as_batch=url.startswith("sqlite"),
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = _database_url()
    # ConfigParser interpolation: escape literal '%' (e.g. percent-encoded
    # Postgres passwords) before storing the URL back into the config.
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))

    if make_url(url).drivername.startswith("postgresql"):
        # Remote Postgres (the Fly release_command, manual ops): enforce TLS like
        # the app engine (store/db.py::get_engine) and bound the lock wait so a
        # contended migration self-cancels instead of wedging the release machine.
        from regwatch.store.db import _enforce_sslmode, _migration_connect_args

        connectable = create_engine(
            _enforce_sslmode(url),
            poolclass=pool.NullPool,
            connect_args=_migration_connect_args(get_settings()),
        )
    else:
        connectable = engine_from_config(
            config.get_section(config.config_ini_section, {}),
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
        )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=connection.dialect.name == "sqlite",
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
