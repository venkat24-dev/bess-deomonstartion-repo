from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.schema import CreateSchema

from bess.db_connection import database_url, get_engine
from bess.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata
TARGET_SCHEMA = "bess"


def include_name(name, type_, parent_names):
    if type_ == "schema":
        return name == TARGET_SCHEMA
    return True


def run_migrations_offline() -> None:
    url = database_url()
    context.configure(
        url=url.render_as_string(hide_password=False),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
        include_name=include_name,
        version_table_schema=TARGET_SCHEMA,
        compare_type=True,
    )
    context.execute(CreateSchema(TARGET_SCHEMA, if_not_exists=True))
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = get_engine()
    with engine.connect() as connection:
        connection.execute(CreateSchema(TARGET_SCHEMA, if_not_exists=True))
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=True,
            include_name=include_name,
            version_table_schema=TARGET_SCHEMA,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
