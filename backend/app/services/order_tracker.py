"""In-memory order cache for real-time order state tracking."""
import time
import logging
from enum import Enum
from dataclasses import dataclass, field
from collections import defaultdict
from typing import Optional

from .exchange_base import BaseExchangeService

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
    side: str
    order_type: str
    amount: float
    price: float
    filled: float = 0.0
    status: OrderState = OrderState.PENDING
    created_at: float = field(default_factory=time.time)
    strategy_id: int = 0
    purpose: str = ""

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

    def add_or_update(self, order_id: str, symbol: str, side: str, order_type: str,
                      amount: float, price: float, strategy_id: int, purpose: str):
        existing = self._orders.get(order_id)
        if existing:
            if amount > 0:
                existing.amount = amount
            if price > 0:
                existing.price = price
            if strategy_id > 0:
                existing.strategy_id = strategy_id
            if purpose:
                existing.purpose = purpose
            return existing
        return self.add(order_id, symbol, side, order_type, amount, price, strategy_id, purpose)

    def get(self, order_id: str) -> Optional[CachedOrder]:
        return self._orders.get(order_id)

    def get_active_for_strategy(self, strategy_id: int) -> list[CachedOrder]:
        ids = self._by_strategy.get(strategy_id, set())
        return [self._orders[oid] for oid in ids if oid in self._orders and self._orders[oid].is_active]

    def get_pending_by_purpose(self, strategy_id: int, purpose: str) -> list[CachedOrder]:
        return [
            o for o in self.get_active_for_strategy(strategy_id)
            if o.purpose == purpose
        ]

    def get_partially_filled_by_purpose(self, strategy_id: int, purpose: str) -> list[CachedOrder]:
        return [
            o for o in self.get_active_for_strategy(strategy_id)
            if o.purpose == purpose and o.status == OrderState.PARTIALLY_FILLED
        ]

    @staticmethod
    def _raw_order_is_filled(raw: dict, co: CachedOrder) -> bool:
        status_str = (raw.get("status") or "").lower()
        info = raw.get("info") if isinstance(raw.get("info"), dict) else {}
        if isinstance(info, dict):
            st2 = str(info.get("state") or info.get("ordStatus") or "").lower()
            if st2 and st2 not in ("none", "null"):
                if not status_str or status_str in ("open", "live"):
                    status_str = st2

        filled = float(raw.get("filled", 0) or 0)
        if isinstance(info, dict):
            try:
                acc = float(info.get("accFillSz") or 0)
                if acc > filled:
                    filled = acc
            except (TypeError, ValueError):
                pass

        amount = float(raw.get("amount", 0) or 0) or float(co.amount or 0)
        if isinstance(info, dict):
            try:
                sz = float(info.get("sz") or 0)
                if sz > amount:
                    amount = sz
            except (TypeError, ValueError):
                pass

        try:
            rem_raw = raw.get("remaining")
            rem = float(rem_raw) if rem_raw is not None and rem_raw != "" else None
        except (TypeError, ValueError):
            rem = None

        if status_str in ("closed", "filled", "effective"):
            return True
        if isinstance(info, dict):
            if str(info.get("state", "")).lower() in ("filled", "effective"):
                return True
            fs = str(info.get("fillState") or "").lower()
            if fs in ("filled", "full_fill", "full"):
                return True
        if amount > 1e-16:
            if filled >= amount * 0.998:
                return True
            if rem is not None and rem <= max(amount * 0.002, 1e-12) and filled > 0:
                return True
        return False

    def _apply_raw_to_co(self, co: CachedOrder, raw: dict) -> CachedOrder:
        info = raw.get("info") if isinstance(raw.get("info"), dict) else {}
        co.filled = float(raw.get("filled", 0) or 0)
        if isinstance(info, dict):
            try:
                acc = float(info.get("accFillSz") or 0)
                if acc > co.filled:
                    co.filled = acc
            except (TypeError, ValueError):
                pass
        avg_price = BaseExchangeService.avg_fill_price_from_order(raw)
        if avg_price > 0:
            co.price = avg_price

        status_str = (raw.get("status") or "").lower()
        if isinstance(info, dict):
            st2 = str(info.get("state") or info.get("ordStatus") or "").lower()
            if st2 and st2 not in ("none", "null"):
                if not status_str or status_str in ("open", "live"):
                    status_str = st2

        if self._raw_order_is_filled(raw, co):
            co.status = OrderState.FILLED
        elif status_str in ("canceled", "cancelled"):
            co.status = OrderState.CANCELED
        elif status_str in ("expired",):
            co.status = OrderState.EXPIRED
        elif status_str == "open" and co.filled > 0:
            co.status = OrderState.PARTIALLY_FILLED
        return co

    async def check_order(self, exchange, order_id: str, symbol: str) -> Optional[CachedOrder]:
        co = self._orders.get(order_id)
        if not co:
            return None
        sym = (co.symbol or symbol or "").strip() or symbol
        try:
            raw = await exchange.fetch_order(order_id, sym)
        except Exception as e:
            if co.purpose == "tp":
                logger.warning("fetch_order tp %s failed (symbol=%s): %s", order_id, sym, e)
            else:
                logger.debug("fetch_order %s failed: %s", order_id, e)
            return co
        return self._apply_raw_to_co(co, raw)

    async def check_all_pending(self, exchange, strategy_id: int) -> dict[str, CachedOrder]:
        updated = {}
        for o in self.get_active_for_strategy(strategy_id):
            result = await self.check_order(exchange, o.order_id, o.symbol)
            if result:
                updated[o.order_id] = result
        return updated

    def get_filled(self, strategy_id: int, purpose: Optional[str] = None) -> list[CachedOrder]:
        ids = self._by_strategy.get(strategy_id, set())
        result = []
        for oid in ids:
            o = self._orders.get(oid)
            if o and o.status == OrderState.FILLED:
                if purpose is None or o.purpose == purpose:
                    result.append(o)
        return result

    def remove_done(self, strategy_id: int, min_age_seconds: float = 3600):
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

    def discard_order(self, order_id: str) -> None:
        oid = (order_id or "").strip()
        if not oid:
            return
        co = self._orders.pop(oid, None)
        if co:
            sid = int(co.strategy_id) if co.strategy_id is not None else 0
            self._by_strategy.get(sid, set()).discard(oid)

    def clear_strategy(self, strategy_id: int):
        ids = self._by_strategy.pop(strategy_id, set())
        for oid in ids:
            self._orders.pop(oid, None)

    def has_active_tp_for_symbol(self, strategy_id: int, symbol: str) -> bool:
        for o in self.get_pending_by_purpose(strategy_id, "tp"):
            if BaseExchangeService._norm_sym(o.symbol) == BaseExchangeService._norm_sym(symbol):
                return True
        return False


order_tracker = OrderTracker()
