"""Position synchronization between local DB and exchange — simplified for grid strategy."""
import asyncio
import time
import logging
from collections import defaultdict

from sqlalchemy import select

from ..database import async_session
from ..models.position import Position
from ..models.strategy import Strategy
from ..models.trade import Trade
from ..config import now_beijing
from .exchange_base import BaseExchangeService
from .order_tracker import order_tracker, CachedOrder

logger = logging.getLogger(__name__)

_POSITION_SYNC_INTERVAL = 60


def _leg_key(symbol: str, side: str) -> tuple[str, str]:
    """与前端 normSym、仪表盘、grid_executor 一致，避免 OKX 存 SUI-USDT 与所里 SUIUSDT 对不齐。"""
    return BaseExchangeService._norm_sym(symbol), (side or "").lower()


def _order_raw_filled(raw: dict) -> bool:
    dummy = CachedOrder(
        order_id="?", symbol="", side="", order_type="limit", amount=0.0, price=0.0,
    )
    return order_tracker._raw_order_is_filled(raw, dummy)


class PositionSyncService:
    """Reconciles local DB positions with exchange state.

    运行中策略：所里已平、DB 仍有仓 → 不直接记 sync 平仓，唤醒 grid_executor 走止盈/撤单/重开。
    已停止策略：仍由 sync 关 DB，并尽量识别止盈单成交记 take_profit。
    """

    def __init__(self):
        self._sync_timestamps: dict[str, float] = {}

    async def _resolve_flat_leg_exit(
        self, exchange: BaseExchangeService, legs: list[Position],
    ) -> tuple[float, str]:
        """推断已平仓腿的出场价与 close_reason。"""
        exit_price = float(legs[0].mark_price or legs[0].entry_price or 0)
        close_reason = "sync"
        sym0 = legs[0].symbol

        for lp in legs:
            tp_oid = (getattr(lp, "tp_limit_order_id", None) or "").strip()
            if not tp_oid:
                continue
            try:
                raw = await exchange.fetch_order(tp_oid, lp.symbol or sym0)
                if _order_raw_filled(raw):
                    px = BaseExchangeService.avg_fill_price_from_order(raw)
                    if px > 0:
                        exit_price = px
                    elif float(getattr(lp, "take_profit_price", 0) or 0) > 0:
                        exit_price = float(lp.take_profit_price)
                    return exit_price, "take_profit"
            except Exception as e:
                logger.debug("sync resolve TP order %s: %s", tp_oid, e)

        # 持仓已平且止盈单不在挂单中 → 高概率为止盈
        tp_oid0 = (getattr(legs[0], "tp_limit_order_id", None) or "").strip()
        if tp_oid0:
            try:
                oo = await exchange.fetch_open_orders(sym0)
            except Exception:
                oo = []
            still_open = False
            for row in oo or []:
                info = row.get("info") if isinstance(row.get("info"), dict) else {}
                row_oid = str(row.get("id") or row.get("orderId") or "").strip()
                if isinstance(info, dict) and not row_oid:
                    row_oid = str(info.get("ordId") or "").strip()
                if row_oid == tp_oid0:
                    still_open = True
                    break
            if not still_open:
                tp_px = float(getattr(legs[0], "take_profit_price", 0) or 0)
                if tp_px > 0:
                    exit_price = tp_px
                return exit_price, "take_profit"

        try:
            tk = await exchange.fetch_ticker(sym0)
            last = float(tk.get("last", 0) or tk.get("close", 0) or 0)
            if last > 0:
                exit_price = last
        except Exception as e:
            logger.debug("sync exit_price ticker %s: %s", sym0, e)
        return exit_price, close_reason

    async def _cancel_leg_pending_orders(
        self, exchange: BaseExchangeService, symbol: str, side_key: str,
    ) -> int:
        if not hasattr(exchange, "cancel_all_pending_orders_for_symbol"):
            return 0
        try:
            hedge = getattr(exchange, "hedge_mode", True)
            pos_filter = side_key if hedge else None
            return int(await exchange.cancel_all_pending_orders_for_symbol(symbol, pos_filter) or 0)
        except Exception as e:
            logger.warning("Sync: cancel pending for %s %s: %s", symbol, side_key, e)
            return 0

    async def _close_db_leg(
        self,
        session,
        exchange: BaseExchangeService,
        legs: list[Position],
        *,
        sync_now,
    ) -> None:
        from .log_service import strategy_log_service

        sym_key = _leg_key(legs[0].symbol, legs[0].side)[0]
        side_key = (legs[0].side or "").lower()
        sym0 = legs[0].symbol

        n_cancel = await self._cancel_leg_pending_orders(exchange, sym0, side_key)
        exit_price, close_reason = await self._resolve_flat_leg_exit(exchange, legs)

        for lp in legs:
            pnl = (
                (exit_price - lp.entry_price) * lp.quantity
                if lp.side == "long"
                else (lp.entry_price - exit_price) * lp.quantity
            )
            pct = (
                ((exit_price - lp.entry_price) / lp.entry_price * 100)
                if lp.side == "long" and lp.entry_price > 0
                else ((lp.entry_price - exit_price) / lp.entry_price * 100)
                if lp.entry_price > 0
                else 0
            )
            session.add(
                Trade(
                    strategy_id=lp.strategy_id,
                    account_id=lp.account_id,
                    symbol=lp.symbol,
                    side=lp.side,
                    quantity=lp.quantity,
                    entry_price=lp.entry_price,
                    exit_price=exit_price,
                    realized_pnl=pnl,
                    pnl_pct=round(pct, 2),
                    entry_time=lp.opened_at or sync_now,
                    exit_time=sync_now,
                    layer=lp.layer,
                    grid_level=getattr(lp, "grid_level", 0),
                    close_reason=close_reason,
                )
            )
            lp.closed_at = sync_now

        reason_label = "止盈" if close_reason == "take_profit" else "同步平仓"
        for sid in {lp.strategy_id for lp in legs}:
            msg = (
                f"{reason_label}对账: {sym0} {side_key} 已写入 {len(legs)} 笔交易 "
                f"(close_reason={close_reason}) 出场价≈{exit_price:.6f}"
            )
            if n_cancel:
                msg += f"，已撤残留挂单约 {n_cancel} 笔"
            strategy_log_service.success(sid, msg)

        logger.warning(
            "Sync: leg %s (%d DB rows) missing on exchange — closed as %s at %.8f",
            sym_key, len(legs), close_reason, exit_price,
        )

    async def sync(self, exchange: BaseExchangeService, account_id: int):
        sync_key = f"sync_{account_id}"
        now_ts = time.time()
        if now_ts - self._sync_timestamps.get(sync_key, 0) < _POSITION_SYNC_INTERVAL:
            return
        self._sync_timestamps[sync_key] = now_ts

        wake_strategy_ids: set[int] = set()

        try:
            exchange_positions = await exchange.fetch_positions()
            async with async_session() as session:
                result = await session.execute(
                    select(Position).where(
                        Position.closed_at.is_(None),
                        Position.account_id == account_id,
                    )
                )
                local_positions = list(result.scalars().all())

                exchange_map: dict[tuple[str, str], dict] = {}
                for ep in exchange_positions:
                    contracts = BaseExchangeService.position_row_contracts_abs(ep)
                    if contracts <= 0:
                        continue
                    sym = BaseExchangeService._norm_sym(str(ep.get("symbol") or ""))
                    side = BaseExchangeService.position_row_side_lower(ep)
                    if side not in ("long", "short"):
                        continue
                    exchange_map[(sym, side)] = ep

                sync_now = now_beijing()
                by_leg: dict[tuple[str, str], list[Position]] = defaultdict(list)
                for lp in local_positions:
                    by_leg[_leg_key(lp.symbol, lp.side)].append(lp)

                from .log_service import strategy_log_service

                for (sym_key, side_key), legs in by_leg.items():
                    if (sym_key, side_key) in exchange_map:
                        continue

                    by_strategy: dict[int, list[Position]] = defaultdict(list)
                    for lp in legs:
                        by_strategy[int(lp.strategy_id or 0)].append(lp)

                    for sid, s_legs in by_strategy.items():
                        if sid <= 0:
                            await self._close_db_leg(session, exchange, s_legs, sync_now=sync_now)
                            continue
                        strategy = await session.get(Strategy, sid)
                        if strategy and strategy.status == "running":
                            wake_strategy_ids.add(sid)
                            strategy_log_service.info(
                                sid,
                                f"对账: 交易所 {s_legs[0].symbol} {side_key} 已无持仓，"
                                f"交由策略引擎处理止盈记账/撤单/重开（避免误记同步平仓）",
                            )
                            continue
                        await self._close_db_leg(session, exchange, s_legs, sync_now=sync_now)

                await session.commit()

            if wake_strategy_ids:
                from .scheduler import strategy_scheduler

                for sid in wake_strategy_ids:
                    asyncio.create_task(strategy_scheduler._execute_strategy(sid))
        except Exception as e:
            logger.error("Position sync for account %d failed: %s", account_id, e)


position_sync_service = PositionSyncService()
