"""Strategy execution log service — in-memory buffer + persistent SQLite storage with 90-day retention."""
import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..config import now_beijing
from ..database import async_session, db_session, run_with_sqlite_retry

logger = logging.getLogger(__name__)

MAX_ENTRIES = 200
LOG_RETENTION_DAYS = 90
LOG_FLUSH_INTERVAL = 60  # seconds


@dataclass
class LogEntry:
    time: str
    level: str  # 'info', 'warning', 'error', 'success'
    message: str


class StrategyLogService:
    def __init__(self):
        self._buffers: dict[int, list[LogEntry]] = defaultdict(list)
        self._pending: list[dict] = []
        self._flush_task: asyncio.Task | None = None

    async def start_persistence(self):
        """Start periodic flush task for persistent log storage."""
        self._flush_task = asyncio.create_task(self._flush_loop())
        # Initialize log table
        async with async_session() as s:
            conn = await s.connection()
            await conn.run_sync(
                lambda c: c.exec_driver_sql(
                    """CREATE TABLE IF NOT EXISTS strategy_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        strategy_id INTEGER NOT NULL,
                        timestamp DATETIME NOT NULL,
                        level TEXT NOT NULL,
                        message TEXT NOT NULL
                    )"""
                )
            )
            await s.commit()

    async def stop_persistence(self):
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        await self._flush_now()

    async def _flush_loop(self):
        while True:
            try:
                await asyncio.sleep(LOG_FLUSH_INTERVAL)
                await self._flush_now()
                await self._cleanup_old()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("Log flush error: %s", e)

    async def _flush_now(self):
        if not self._pending:
            return
        batch = self._pending[:]
        self._pending = []

        async def _write():
            async with db_session() as s:
                conn = await s.connection()
                for entry in batch:
                    await conn.run_sync(
                        lambda c, e=entry: c.exec_driver_sql(
                            "INSERT INTO strategy_logs (strategy_id, timestamp, level, message) VALUES (?, ?, ?, ?)",
                            (e["strategy_id"], e["timestamp"], e["level"], e["message"]),
                        )
                    )
                await s.commit()

        try:
            await run_with_sqlite_retry(_write)
        except Exception as e:
            logger.debug("Log flush to DB failed: %s", e)
            self._pending = batch + self._pending

    async def _cleanup_old(self):
        cutoff = now_beijing() - timedelta(days=LOG_RETENTION_DAYS)

        async def _run():
            async with db_session() as s:
                conn = await s.connection()
                await conn.run_sync(
                    lambda c: c.exec_driver_sql(
                        "DELETE FROM strategy_logs WHERE timestamp < ?",
                        (cutoff.isoformat(),),
                    )
                )
                await s.commit()

        try:
            await run_with_sqlite_retry(_run)
        except Exception as e:
            logger.debug("Log cleanup failed: %s", e)

    def add(self, strategy_id: int, level: str, message: str):
        ts = now_beijing()
        entry = LogEntry(
            time=ts.strftime("%H:%M:%S"),
            level=level,
            message=message,
        )
        buf = self._buffers[strategy_id]
        buf.append(entry)
        if len(buf) > MAX_ENTRIES:
            self._buffers[strategy_id] = buf[-MAX_ENTRIES:]

        self._pending.append({
            "strategy_id": strategy_id,
            "timestamp": ts.isoformat(),
            "level": level,
            "message": message,
        })

    def info(self, strategy_id: int, msg: str):
        self.add(strategy_id, "info", msg)

    def warning(self, strategy_id: int, msg: str):
        self.add(strategy_id, "warning", msg)

    def error(self, strategy_id: int, msg: str):
        self.add(strategy_id, "error", msg)

    def success(self, strategy_id: int, msg: str):
        self.add(strategy_id, "success", msg)

    def get(self, strategy_id: int, limit: int = 100) -> list[dict]:
        buf = self._buffers.get(strategy_id, [])
        return [
            {"time": e.time, "level": e.level, "message": e.message}
            for e in buf[-limit:][::-1]
        ]

    def clear(self, strategy_id: int):
        self._buffers.pop(strategy_id, None)

    async def purge_strategy(self, strategy_id: int) -> None:
        """删除策略时清理内存缓冲、待落库队列与 SQLite 持久化日志。"""
        self._buffers.pop(strategy_id, None)
        self._pending = [e for e in self._pending if e.get("strategy_id") != strategy_id]

        async def _delete_rows():
            async with db_session() as s:
                conn = await s.connection()
                await conn.run_sync(
                    lambda c, sid=strategy_id: c.exec_driver_sql(
                        "DELETE FROM strategy_logs WHERE strategy_id = ?",
                        (sid,),
                    )
                )
                await s.commit()

        try:
            await run_with_sqlite_retry(_delete_rows)
        except Exception as e:
            logger.debug("purge_strategy logs strategy=%d: %s", strategy_id, e)


# Singleton
strategy_log_service = StrategyLogService()
