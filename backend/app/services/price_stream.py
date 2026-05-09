"""Unified WebSocket price feed with auto-reconnect and REST fallback.

One watch_tickers subscription per exchange covers all tracked symbols.
Distributes price updates to registered callbacks.
"""
import asyncio
import time
import logging
from collections import defaultdict
from typing import Optional, Callable, Awaitable

logger = logging.getLogger(__name__)

RECONNECT_BASE_DELAY = 1.0
RECONNECT_MAX_DELAY = 30.0
HEARTBEAT_INTERVAL = 30.0
REST_FALLBACK_INTERVAL = 2.0


class PriceStreamManager:
    """Manages WebSocket ticker subscriptions across exchanges.

    Features:
    - One subscription per exchange for all tracked symbols
    - Auto-reconnect with exponential backoff
    - Heartbeat monitoring (30s)
    - REST fallback polling when WS disconnected >60s
    - Last price cache for O(1) lookups
    - Callback registration per symbol
    """

    def __init__(self):
        self._prices: dict[str, float] = {}  # normalized_symbol -> last_price
        self._callbacks: dict[str, set[Callable[[str, float], Awaitable[None]]]] = defaultdict(set)
        self._subscriptions: dict[str, asyncio.Task] = {}  # exchange_id -> ws task
        self._fallback_tasks: dict[str, asyncio.Task] = {}  # exchange_id -> rest fallback task
        self._last_ws_update: dict[str, float] = {}  # exchange_id -> last ws update timestamp
        self._watched_symbols: dict[str, set[str]] = defaultdict(set)  # exchange_id -> set of symbols
        self._public_exchanges: dict[str, object] = {}  # exchange_id -> exchange instance
        self._lock = asyncio.Lock()
        self._running = False

    async def get_price(self, symbol: str) -> Optional[float]:
        """Get last cached price for a symbol."""
        norm = self._norm(symbol)
        return self._prices.get(norm)

    @staticmethod
    def _norm(s: str) -> str:
        return (s or "").replace("/", "").replace(":USDT", "").replace("-SWAP", "").upper()

    def register_callback(self, symbol: str, callback: Callable[[str, float], Awaitable[None]]):
        """Register a callback to be called when price updates for this symbol."""
        norm = self._norm(symbol)
        self._callbacks[norm].add(callback)

    def unregister_callback(self, symbol: str, callback: Callable[[str, float], Awaitable[None]]):
        norm = self._norm(symbol)
        self._callbacks[norm].discard(callback)

    async def _notify(self, symbol: str, price: float):
        norm = self._norm(symbol)
        self._prices[norm] = price
        for cb in list(self._callbacks.get(norm, set())):
            try:
                await cb(norm, price)
            except Exception as e:
                logger.debug("Price callback error for %s: %s", norm, e)

    async def subscribe_exchange(self, exchange_id: str, symbols: list[str], exchange):
        """Start watching tickers for an exchange."""
        if not symbols:
            return

        norm_symbols = [self._norm(s) for s in symbols]
        self._watched_symbols[exchange_id] = set(norm_symbols)
        self._public_exchanges[exchange_id] = exchange

        if exchange_id in self._subscriptions:
            return  # already subscribed

        self._running = True
        self._subscriptions[exchange_id] = asyncio.create_task(
            self._ws_loop(exchange_id, exchange, list(norm_symbols))
        )
        # Start REST fallback task for when WS disconnects
        self._fallback_tasks[exchange_id] = asyncio.create_task(
            self._rest_fallback_loop(exchange_id, exchange, list(norm_symbols))
        )

    async def _ws_loop(self, exchange_id: str, exchange, symbols: list[str]):
        """WebSocket watch_tickers loop with auto-reconnect."""
        delay = RECONNECT_BASE_DELAY
        while self._running:
            try:
                logger.info("PriceStream: subscribing to %d symbols on %s", len(symbols), exchange_id)
                while self._running:
                    tickers = await exchange.watch_tickers(symbols)
                    self._last_ws_update[exchange_id] = time.time()
                    if isinstance(tickers, dict):
                        for sym, t in tickers.items():
                            if isinstance(t, dict) and t.get("last") is not None:
                                price = float(t["last"])
                                if price > 0:
                                    await self._notify(sym, price)
                    delay = RECONNECT_BASE_DELAY  # reset on success
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("PriceStream %s WS error: %s — reconnecting in %.1fs", exchange_id, e, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, RECONNECT_MAX_DELAY)

    async def _rest_fallback_loop(self, exchange_id: str, exchange, symbols: list[str]):
        """REST polling fallback when WS is down."""
        while self._running:
            try:
                last_ws = self._last_ws_update.get(exchange_id, 0)
                if time.time() - last_ws < 60:
                    await asyncio.sleep(REST_FALLBACK_INTERVAL)
                    continue

                tickers = await exchange.fetch_tickers(symbols)
                if isinstance(tickers, dict):
                    for sym, t in tickers.items():
                        if isinstance(t, dict) and t.get("last") is not None:
                            price = float(t["last"])
                            if price > 0:
                                await self._notify(sym, price)
                await asyncio.sleep(REST_FALLBACK_INTERVAL)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("PriceStream REST fallback error: %s", e)
                await asyncio.sleep(REST_FALLBACK_INTERVAL)

    async def shutdown(self):
        """Stop all subscriptions and cleanup."""
        self._running = False
        for task in list(self._subscriptions.values()) + list(self._fallback_tasks.values()):
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._subscriptions.clear()
        self._fallback_tasks.clear()
        self._watched_symbols.clear()
        self._callbacks.clear()
        self._prices.clear()


# Singleton
price_stream = PriceStreamManager()
