"""Alembic's entry: the database from the app's settings, the tables from `app.tables`."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import Env
from app.tables import metadata

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
config.set_main_option("sqlalchemy.url", Env().database_url)


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=metadata)
        with context.begin_transaction():
            # Fail fast instead of queueing the live app's queries behind a migration.
            context.execute("SET LOCAL lock_timeout = '5s'")
            context.execute("SET LOCAL statement_timeout = '60s'")
            context.run_migrations()


if context.is_offline_mode():
    raise SystemExit("offline migrations are not set up: run against a database")
run_migrations_online()
