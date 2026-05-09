"""Strategy scheduler: lifecycle management and grid strategy execution loop."""
import asyncio
import logging
from typing import Optional

from sqlalchemy import select
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from ..database import async_session
from ..models.strategy import Strategy
from ..models.account import Account
from ..models.bot_config import BotConfig
from ..config import now_beijing, BEIJING_TZ
from .exchange_base import BaseExchangeService
from .exchange_factory import get_exchange_service, get_public_exchange
from .log_service import strategy_log_service
from .grid_engine import GridStrategyEngine
from .grid_executor import GridExecutor
from .price_stream import price_stream

logger = logging.getLogger(__name__)

_STRATEGY_TICK_SECONDS = 30  # fixed internal execution interval
_STRATEGY_SEMAPHORE = asyncio.Semaphore(20)


class StrategyScheduler:
    def __init__(self):
        self._aps = AsyncIOScheduler(timezone=BEIJING_TZ)
        self._strategy_jobs: dict[int, str] = {}
        self._exchange_services: dict[int, BaseExchangeService] = {}
        self._engines: dict[int, GridStrategyEngine] = {}
        self._executors: dict[int, GridExecutor] = {}
        self._prices_task: Optional[asyncio.Task] = None

    @property
    def scheduler(self) -> AsyncIOScheduler:
        return self._aps

    async def resume_running_strategies(self):
        """Restart: re-register jobs for strategies that were running, rebuild exchange connections."""
        async with async_session() as session:
            result = await session.execute(
                select(Strategy).where(Strategy.status == "running").order_by(Strategy.id)
            )
            rows = list(result.scalars().all())
        for s in rows:
            self._register_strategy_job(s.id)
            # Pre-warm exchange service
            exchange = await get_exchange_service(s.account_id)
            if exchange:
                self._exchange_services[s.id] = exchange
            self._engines[s.id] = GridStrategyEngine(s)
            self._executors[s.id] = GridExecutor(self._engines[s.id])
            strategy_log_service.info(s.id, "后端已重启：已恢复调度任务")
            logger.info("Resumed scheduler for strategy %d (%s)", s.id, s.name)

        # Subscribe to price feeds for all resumed strategies' symbols
        await self._start_price_stream()

    async def _start_price_stream(self):
        """Subscribe price stream to all running strategy symbols."""
        symbols_by_exchange: dict[str, set[str]] = {}
        async with async_session() as session:
            result = await session.execute(
                select(Strategy).where(Strategy.status == "running")
            )
            for s in result.scalars().all():
                account = await session.get(Account, s.account_id)
                if not account:
                    continue
                ex_type = account.exchange or "binance"
                if ex_type not in symbols_by_exchange:
                    symbols_by_exchange[ex_type] = set()
                symbols_by_exchange[ex_type].add(s.symbol)

        for ex_type, symbols in symbols_by_exchange.items():
            try:
                pub_ex = await get_public_exchange(ex_type)
                await price_stream.subscribe_exchange(ex_type, list(symbols), pub_ex)
                logger.info("Price stream started for %s: %d symbols", ex_type, len(symbols))
            except Exception as e:
                logger.error("Failed to start price stream for %s: %s", ex_type, e)

    def _register_strategy_job(self, strategy_id: int):
        job_id = f"strategy_{strategy_id}"
        if self._aps.get_job(job_id):
            self._aps.remove_job(job_id)
        self._aps.add_job(
            self._execute_strategy,
            "interval",
            seconds=_STRATEGY_TICK_SECONDS,
            id=job_id,
            args=[strategy_id],
            next_run_time=None,  # start immediately
        )
        self._strategy_jobs[strategy_id] = job_id

    def start(self):
        if not self._aps.running:
            self._aps.start()

    def stop(self):
        if self._aps.running:
            self._aps.shutdown(wait=False)

    async def add_strategy(self, strategy_id: int, session=None):
        if session is None:
            async with async_session() as s:
                return await self._add_strategy_impl(strategy_id, s)
        return await self._add_strategy_impl(strategy_id, session)

    async def _add_strategy_impl(self, strategy_id: int, session):
        strategy = await session.get(Strategy, strategy_id)
        if not strategy:
            logger.warning("Strategy %d not found", strategy_id)
            return False

        self._register_strategy_job(strategy_id)
        self._engines[strategy_id] = GridStrategyEngine(strategy)
        self._executors[strategy_id] = GridExecutor(self._engines[strategy_id])

        # Ensure exchange service is cached
        exchange = await get_exchange_service(strategy.account_id)
        if exchange:
            self._exchange_services[strategy_id] = exchange

        # Subscribe to price feed
        account = await session.get(Account, strategy.account_id)
        if account:
            ex_type = account.exchange or "binance"
            pub_ex = await get_public_exchange(ex_type)
            await price_stream.subscribe_exchange(ex_type, [strategy.symbol], pub_ex)

        strategy.status = "running"
        strategy.started_at = now_beijing()
        await session.commit()
        await session.refresh(strategy)
        logger.info("Strategy %d (%s) started", strategy_id, strategy.name)
        strategy_log_service.success(strategy_id, f"策略启动 — {strategy.name}")
        return True

    async def remove_strategy(self, strategy_id: int):
        job_id = f"strategy_{strategy_id}"
        self._strategy_jobs.pop(strategy_id, None)
        self._engines.pop(strategy_id, None)
        self._executors.pop(strategy_id, None)
        self._exchange_services.pop(strategy_id, None)
        if self._aps.get_job(job_id):
            self._aps.remove_job(job_id)
        async with async_session() as session:
            strategy = await session.get(Strategy, strategy_id)
            if strategy:
                strategy.status = "stopped"
                await session.commit()
        logger.info("Strategy %d stopped", strategy_id)

    async def _get_exchange_for_strategy(self, strategy_id: int) -> Optional[BaseExchangeService]:
        if strategy_id in self._exchange_services:
            return self._exchange_services[strategy_id]
        async with async_session() as session:
            strategy = await session.get(Strategy, strategy_id)
            if not strategy:
                return None
            exchange = await get_exchange_service(strategy.account_id)
            if exchange:
                self._exchange_services[strategy_id] = exchange
            return exchange

    async def _execute_strategy(self, strategy_id: int):
        async with _STRATEGY_SEMAPHORE:
            try:
                await self._execute_strategy_impl(strategy_id)
            except Exception as e:
                logger.error("Strategy %d unhandled error: %s", strategy_id, e, exc_info=True)

    async def _execute_strategy_impl(self, strategy_id: int):
        async with async_session() as session:
            # Master switch
            switch_result = await session.execute(
                select(BotConfig).where(BotConfig.key == "master_switch")
            )
            switch = switch_result.scalar()
            if switch and switch.value == "false":
                return

            strategy = await session.get(Strategy, strategy_id)
            if not strategy or strategy.status != "running":
                return

            strategy_log_service.info(strategy_id, "执行周期开始")

            exchange = await self._get_exchange_for_strategy(strategy_id)
            if not exchange:
                logger.warning("Strategy %d: exchange not available", strategy_id)
                strategy_log_service.warning(strategy_id, "无法获取交易所连接")
                return

            # Get current price from stream
            current_price = await price_stream.get_price(strategy.symbol)
            if not current_price:
                try:
                    ticker = await exchange.fetch_ticker(strategy.symbol)
                    current_price = float(ticker.get("last", 0))
                except Exception:
                    logger.warning("Strategy %d: no price for %s", strategy_id, strategy.symbol)
                    return

            # Execute grid strategy
            executor = self._executors.get(strategy_id)
            if not executor:
                engine = self._engines.get(strategy_id)
                if not engine:
                    engine = GridStrategyEngine(strategy)
                    self._engines[strategy_id] = engine
                executor = GridExecutor(engine)
                self._executors[strategy_id] = executor

            try:
                await executor.process_symbol(
                    session, strategy, strategy.symbol, exchange, current_price,
                )
                # Cleanup old filled/canceled orders to prevent memory leak
                from .order_tracker import order_tracker
                order_tracker.remove_done(strategy_id, min_age_seconds=3600)
            except Exception as e:
                logger.error("Strategy %d: processing error: %s", strategy_id, e)
                strategy_log_service.error(strategy_id, f"执行错误 — {e}")
                await session.rollback()



# Singleton
strategy_scheduler = StrategyScheduler()
