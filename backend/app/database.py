import logging
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
import os

from .config import settings

logger = logging.getLogger(__name__)

# Ensure data directory exists
db_path = settings.database_url.replace("sqlite+aiosqlite:///", "")
db_dir = os.path.dirname(db_path)
if db_dir and not os.path.exists(db_dir):
    os.makedirs(db_dir, exist_ok=True)

engine = create_async_engine(settings.database_url, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with async_session() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_db():
    # Enable WAL mode for better concurrent performance
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # WAL mode — better concurrent read/write performance
        try:
            await conn.run_sync(lambda c: c.exec_driver_sql("PRAGMA journal_mode=WAL"))
        except Exception:
            pass
        try:
            await conn.run_sync(lambda c: c.exec_driver_sql("PRAGMA synchronous=NORMAL"))
        except Exception:
            pass
        try:
            await conn.run_sync(lambda c: c.exec_driver_sql("PRAGMA cache_size=-20000"))
        except Exception:
            pass

        migrations = [
            "ALTER TABLE accounts ADD COLUMN exchange VARCHAR(20) DEFAULT 'binance'",
            "ALTER TABLE accounts ADD COLUMN okx_passphrase_encrypted TEXT",
            # Grid strategy columns
            "ALTER TABLE strategies ADD COLUMN tp_pct FLOAT DEFAULT 1.0",
            "ALTER TABLE strategies ADD COLUMN grid_drop_base_pct FLOAT DEFAULT 1.0",
            "ALTER TABLE strategies ADD COLUMN grid_interval_multiplier FLOAT DEFAULT 1.5",
            "ALTER TABLE strategies ADD COLUMN position_multiplier FLOAT DEFAULT 1.5",
            "ALTER TABLE strategies ADD COLUMN cumulative_loss_threshold_u FLOAT DEFAULT 0.0",
            "ALTER TABLE strategies ADD COLUMN reopen_after_close BOOLEAN DEFAULT 1",
            # Position grid tracking
            "ALTER TABLE positions ADD COLUMN grid_level INTEGER DEFAULT 0",
            "ALTER TABLE positions ADD COLUMN grid_trigger_price FLOAT",
            "ALTER TABLE positions ADD COLUMN tp_limit_order_id VARCHAR(100)",
            "ALTER TABLE positions ADD COLUMN add_limit_order_id VARCHAR(100)",
            # Trade grid tracking
            "ALTER TABLE trades ADD COLUMN grid_level INTEGER DEFAULT 0",
        ]
        for sql in migrations:
            try:
                await conn.run_sync(lambda c, s=sql: c.exec_driver_sql(s))
            except Exception:
                pass  # column already exists
