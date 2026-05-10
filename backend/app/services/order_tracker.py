"""In-memory order cache for real-time order state tracking."""
import time
import logging
from enum import Enum
from dataclasses import dataclass, field
from collections import defaultdict
from typing import Optional

logger = logging.getLogger(__name__)


class OrderState(Enum):
    PENDING = "pending"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    EXPIRED = "expired"


@dataclass
class CachedOrder:
    order_id: str
    symbol: str
    side: str          # 'buy' or 'sell'
    order_type: str    # 'limit' or 'market'
    amount: float
    price: float
    filled: float = 0.0
    status: OrderState = OrderState.PENDING
    created_at: float = field(default_factory=time.time)
    strategy_id: int = 0
    purpose: str = ""  # 'initial_entry', 'tp', 'grid_add', 'stop_loss'

    @property
    def is_active(self) -> bool:
        return self.status in (OrderState.PENDING, OrderState.PARTIALLY_FILLED)

    @property
    def is_done(self) -> bool:
        return self.status in (OrderState.FILLED, OrderState.CANCELED, OrderState.EXPIRED)


class OrderTracker:
    """Tracks exchange orders in memory with fast per-strategy lookups.

    Reduces exchange API calls. Batch checks all pending orders per tick.
    """

    def __init__(self):
        self._orders: dict[str, CachedOrder] = {}
        self._by_strategy: dict[int, set[str]] = defaultdict(set)

    def add(self, order_id: str, symbol: str, side: str, order_type: str,
            amount: float, price: float, strategy_id: int, purpose: str):
        """Register a new order in the tracker."""
        co = CachedOrder(
            order_id=order_id,
            symbol=symbol,
            side=side,
            order_type=order_type,
            amount=amount,
            price=price,
            strategy_id=strategy_id,
            purpose=purpose,
        )
        self._orders[order_id] = co
        self._by_strategy[strategy_id].add(order_id)

    def get(self, order_id: str) -> Optional[CachedOrder]:
        return self._orders.get(order_id)

    def get_active_for_strategy(self, strategy_id: int) -> list[CachedOrder]:
        """Get all active (not done) orders for a strategy."""
        ids = self._by_strategy.get(strategy_id, set())
        return [self._orders[oid] for oid in ids if oid in self._orders and self._orders[oid].is_active]

    def get_pending_by_purpose(self, strategy_id: int, purpose: str) -> list[CachedOrder]:
        """Get active orders of a specific purpose (e.g., 'tp', 'grid_add').

        Includes both PENDING and PARTIALLY_FILLED orders.
        """
        return [
            o for o in self.get_active_for_strategy(strategy_id)
            if o.purpose == purpose
        ]

    async def check_order(self, exchange, order_id: str, symbol: str) -> Optional[CachedOrder]:
        """Check a single order's status from exchange and update cache. Returns updated CachedOrder."""
        co = self._orders.get(order_id)
        if not co:
            return None
        try:
            raw = await exchange.fetch_order(order_id, symbol)
        except Exception as e:
            logger.debug("fetch_order %s failed: %s", order_id, e)
            return co

        status_str = (raw.get("status") or "").lower()
        info = raw.get("info") if isinstance(raw.get("info"), dict) else {}
        if isinstance(info, dict):
            st2 = str(info.get("state") or info.get("ordStatus") or "").lower()
            if st2 and st2 not in ("none", "null"):
                if not status_str or status_str in ("open", "live"):
                    status_str = st2
        co.filled = float(raw.get("filled", 0) or 0)
        avg_price = float(raw.get("average", 0) or 0)
        if avg_price > 0:
            co.price = avg_price
        amount = float(raw.get("amount", 0) or 0) or float(co.amount or 0)
        try:
            rem_raw = raw.get("remaining")
            rem = float(rem_raw) if rem_raw is not None and rem_raw != "" else None
        except (TypeError, ValueError):
            rem = None

        filled_done = status_str in ("closed", "filled")
        if not filled_done and amount > 1e-16:
            if co.filled >= amount * 0.998:
                filled_done = True
            if rem is not None and rem <= max(amount * 0.002, 1e-12) and co.filled > 0:
                filled_done = True
        if isinstance(info, dict) and str(info.get("state", "")).lower() == "filled":
            filled_done = True
        # OKX：部分响应里 amount/remaining 不全，用 accFillSz 与 sz 判断已完全成交
        if isinstance(info, dict) and not filled_done:
            try:
                acc = float(info.get("accFillSz") or 0)
                sz = float(info.get("sz") or 0)
                if sz > 1e-16 and acc >= sz * 0.998:
                    filled_done = True
            except (TypeError, ValueError):
                pass
            st = str(info.get("state") or "").lower()
            if st in ("filled", "effective"):
                filled_done = True

        if filled_done:
            co.status = OrderState.FILLED
        elif status_str in ("canceled", "cancelled"):
            co.status = OrderState.CANCELED
        elif status_str in ("expired",):
            co.status = OrderState.EXPIRED
        elif status_str == "open" and co.filled > 0:
            co.status = OrderState.PARTIALLY_FILLED
        return co

    async def check_all_pending(self, exchange, strategy_id: int) -> dict[str, CachedOrder]:
        """Check all pending orders for a strategy. Returns dict of order_id -> updated CachedOrder."""
        updated = {}
        for o in self.get_active_for_strategy(strategy_id):
            result = await self.check_order(exchange, o.order_id, o.symbol)
            if result:
                updated[o.order_id] = result
        return updated

    def get_filled(self, strategy_id: int, purpose: Optional[str] = None) -> list[CachedOrder]:
        """Get recently filled orders for a strategy."""
        ids = self._by_strategy.get(strategy_id, set())
        result = []
        for oid in ids:
            o = self._orders.get(oid)
            if o and o.status == OrderState.FILLED:
                if purpose is None or o.purpose == purpose:
                    result.append(o)
        return result

    def remove_done(self, strategy_id: int, min_age_seconds: float = 3600):
        """Remove old filled/canceled orders from memory to prevent leaks."""
        now = time.time()
        ids = self._by_strategy.get(strategy_id, set())
        to_remove = set()
        for oid in ids:
            o = self._orders.get(oid)
            if o and o.is_done and (now - o.created_at) > min_age_seconds:
                to_remove.add(oid)
        for oid in to_remove:
            self._orders.pop(oid, None)
            self._by_strategy[strategy_id].discard(oid)

    def clear_strategy(self, strategy_id: int):
        """Remove all tracked orders for a strategy."""
        ids = self._by_strategy.pop(strategy_id, set())
        for oid in ids:
            self._orders.pop(oid, None)


# Singleton
order_tracker = OrderTracker()
