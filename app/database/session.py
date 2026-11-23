from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.database.models import Base


def create_database(url: str) -> tuple[AsyncEngine, async_sessionmaker]:
    if url.startswith("sqlite") and "///" in url:
        Path(url.split("///", 1)[1]).parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(url)
    if url.startswith("sqlite"):

        @event.listens_for(engine.sync_engine, "connect")
        def pragmas(connection, _record):  # type: ignore[no-untyped-def]
            cursor = connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def initialize_database(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        # Additive SQLite upgrade for already deployed installations. create_all()
        # does not add columns to existing tables; preserve DB and sessions.
        if engine.url.get_backend_name() == "sqlite":
            upgrades = {
                "campaign_targets": {
                    "interval_seconds": "INTEGER",
                    "next_run_at": "DATETIME",
                },
                "campaign_messages": {
                    "forward_chat_ref": "VARCHAR(200)",
                    "forward_message_id": "INTEGER",
                },
            }
            for table, columns in upgrades.items():
                existing = {
                    row[1] for row in (
                        await connection.exec_driver_sql(f"PRAGMA table_info({table})")
                    ).all()
                }
                for name, sql_type in columns.items():
                    if name not in existing:
                        await connection.exec_driver_sql(
                            f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"
                        )
