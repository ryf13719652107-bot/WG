"""Position synchronization between local DB and exchange — simplified for grid strategy."""
import time
import logging
from collections import defaultdict

from sqlalchemy import select

from ..database import async_session
from ..models.position import Position
from ..models.trade import Trade
from ..config import now_beijing
from .exchange_base import BaseExchangeService

logger = logging.getLogger(__name__)

_POSITION_SYNC_INTERVAL = 60


def _leg_key(symbol: str, side: str) -> tuple[str, str]:
    """与前端 normSym、仪表盘、grid_executor 一致，避免 OKX 存 SUI-USDT 与所里 SUIUSDT 对不齐。"""
    return BaseExchangeService._norm_sym(symbol), (side or "").lower()


class PositionSyncService:
    """Reconciles local DB positions with exchange state.

    If an exchange position no longer exists but local DB still has it open,
    close the local record and create a trade entry.
    """

    def __init__(self):
        self._sync_timestamps: dict[str, float] = {}

    async def sync(self, exchange: BaseExchangeService, account_id: int):
        sync_key = f"sync_{account_id}"
        now_ts = time.time()
        if now_ts - self._sync_timestamps.get(sync_key, 0) < _POSITION_SYNC_INTERVAL:
            return
        self._sync_timestamps[sync_key] = now_ts

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

                # Build map of exchange positions（symbol 与全站 _norm_sym 一致）
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
                    sk = _leg_key(lp.symbol, lp.side)
                    by_leg[sk].append(lp)

                for (sym_key, side_key), legs in by_leg.items():
                    if (sym_key, side_key) in exchange_map:
                        continue

                    sym0 = legs[0].symbol
                    if hasattr(exchange, "cancel_all_pending_orders_for_symbol"):
                        try:
                            hedge = getattr(exchange, "hedge_mode", True)
                            pos_filter = side_key if hedge else None
                            n_cancel = await exchange.cancel_all_pending_orders_for_symbol(sym0, pos_filter)
                            if n_cancel:
                                logger.info(
                                    "Sync: cancelled %d pending orders for %s %s before closing DB leg",
                                    n_cancel, sym_key, side_key,
                                )
                        except Exception as e:
                            logger.warning("Sync: cancel pending for %s %s: %s", sym_key, side_key, e)

                    exit_price = float(legs[0].mark_price or legs[0].entry_price or 0)
                    try:
                        sym0 = legs[0].symbol
                        tk = await exchange.fetch_ticker(sym0)
                        last = float(tk.get("last", 0) or tk.get("close", 0) or 0)
                        if last > 0:
                            exit_price = last
                    except Exception as e:
                        logger.debug("sync exit_price ticker %s: %s", sym_key, e)
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
                        trade = Trade(
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
                            grid_level=getattr(lp, 'grid_level', 0),
                            close_reason="sync",
                        )
                        session.add(trade)
                        lp.closed_at = sync_now
                    logger.warning(
                        "Sync: leg %s (%d DB rows) missing on exchange — closed at %.8f",
                        sym_key, len(legs), exit_price,
                    )

                await session.commit()
        except Exception as e:
            logger.error("Position sync for account %d failed: %s", account_id, e)


position_sync_service = PositionSyncService()
