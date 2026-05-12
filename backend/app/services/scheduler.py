"""Strategy scheduler: lifecycle management and grid strategy execution loop."""
import asyncio
import logging
from typing import Optional

from sqlalchemy import select
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from ..database import async_session
from ..models.strategy import Strategy
from ..models.position import Position
from ..models.account import Account
from ..models.bot_config import BotConfig
from ..config import now_beijing, BEIJING_TZ
from .exchange_base import BaseExchangeService
from .exchange_factory import get_exchange_service, get_public_exchange
from .log_service import strategy_log_service
from .grid_engine import GridStrategyEngine
from .grid_executor import GridExecutor
from .price_stream import price_stream
from .health_monitor import health_monitor
from .sync_service import position_sync_service

logger = logging.getLogger(__name__)

_STRATEGY_TICK_SECONDS = 30  # 兜底轮询(WS实时监听已覆盖订单成交)
_STRATEGY_SEMAPHORE = asyncio.Semaphore(100)


class StrategyScheduler:
    def __init__(self):
        self._aps = AsyncIOScheduler(timezone=BEIJING_TZ)
        self._strategy_jobs: dict[int, str] = {}
        self._exchange_services: dict[int, BaseExchangeService] = {}
        self._engines: dict[int, GridStrategyEngine] = {}
        self._executors: dict[int, GridExecutor] = {}
        self._order_watch_tasks: dict[int, asyncio.Task] = {}
        self._running = False
        self._strategy_locks: dict[int, asyncio.Lock] = {}

    def _get_strategy_lock(self, strategy_id: int) -> asyncio.Lock:
        if strategy_id not in self._strategy_locks:
            self._strategy_locks[strategy_id] = asyncio.Lock()
        return self._strategy_locks[strategy_id]

    @property
    def scheduler(self) -> AsyncIOScheduler:
        return self._aps

    async def resume_running_strategies(self):
        """Restart: re-register jobs for strategies that were running, rebuild exchange connections."""
        from .order_tracker import order_tracker
        async with async_session() as session:
            result = await session.execute(
                select(Strategy).where(Strategy.status == "running").order_by(Strategy.id)
            )
            rows = list(result.scalars().all())
        for s in rows:
            self._register_strategy_job(s.id)
            exchange = await get_exchange_service(s.account_id)
            if exchange:
                self._exchange_services[s.id] = exchange
                try:
                    open_orders = await exchange.fetch_open_orders(s.symbol)
                    for oo in (open_orders or []):
                        oid = str(oo.get("id", ""))
                        side = (oo.get("side") or "").lower()
                        otype = (oo.get("type") or "").lower()
                        amount = float(oo.get("amount", 0) or 0)
                        price = float(oo.get("price", 0) or 0)
                        if oid and amount > 0:
                            purpose = "tp" if side != s.direction.lower() else "grid_add"
                            order_tracker.add(oid, s.symbol, side, otype, amount, price, s.id, purpose)
                    n_open = len(open_orders or [])
                    n_algo_sl = 0
                    if float(getattr(s, "cumulative_loss_threshold_u", 0) or 0) > 0:
                        try:
                            algo_rows = await exchange.fetch_open_algo_orders(s.symbol)
                        except Exception as e:
                            logger.debug("Strategy %d: fetch_open_algo_orders: %s", s.id, e)
                            algo_rows = []
                        for row in algo_rows or []:
                            typ = str(row.get("type") or row.get("orderType") or "").upper()
                            if "STOP" not in typ:
                                continue
                            aid = str(row.get("algoId") or "")
                            if not aid:
                                continue
                            side_a = str(row.get("side") or "").lower()
                            if side_a not in ("buy", "sell"):
                                continue
                            qty = float(row.get("origQty") or row.get("quantity") or row.get("qty") or 0)
                            trig = float(row.get("triggerPrice") or row.get("stopPrice") or 0)
                            if qty <= 0:
                                qty = float(row.get("executedQty") or row.get("cumQty") or 0)
                            if qty <= 0:
                                continue
                            order_tracker.add(aid, s.symbol, side_a, "stop", qty, trig, s.id, "stop_loss")
                            n_algo_sl += 1
                    logger.info(
                        "Strategy %d: restored %d open orders + %d algo stop orders to tracker",
                        s.id,
                        n_open,
                        n_algo_sl,
                    )
                except Exception as e:
                    logger.warning("Strategy %d: failed to restore orders: %s", s.id, e)
            self._engines[s.id] = GridStrategyEngine(s)
            self._executors[s.id] = GridExecutor(self._engines[s.id])
            strategy_log_service.info(s.id, "后端已重启：已恢复调度任务")
            logger.info("Resumed scheduler for strategy %d (%s %s)", s.id, s.symbol, s.direction)

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

        # 启动订单成交实时监听
        await self._start_order_watchers()

    async def _start_order_watchers(self):
        """启动WS订单监听,加仓/止盈成交时立即触发策略执行."""
        self._running = True
        async with async_session() as session:
            result = await session.execute(
                select(Strategy).where(Strategy.status == "running")
            )
            for s in result.scalars().all():
                if s.id in self._order_watch_tasks:
                    continue
                exchange = self._exchange_services.get(s.id)
                if not exchange:
                    continue
                self._order_watch_tasks[s.id] = asyncio.create_task(
                    self._watch_orders_loop(s.id, exchange, s.symbol)
                )
                logger.info("Order watcher started for strategy %d (%s)", s.id, s.symbol)

    async def _try_hydrate_tracker_order_from_db(
        self, strategy_id: int, symbol: str, order_id: str,
    ):
        """WS 先到而内存 tracker 无记录时（如刚重启），用 DB 未平仓行的止盈/加仓单号补登记。"""
        from .order_tracker import order_tracker

        oid = (order_id or "").strip()
        if not oid:
            return None
        existing = order_tracker.get(oid)
        if existing:
            return existing if existing.strategy_id == strategy_id else None

        want = BaseExchangeService._norm_sym(symbol)
        async with async_session() as session:
            r = await session.execute(
                select(Position).where(
                    Position.strategy_id == strategy_id,
                    Position.closed_at.is_(None),
                )
            )
            rows = list(r.scalars().all())

        for p in rows:
            if BaseExchangeService._norm_sym(p.symbol or "") != want:
                continue
            purpose = None
            side_hint = None
            qty_hint = float(p.quantity or 0)
            price_hint = 0.0
            if (p.tp_limit_order_id or "").strip() == oid:
                purpose = "tp"
                side_hint = "sell" if p.side == "long" else "buy"
                price_hint = float(p.take_profit_price or 0) or 0.0
            elif (p.add_limit_order_id or "").strip() == oid:
                purpose = "grid_add"
                side_hint = "buy" if p.side == "long" else "sell"
                price_hint = float(p.grid_trigger_price or 0) or 0.0
            else:
                continue
            order_tracker.add(
                oid, symbol, side_hint, "limit",
                qty_hint, price_hint, strategy_id, purpose,
            )
            return order_tracker.get(oid)
        return None

    async def _watch_orders_loop(self, strategy_id: int, exchange, symbol: str):
        """监听订单成交(WebSocket),成交后立即触发策略tick."""
        from .order_tracker import order_tracker, OrderState
        consecutive_errors = 0
        max_consecutive_errors = 10
        base_delay = 2.0

        while self._running and strategy_id in self._order_watch_tasks:
            try:
                if not hasattr(exchange, 'ws_exchange') or not exchange.ws_exchange:
                    await asyncio.sleep(2)
                    continue
                ws = exchange.ws_exchange
                ws_symbol = symbol
                fmt = getattr(exchange, "_format_symbol", None)
                if callable(fmt):
                    try:
                        ws_symbol = fmt(symbol)
                    except Exception:
                        ws_symbol = symbol
                orders = await ws.watch_orders(ws_symbol)
                consecutive_errors = 0
                for raw in (orders if isinstance(orders, list) else [orders]):
                    info = raw.get("info") if isinstance(raw.get("info"), dict) else {}
                    oid = str(raw.get("id", "") or raw.get("orderId", "") or "").strip()
                    if not oid and isinstance(info, dict):
                        oid = str(info.get("ordId") or info.get("algoId") or "").strip()
                    if not oid:
                        continue
                    ws_status = (raw.get("status") or "").lower()
                    if isinstance(info, dict) and not ws_status:
                        ws_status = str(info.get("state") or info.get("ordStatus") or "").lower()
                    filled_w = float(raw.get("filled", 0) or 0)
                    amount_w = float(raw.get("amount", 0) or 0)
                    if isinstance(info, dict):
                        try:
                            acc = float(info.get("accFillSz") or 0)
                            sz = float(info.get("sz") or 0)
                            if acc > filled_w:
                                filled_w = acc
                            if sz > amount_w:
                                amount_w = sz
                        except (TypeError, ValueError):
                            pass
                    essentially_filled = amount_w > 1e-12 and filled_w >= amount_w * 0.998
                    is_filled = (
                        ws_status in ("closed", "filled", "effective")
                        or essentially_filled
                    )
                    is_canceled = ws_status in ("canceled", "cancelled") and not essentially_filled
                    if not is_filled and not is_canceled:
                        continue
                    co = order_tracker.get(oid)
                    if not co:
                        co = await self._try_hydrate_tracker_order_from_db(strategy_id, symbol, oid)
                    if not co or co.strategy_id != strategy_id:
                        continue
                    co.filled = filled_w or co.filled
                    avg = BaseExchangeService.avg_fill_price_from_order(raw)
                    if avg > 0:
                        co.price = avg
                    if is_filled or essentially_filled:
                        co.status = OrderState.FILLED
                        logger.info(
                            "WS order FILLED: %s purpose=%s filled=%.4f price=%.4f for strategy %d",
                            oid, co.purpose, co.filled, co.price, strategy_id,
                        )
                    else:
                        co.status = OrderState.CANCELED
                        logger.info("WS order CANCELED: %s for strategy %d", oid, strategy_id)
                    asyncio.create_task(self._execute_strategy(strategy_id))
            except asyncio.CancelledError:
                break
            except Exception as e:
                consecutive_errors += 1
                err_str = str(e).lower()
                is_permanent = any(x in err_str for x in ["invalid api key", "signature", "permission", "banned", "forbidden"])
                if is_permanent:
                    logger.error("Order watcher %d permanent error: %s — stopping watcher", strategy_id, e)
                    break
                delay = min(base_delay * (2 ** min(consecutive_errors, 5)), 60.0)
                if consecutive_errors <= 3:
                    logger.debug("Order watcher %d error (%d): %s — retry in %.1fs", strategy_id, consecutive_errors, e, delay)
                else:
                    logger.warning("Order watcher %d repeated errors (%d): %s — retry in %.1fs", strategy_id, consecutive_errors, e, delay)
                if consecutive_errors >= max_consecutive_errors:
                    logger.error("Order watcher %d exceeded max errors, falling back to REST polling", strategy_id)
                    break
                await asyncio.sleep(delay)

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

    async def _position_sync_tick(self):
        """将本地未平仓记录与交易所持仓对账（每账户最多每 60 秒一次，由 sync_service 节流）。"""
        try:
            async with async_session() as session:
                r = await session.execute(
                    select(Position.account_id)
                    .where(Position.closed_at.is_(None))
                    .distinct()
                )
                account_ids = [row[0] for row in r.all()]
        except Exception as e:
            logger.warning("Position sync tick: load accounts failed: %s", e)
            return

        for aid in account_ids:
            try:
                exchange = await get_exchange_service(aid)
                if exchange:
                    await position_sync_service.sync(exchange, aid)
            except Exception as e:
                logger.warning("Position sync tick: account %s failed: %s", aid, e)

    def start(self):
        if not self._aps.running:
            self._aps.start()
        if self._aps.get_job("position_sync"):
            return
        self._aps.add_job(
            self._position_sync_tick,
            "interval",
            seconds=60,
            id="position_sync",
            coalesce=True,
            max_instances=1,
        )

    def stop(self):
        self._running = False
        for task in list(self._order_watch_tasks.values()):
            if not task.done():
                task.cancel()
        self._order_watch_tasks.clear()
        if self._aps.get_job("position_sync"):
            try:
                self._aps.remove_job("position_sync")
            except Exception:
                pass
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
        logger.info("Strategy %d (%s %s) started", strategy_id, strategy.symbol, strategy.direction)
        strategy_log_service.success(strategy_id, f"策略启动 — {strategy.symbol} {strategy.direction}")

        # 启动订单成交实时监听
        exchange = self._exchange_services.get(strategy_id)
        if exchange and strategy_id not in self._order_watch_tasks:
            self._running = True
            self._order_watch_tasks[strategy_id] = asyncio.create_task(
                self._watch_orders_loop(strategy_id, exchange, strategy.symbol)
            )
            logger.info("Order watcher started for strategy %d", strategy_id)

        # 立即执行一次
        asyncio.create_task(self._execute_strategy(strategy_id))

        return True

    async def remove_strategy(self, strategy_id: int):
        job_id = f"strategy_{strategy_id}"
        self._strategy_jobs.pop(strategy_id, None)
        self._engines.pop(strategy_id, None)
        self._executors.pop(strategy_id, None)
        task = self._order_watch_tasks.pop(strategy_id, None)
        if task and not task.done():
            task.cancel()
        if self._aps.get_job(job_id):
            self._aps.remove_job(job_id)

        exchange = self._exchange_services.pop(strategy_id, None)
        self._strategy_locks.pop(strategy_id, None)

        async with async_session() as session:
            strategy = await session.get(Strategy, strategy_id)
            if not strategy:
                return

            symbol = strategy.symbol

            if exchange:
                try:
                    ex_id = getattr(exchange, "exchange_id", "") or ""
                    hedge = getattr(exchange, "hedge_mode", True)
                    dir_low = (strategy.direction or "").strip().lower()
                    pos_filter = dir_low if hedge else None

                    if ex_id == "okx" and hasattr(exchange, "cancel_all_pending_orders_for_symbol"):
                        try:
                            await exchange.cancel_all_pending_orders_for_symbol(symbol, pos_filter)
                        except Exception as e:
                            logger.warning("Strategy %d stop: OKX cancel_all_pending: %s", strategy_id, e)

                    open_orders = await exchange.fetch_open_orders(symbol)
                    cancel_tasks = []
                    for oo in (open_orders or []):
                        if not BaseExchangeService.open_order_matches_strategy_scope(oo, symbol, dir_low, hedge):
                            continue
                        oid = str(oo.get("id") or oo.get("orderId") or oo.get("algoId") or "").strip()
                        if oid:
                            cancel_tasks.append(exchange.cancel_order(oid, symbol))
                    if cancel_tasks:
                        results = await asyncio.gather(*cancel_tasks, return_exceptions=True)
                        success = sum(1 for r in results if not isinstance(r, Exception))
                        logger.info("Strategy %d stopped: cancelled %d/%d orders on exchange", strategy_id, success, len(cancel_tasks))

                    if hasattr(exchange, "cancel_all_open_algo_orders"):
                        try:
                            n_algo = await exchange.cancel_all_open_algo_orders(symbol, pos_filter)
                            if n_algo:
                                logger.info("Strategy %d stopped: cancelled %d open algo orders", strategy_id, n_algo)
                        except Exception as e:
                            logger.warning("Strategy %d stop: cancel_all_open_algo_orders: %s", strategy_id, e)
                except Exception as e:
                    logger.warning("Strategy %d stop: failed to cancel orders: %s", strategy_id, e)

            from .order_tracker import order_tracker
            order_tracker.clear_strategy(strategy_id)

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
        lock = self._get_strategy_lock(strategy_id)
        if lock.locked():
            logger.debug("Strategy %d already executing, skip", strategy_id)
            return
        async with lock:
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
                    current_price = float(ticker.get("last", 0) or 0)
                except Exception as e:
                    logger.warning(
                        "Strategy %d: no price for %s: %s",
                        strategy_id,
                        strategy.symbol,
                        e,
                    )
                    strategy_log_service.warning(
                        strategy_id,
                        f"无法获取 {strategy.symbol} 行情，跳过本周期: {e}",
                    )
                    return
            if current_price <= 0:
                strategy_log_service.warning(
                    strategy_id,
                    f"行情价格无效({current_price})，跳过本周期",
                )
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
                from .order_tracker import order_tracker
                order_tracker.remove_done(strategy_id, min_age_seconds=3600)
                health_monitor.record_success(strategy_id)
            except Exception as e:
                logger.error("Strategy %d: processing error: %s", strategy_id, e)
                strategy_log_service.error(strategy_id, f"执行错误 — {e}")
                health_monitor.record_failure(strategy_id, str(e))
                await session.rollback()



# Singleton
strategy_scheduler = StrategyScheduler()
