from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from app.config import get_settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def run_migrations_online() -> None:
    url = get_settings().analytics_owner_database_url
    if not url:
        raise RuntimeError("ANALYTICS_OWNER_DATABASE_URL is required to migrate the analytics database")
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=None, version_table="alembic_version_analytics")
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
