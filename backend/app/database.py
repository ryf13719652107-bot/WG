import asyncio
import contextvars
import logging
from contextlib import asynccontextmanager
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool
import os

from .config import settings

logger = logging.getLogger(__name__)

# 单账户 50–100 策略：限制同时占用 DB 的 tick 数；连接用 NullPool 按需创建，避免 QueuePool 排队超时
TICK_DB_CONCURRENCY = max(8, int(os.environ.get("TICK_DB_CONCURRENCY", "48")))
_TICK_DB_SEM = asyncio.Semaphore(TICK_DB_CONCURRENCY)

# 嵌套 db_session（tick 内再开 session）时避免重复占信号量导致自死锁
_db_session_depth: contextvars.ContextVar[int] = contextvars.ContextVar("_db_session_depth", default=0)

# Ensure data directory exists
db_path = settings.database_url.replace("sqlite+aiosqlite:///", "")
db_dir = os.path.dirname(db_path)
if db_dir and not os.path.exists(db_dir):
    os.makedirs(db_dir, exist_ok=True)

# SQLite + 高并发 tick：NullPool 每次短连接，配合 WAL；busy_timeout 在 connect_args
engine = create_async_engine(
    settings.database_url,
    echo=False,
    poolclass=NullPool,
    connect_args={
        "timeout": 60,
        "check_same_thread": False,
    },
)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class TickDbSession:
    """策略 tick 专用会话：仅在 execute/add/commit 时占库，commit/rollback 后立即释放。"""

    def __init__(self) -> None:
        self._session: Optional[AsyncSession] = None
        self._sem_held = False

    async def _open(self) -> AsyncSession:
        if self._session is not None:
            return self._session
        await _TICK_DB_SEM.acquire()
        self._sem_held = True
        self._session = async_session()
        # sessionmaker() 返回的 AsyncSession 需显式 close；异常时由 rollback/close 释放
        return self._session

    async def _close(self) -> None:
        if self._session is not None:
            try:
                await self._session.close()
            except Exception as e:
                logger.debug("TickDbSession close: %s", e)
            self._session = None
        if self._sem_held:
            _TICK_DB_SEM.release()
            self._sem_held = False

    async def reattach(
        self, strategy: Any = None, positions: Optional[list] = None,
    ) -> Any:
        """交易所 I/O 后重新绑定 ORM 对象，便于后续 commit。"""
        s = await self._open()
        if strategy is not None:
            strategy = await s.merge(strategy)
        if positions:
            for i, p in enumerate(positions):
                positions[i] = await s.merge(p)
        return strategy

    async def execute(self, *args, **kwargs):
        s = await self._open()
        return await s.execute(*args, **kwargs)

    async def get(self, *args, **kwargs):
        s = await self._open()
        return await s.get(*args, **kwargs)

    async def add(self, obj: Any) -> None:
        s = await self._open()
        s.add(obj)

    async def refresh(self, obj: Any) -> Any:
        """commit 后会话已关闭，须 merge 后再 refresh。"""
        s = await self._open()
        merged = await s.merge(obj)
        await s.refresh(merged)
        return merged

    async def commit(self) -> None:
        s = await self._open()
        await s.commit()
        await self._close()

    async def rollback(self) -> None:
        try:
            if self._session is not None:
                await self._session.rollback()
        finally:
            await self._close()

    async def close(self) -> None:
        await self._close()


@asynccontextmanager
async def db_session():
    """HTTP/对账等短事务；策略 tick 内嵌套时不再抢 TICK 信号量。"""
    depth = _db_session_depth.get()
    if depth > 0:
        async with async_session() as session:
            yield session
        return
    await _TICK_DB_SEM.acquire()
    try:
        async with async_session() as session:
            yield session
    finally:
        _TICK_DB_SEM.release()


@asynccontextmanager
async def db_session_nested_safe():
    """不占用 TICK 信号量的短会话（供 tick 执行过程中偶发查库）。"""
    async with async_session() as session:
        yield session


def tick_db_session() -> TickDbSession:
    return TickDbSession()


