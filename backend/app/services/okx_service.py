"""OKX exchange implementation via ccxt."""
import logging
from typing import Optional
import ccxt.async_support as ccxt_async

from .exchange_base import BaseExchangeService, retry_with_backoff

logger = logging.getLogger(__name__)


class OkxService(BaseExchangeService):
    """OKX USD-M Futures exchange wrapper."""

    exchange_id = "okx"

    def __init__(self, api_key: str = "", secret: str = "", testnet: bool = True, hedge_mode: bool = True):
        super().__init__(api_key, secret, testnet, hedge_mode)
        self._exchange: Optional[ccxt_async.okx] = None
        self._passphrase = ""  # OKX requires passphrase

    def set_passphrase(self, passphrase: str):
        self._passphrase = passphrase

    @property
    def exchange(self) -> ccxt_async.okx:
        if self._exchange is None:
            self._exchange = self._create_exchange()
        return self._exchange

    def _create_exchange(self):
        from ..config import settings

        config = {
            "apiKey": self.api_key,
            "secret": self.secret,
            "password": self._passphrase,
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
        }
        if settings.http_proxy:
            config["proxies"] = {"http": settings.http_proxy, "https": settings.http_proxy}

        ex = ccxt_async.okx(config)
        if self.testnet:
            ex.set_sandbox_mode(True)
        return ex

    # ---- Symbol formatting ----

    def _format_symbol(self, symbol: str) -> str:
        """BTCUSDT -> BTC/USDT:USDT (ccxt unified)"""
        s = self._norm_sym(symbol)
        if "/" in symbol:
            return symbol
        if s.endswith("USDT"):
            base = s[:-4]
            return f"{base}/USDT:USDT"
        return symbol

    def _to_okx_inst_id(self, symbol: str) -> str:
        """BTCUSDT -> BTC-USDT-SWAP"""
        s = self._norm_sym(symbol)
        if s.endswith("USDT"):
            base = s[:-4]
            return f"{base}-USDT-SWAP"
        return s

    # ---- Market Data ----

    async def fetch_balance(self) -> dict:
        return await retry_with_backoff(
            "okx.fetch_balance",
            lambda: self.exchange.fetch_balance(),
        )

    async def fetch_ticker(self, symbol: str) -> dict:
        return await retry_with_backoff(
            "okx.fetch_ticker",
            lambda: self.exchange.fetch_ticker(self._format_symbol(symbol)),
        )

    async def fetch_tickers(self, symbols: Optional[list[str]] = None) -> dict:
        formatted = [self._format_symbol(s) for s in symbols] if symbols else None
        return await retry_with_backoff(
            "okx.fetch_tickers",
            lambda: self.exchange.fetch_tickers(formatted),
        )

    async def fetch_positions(self, symbols: Optional[list[str]] = None) -> list[dict]:
        formatted = [self._format_symbol(s) for s in symbols] if symbols else None
        return await retry_with_backoff(
            "okx.fetch_positions",
            lambda: self.exchange.fetch_positions(formatted),
        )

    async def fetch_leverage(self, symbol: str) -> float:
        try:
            positions = await self.fetch_positions([symbol])
            for p in positions:
                if p.get("symbol") == self._format_symbol(symbol):
                    return float(p.get("leverage", 20))
        except Exception:
            pass
        return 20.0

    async def fetch_order(self, order_id: str, symbol: str) -> dict:
        return await retry_with_backoff(
            "okx.fetch_order",
            lambda: self.exchange.fetch_order(order_id, self._format_symbol(symbol)),
        )

    async def fetch_open_orders(self, symbol: Optional[str] = None) -> list[dict]:
        fmt = self._format_symbol(symbol) if symbol else None
        return await retry_with_backoff(
            "okx.fetch_open_orders",
            lambda: self.exchange.fetch_open_orders(fmt),
        )

    async def fetch_order_book(self, symbol: str, limit: int = 20) -> dict:
        return await retry_with_backoff(
            "okx.fetch_order_book",
            lambda: self.exchange.fetch_order_book(self._format_symbol(symbol), limit),
        )

    async def fetch_markets(self) -> list:
        raw = await retry_with_backoff(
            "okx.fetch_markets",
            lambda: self.exchange.load_markets(True),
        )
        if isinstance(raw, dict):
            return list(raw.values())
        return raw or []

    async def normalize_order_amount(self, symbol: str, amount: float) -> float:
        if amount <= 0:
            return 0.0
        formatted = self._format_symbol(symbol)
        try:
            await self.exchange.load_markets()
            step = float(self.exchange.amount_to_precision(formatted, amount))
            return max(0.0, step)
        except Exception as e:
            logger.warning("OKX normalize_order_amount(%s): %s", symbol, e)
            return float(amount)

    # ---- Orders ----

    def _order_params(self, position_side: str, reduce_only: bool = False) -> dict:
        params: dict = {}
        if self.hedge_mode:
            # OKX uses posSide in hedge mode
            pos_side = "long" if position_side.upper() == "LONG" else "short"
            params["posSide"] = pos_side
            if reduce_only:
                params["reduceOnly"] = True
        if not self.hedge_mode and reduce_only:
            params["reduceOnly"] = True
        return params

    async def create_market_order(
        self, symbol: str, side: str, amount: float,
        reduce_only: bool = False, position_side: str = "LONG",
    ) -> dict:
        return await retry_with_backoff(
            "okx.create_market_order",
            lambda: self.exchange.create_order(
                symbol=self._format_symbol(symbol),
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
            "okx.create_limit_order",
            lambda: self.exchange.create_order(
                symbol=self._format_symbol(symbol),
                type="limit",
                side=side,
                amount=amount,
                price=price,
                params=self._order_params(position_side, reduce_only),
            ),
        )

    async def create_stop_loss_order(
        self, symbol: str, side: str, amount: float, stop_price: float,
        reduce_only: bool = True, position_side: str = "LONG",
    ) -> dict:
        """Create a stop-loss market order for OKX."""
        params = self._order_params(position_side, reduce_only)
        params["stopPrice"] = stop_price
        params["triggerPrice"] = stop_price
        return await retry_with_backoff(
            "okx.create_stop_loss_order",
            lambda: self.exchange.create_order(
                symbol=self._format_symbol(symbol),
                type="market",
                side=side,
                amount=amount,
                params=params,
            ),
        )

    async def cancel_order(self, order_id: str, symbol: str) -> dict:
        return await retry_with_backoff(
            "okx.cancel_order",
            lambda: self.exchange.cancel_order(order_id, self._format_symbol(symbol)),
        )

    async def cancel_algo_order(self, algo_id: str, symbol: str) -> dict:
        """OKX uses same cancel endpoint for all orders."""
        return await self.cancel_order(algo_id, symbol)

    async def close_position(self, symbol: str, side: str) -> dict:
        formatted = self._format_symbol(symbol)
        positions = await self.fetch_positions([symbol])
        total = 0.0
        for pos in positions:
            p_side = (pos.get("side") or "").lower()
            if pos.get("symbol") == formatted and p_side == side.lower():
                total += float(pos.get("contracts", 0) or 0)

        if total <= 0:
            logger.warning("OKX close_position: no contracts for %s %s", symbol, side)
            return {}

        close_side = "sell" if side == "long" else "buy"
        position_side = "LONG" if side == "long" else "SHORT"
        return await self.create_market_order(
            symbol, close_side, total,
            reduce_only=True, position_side=position_side,
        )

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        """Set leverage for a symbol on OKX."""
        formatted = self._format_symbol(symbol)
        try:
            await retry_with_backoff(
                "okx.set_leverage",
                lambda: self.exchange.set_leverage(leverage, formatted, params={"mgnMode": "isolated"}),
            )
        except Exception as e:
            try:
                await retry_with_backoff(
                    "okx.set_leverage_cross",
                    lambda: self.exchange.set_leverage(leverage, formatted, params={"mgnMode": "cross"}),
                )
            except Exception as e2:
                logger.warning("OKX set_leverage(%s, %d) failed: %s / %s", symbol, leverage, e, e2)

    # ---- WebSocket ----

    async def watch_tickers(self, symbols: Optional[list[str]] = None):
        import ccxt.pro as ccxtpro
        if not hasattr(self, '_ws_exchange') or self._ws_exchange is None:
            self._ws_exchange = self._create_ws_exchange()
        formatted = [self._format_symbol(s) for s in symbols] if symbols else None
        return await self._ws_exchange.watch_tickers(formatted)

    def _create_ws_exchange(self):
        from ..config import settings
        import ccxt.pro as ccxtpro

        config = {
            "apiKey": self.api_key,
            "secret": self.secret,
            "password": self._passphrase,
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
        }
        if settings.http_proxy:
            config["proxies"] = {"http": settings.http_proxy, "https": settings.http_proxy}

        ex = ccxtpro.okx(config)
        if self.testnet:
            ex.set_sandbox_mode(True)
        return ex

    async def close(self):
        if self._exchange:
            try:
                await self._exchange.close()
            except Exception:
                pass
            self._exchange = None
        if hasattr(self, '_ws_exchange') and self._ws_exchange:
            try:
                await self._ws_exchange.close()
            except Exception:
                pass
            self._ws_exchange = None
