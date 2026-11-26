"""Alembic environment for the enterprise (ent_*) tables."""

import os

from alembic import context
from sqlalchemy import engine_from_config, pool

from enterprise import models  # noqa: F401  (register tables)
from enterprise.db import Base, normalize_url

VERSION_TABLE = "ent_alembic_version"

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    url = (
        context.get_x_argument(as_dictionary=True).get("url")
        or os.environ.get("ENTERPRISE_DATABASE_URL")
        or config.get_main_option("sqlalchemy.url")
    )
    if not url:
        raise RuntimeError("Set ENTERPRISE_DATABASE_URL or pass -x url=...")
    return normalize_url(url)


def include_object(obj, name, type_, reflected, compare_to):
    # Only manage enterprise tables; never touch the consumer/dealer schema.
    if type_ == "table":
        return name.startswith("ent_")
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        version_table=VERSION_TABLE,
        include_object=include_object,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = config.attributes.get("connection")
    if connectable is None:
        engine = engine_from_config(
            {"sqlalchemy.url": _url()}, prefix="sqlalchemy.", poolclass=pool.NullPool
        )
        with engine.connect() as connection:
            _run(connection)
    else:
        _run(connectable)


def _run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table=VERSION_TABLE,
        include_object=include_object,
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