@asynccontextmanager
async def db_tick_context():
    """标记当前协程处于策略 tick 内，供 db_session() 嵌套判断。"""
    token = _db_session_depth.set(_db_session_depth.get() + 1)
    try:
        yield
    finally:
        _db_session_depth.reset(token)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with async_session() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        pragmas = [
            "PRAGMA journal_mode=WAL",
            "PRAGMA synchronous=NORMAL",
            "PRAGMA cache_size=-64000",
            "PRAGMA temp_store=MEMORY",
            "PRAGMA mmap_size=268435456",
            "PRAGMA wal_autocheckpoint=1000",
            "PRAGMA busy_timeout=60000",
        ]
        for sql in pragmas:
            try:
                await conn.run_sync(lambda c, s=sql: c.exec_driver_sql(s))
            except Exception:
                pass

        migrations = [
            "ALTER TABLE accounts ADD COLUMN exchange VARCHAR(20) DEFAULT 'binance'",
            "ALTER TABLE accounts ADD COLUMN okx_passphrase_encrypted TEXT",
            "ALTER TABLE accounts ADD COLUMN equity_stop_floor_u FLOAT DEFAULT 0",
            "ALTER TABLE accounts ADD COLUMN equity_baseline_u FLOAT",
            "ALTER TABLE accounts ADD COLUMN equity_baseline_at DATETIME",
            "ALTER TABLE accounts ADD COLUMN equity_stop_triggered BOOLEAN DEFAULT 0",
            "ALTER TABLE strategies ADD COLUMN tp_pct FLOAT DEFAULT 1.0",
            "ALTER TABLE strategies ADD COLUMN grid_drop_base_pct FLOAT DEFAULT 1.0",
            "ALTER TABLE strategies ADD COLUMN grid_interval_multiplier FLOAT DEFAULT 1.5",
            "ALTER TABLE strategies ADD COLUMN position_multiplier FLOAT DEFAULT 1.5",
            "ALTER TABLE strategies ADD COLUMN cumulative_loss_threshold_u FLOAT DEFAULT 0.0",
            "ALTER TABLE strategies ADD COLUMN stop_loss_close_pct FLOAT DEFAULT 100.0",
            "ALTER TABLE strategies ADD COLUMN reopen_after_close BOOLEAN DEFAULT 1",
            "ALTER TABLE strategies ADD COLUMN consecutive_failures INTEGER DEFAULT 0",
            "ALTER TABLE positions ADD COLUMN grid_level INTEGER DEFAULT 0",
            "ALTER TABLE positions ADD COLUMN grid_trigger_price NUMERIC(20,8)",
            "ALTER TABLE positions ADD COLUMN tp_limit_order_id VARCHAR(100)",
            "ALTER TABLE positions ADD COLUMN add_limit_order_id VARCHAR(100)",
            "ALTER TABLE positions ADD COLUMN sl_algo_order_id VARCHAR(100)",
            "ALTER TABLE positions ADD COLUMN take_profit_price NUMERIC(20,8)",
            "ALTER TABLE trades ADD COLUMN grid_level INTEGER DEFAULT 0",
        ]
        for sql in migrations:
            try:
                await conn.run_sync(lambda c, s=sql: c.exec_driver_sql(s))
            except Exception:
                pass

        legacy_drops = [
            ("strategies", "timeframe"),
            ("strategies", "margin_threshold"),
            ("strategies", "signal_source"),
            ("strategies", "wt_channel_length"),
            ("strategies", "wt_average_length"),
            ("strategies", "martingale_rsi_enabled"),
            ("strategies", "coin_pool_top_n"),
            ("strategies", "exclude_tradefi"),
            ("strategies", "name"),
            ("strategies", "leverage"),
        ]
        for table, column in legacy_drops:
            try:
                def _drop_col(c, t=table, col=column):
                    existing = [r[1] for r in c.execute(f"PRAGMA table_info({t})").fetchall()]
                    if col in existing:
                        c.exec_driver_sql(f"ALTER TABLE {t} DROP COLUMN {col}")
                await conn.run_sync(_drop_col)
            except Exception:
                pass

    logger.info(
        "SQLite ready (NullPool, WAL, tick_db_concurrency=%d)",
        TICK_DB_CONCURRENCY,
    )
