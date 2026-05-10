"""Binance USDM Futures exchange service — refactored to inherit BaseExchangeService."""
import time
import logging
from typing import Optional
import ccxt.async_support as ccxt_async
import ccxt.pro as ccxtpro

from .exchange_base import BaseExchangeService, retry_with_backoff

logger = logging.getLogger(__name__)


class BinanceService(BaseExchangeService):
    """Wrapper around ccxt binanceusdm (USD-M Futures)."""

    exchange_id = "binance"

    def __init__(self, api_key: str = "", secret: str = "", testnet: bool = True, hedge_mode: bool = True):
        super().__init__(api_key, secret, testnet, hedge_mode)
        self._exchange: Optional[ccxt_async.binanceusdm] = None
        self._ws_exchange: Optional[ccxtpro.binanceusdm] = None
        self._created_at: float = time.time()
        self._ttl_seconds = 1800

    def _is_expired(self) -> bool:
        return (time.time() - self._created_at) > self._ttl_seconds

    @property
    def exchange(self) -> ccxt_async.binanceusdm:
        if self._exchange is None or self._is_expired():
            self._recreate()
        return self._exchange

    @property
    def ws_exchange(self) -> ccxtpro.binanceusdm:
        if self._ws_exchange is None or self._is_expired():
            self._recreate()
        return self._ws_exchange

    def _recreate(self):
        old_ex = self._exchange
        old_ws = self._ws_exchange
        self._exchange = None
        self._ws_exchange = None
        import asyncio
        try:
            loop = asyncio.get_running_loop()
            if old_ex:
                loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._safe_close(old_ex)))
            if old_ws:
                loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._safe_close(old_ws)))
        except RuntimeError:
            pass
        self._exchange = self._create_exchange(False)
        self._ws_exchange = self._create_exchange(True)
        self._created_at = time.time()
        logger.info("BinanceService TTL expired, recreated exchange instances")

    async def _safe_close(self, ex):
        try:
            await ex.close()
        except Exception:
            pass

    def _create_exchange(self, pro: bool = False):
        from ..config import settings

        cls = ccxtpro.binanceusdm if pro else ccxt_async.binanceusdm
        config = {
            "apiKey": self.api_key,
            "secret": self.secret,
            "enableRateLimit": True,
            "options": {"defaultType": "future"},
        }
        if settings.http_proxy:
            config["proxies"] = {"http": settings.http_proxy, "https": settings.http_proxy}

        exchange = cls(config)
        if self.testnet:
            exchange.set_sandbox_mode(True)
        return exchange

    # ---- Symbol formatting ----

    @staticmethod
    def _format_symbol(symbol: str) -> str:
        """BTCUSDT -> BTC/USDT:USDT (ccxt unified format for Binance USDM)."""
        if "/" in symbol:
            if ":USDT" not in symbol and symbol.endswith("/USDT"):
                return f"{symbol}:USDT"
            return symbol
        if symbol.endswith("USDT"):
            base = symbol[:-4]
            return f"{base}/USDT:USDT"
        return symbol

    # ---- Market Data (Public) ----

    async def fetch_balance(self) -> dict:
        return await retry_with_backoff(
            "binance.fetch_balance",
            lambda: self.exchange.fetch_balance(),
        )

    async def fetch_ticker(self, symbol: str) -> dict:
        return await retry_with_backoff(
            "binance.fetch_ticker",
            lambda: self.exchange.fetch_ticker(self._format_symbol(symbol)),
        )

    async def fetch_tickers(self, symbols: Optional[list[str]] = None) -> dict:
        formatted = [self._format_symbol(s) for s in symbols] if symbols else None
        return await retry_with_backoff(
            "binance.fetch_tickers",
            lambda: self.exchange.fetch_tickers(formatted),
        )

    async def fetch_positions(self, symbols: Optional[list[str]] = None) -> list[dict]:
        if not symbols:
            return await retry_with_backoff(
                "binance.fetch_positions(all)",
                lambda: self.exchange.fetch_positions(None),
            )
        formatted = [self._format_symbol(s) for s in symbols]
        try:
            return await retry_with_backoff(
                "binance.fetch_positions",
                lambda: self.exchange.fetch_positions(formatted),
            )
        except Exception as e:
            msg = str(e).lower()
            if "does not have market symbol" in msg or "marketsymbol" in msg or "invalid symbol" in msg:
                try:
                    await self.exchange.load_markets(True)
                    return await self.exchange.fetch_positions(formatted)
                except Exception as e2:
                    logger.debug("fetch_positions(%s) after load_markets still failed: %s", symbols, e2)
                logger.warning("fetch_positions(%s) symbol missing, fallback fetch all: %s", symbols, e)
                raw = await self.exchange.fetch_positions(None)
                want = {self._norm_sym(s) for s in symbols}
                out = []
                for p in raw:
                    sym = self._norm_sym(p.get("symbol") or "")
                    if sym in want:
                        out.append(p)
                return out
            raise

    async def fetch_leverage(self, symbol: str) -> float:
        try:
            raw_sym = self._norm_sym(symbol)
            response = await self.exchange.fapiPrivate_get_leverage({"symbol": raw_sym})
            return float(response.get("leverage", 20))
        except Exception:
            return 20.0

    async def fetch_order(self, order_id: str, symbol: str) -> dict:
        return await retry_with_backoff(
            "binance.fetch_order",
            lambda: self.exchange.fetch_order(order_id, self._format_symbol(symbol)),
        )

    async def fetch_open_orders(self, symbol: Optional[str] = None) -> list[dict]:
        fmt = self._format_symbol(symbol) if symbol else None
        return await retry_with_backoff(
            "binance.fetch_open_orders",
            lambda: self.exchange.fetch_open_orders(fmt),
        )

    async def fetch_order_book(self, symbol: str, limit: int = 20) -> dict:
        return await retry_with_backoff(
            "binance.fetch_order_book",
            lambda: self.exchange.fetch_order_book(self._format_symbol(symbol), limit),
        )

    async def fetch_markets(self) -> list:
        raw = await retry_with_backoff(
            "binance.fetch_markets",
            lambda: self.exchange.load_markets(True),
        )
        if isinstance(raw, dict):
            return list(raw.values())
        return raw or []

    # ---- Orders (Private) ----

    def _order_params(self, position_side: str, reduce_only: bool = False) -> dict:
        """Build params dict. positionSide and reduceOnly only sent in hedge mode."""
        params: dict = {}
        if self.hedge_mode:
            params["positionSide"] = position_side
            if reduce_only:
                params["reduceOnly"] = True
        return params

    async def create_market_order(
        self, symbol: str, side: str, amount: float,
        reduce_only: bool = False, position_side: str = "LONG",
    ) -> dict:
        formatted = self._format_symbol(symbol)
        return await retry_with_backoff(
            "binance.create_market_order",
            lambda: self.exchange.create_order(
                symbol=formatted,
                type="market",
                side=side,
                amount=amount,
                params=self._order_params(position_side, reduce_only),
            ),
        )

    async def create_limit_order(
        self, symbol: str, side: str, amount: float, price: float,
        reduce_only: bool = False, position_side: str = "LONG",
    ) -> dict:
        return await retry_with_backoff(
            "binance.create_limit_order",
            lambda: self.exchange.create_order(
                symbol=self._format_symbol(symbol),
                type="limit",
                side=side,
                amount=amount,
                price=price,
                params=self._order_params(position_side, reduce_only),
            ),
        )

    async def cancel_order(self, order_id: str, symbol: str) -> dict:
        return await retry_with_backoff(
            "binance.cancel_order",
            lambda: self.exchange.cancel_order(order_id, self._format_symbol(symbol)),
        )

    async def close_position(self, symbol: str, side: str) -> dict:
        """Close all positions for symbol+side. Handles hedge mode with multiple entries."""
        formatted = self._format_symbol(symbol)
        positions = await self.fetch_positions([symbol])
        position_side = "LONG" if side == "long" else "SHORT"

        total_contracts = 0.0
        for pos in positions:
            pos_side_ex = (pos.get("side") or "").lower()
            if pos.get("symbol") == formatted and pos_side_ex == side.lower() and float(pos.get("contracts", 0)) > 0:
                total_contracts += float(pos.get("contracts"))

        if total_contracts <= 0:
            logger.warning("close_position: no contracts for %s %s", symbol, side)
            return {}

        close_side = "sell" if side == "long" else "buy"
        try:
            return await self.create_market_order(
                symbol, close_side, total_contracts,
                reduce_only=True, position_side=position_side,
            )
        except Exception as e:
            if "-1106" in str(e):
                return await self.create_market_order(
                    symbol, close_side, total_contracts,
                    reduce_only=False, position_side=position_side,
                )
            raise

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        """Set leverage for a symbol on Binance USDM Futures."""
        formatted = self._format_symbol(symbol)
        try:
            await retry_with_backoff(
                "binance.set_leverage",
                lambda: self.exchange.set_leverage(leverage, formatted),
            )
        except Exception as e:
            logger.warning("Binance set_leverage(%s, %d) failed: %s", symbol, leverage, e)

    # ---- WebSocket ----

    async def watch_tickers(self, symbols: Optional[list[str]] = None):
        formatted = [self._format_symbol(s) for s in symbols] if symbols else None
        return await self.ws_exchange.watch_tickers(formatted)

    # ---- Lifecycle ----

    async def close(self):
        if self._exchange:
            try:
                await self._exchange.close()
            except Exception:
                pass
            self._exchange = None
        if self._ws_exchange:
            try:
                await self._ws_exchange.close()
            except Exception:
                pass
            self._ws_exchange = None
