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


def _norm_leg_symbol(sym: str) -> str:
    return (sym or "").replace("/", "").replace(":USDT", "").replace("-SWAP", "").upper()


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

                # Build map of exchange positions
                exchange_map: dict[tuple[str, str], dict] = {}
                for ep in exchange_positions:
                    contracts = float(ep.get("contracts", 0) or 0)
                    if contracts <= 0:
                        continue
                    sym = _norm_leg_symbol(ep.get("symbol") or "")
                    side = (ep.get("side") or "").lower()
                    exchange_map[(sym, side)] = ep

                sync_now = now_beijing()
                by_leg: dict[tuple[str, str], list[Position]] = defaultdict(list)
                for lp in local_positions:
                    sk = (_norm_leg_symbol(lp.symbol), lp.side.lower())
                    by_leg[sk].append(lp)

                for (sym_key, _), legs in by_leg.items():
                    if (sym_key, _) in exchange_map:
                        continue

                    exit_price = float(legs[0].mark_price or legs[0].entry_price or 0)
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
