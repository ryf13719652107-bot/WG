"""Abstract base class for exchange services. All exchange implementations must inherit this."""
import asyncio
import logging
import time
from abc import ABC, abstractmethod
from typing import Optional

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
BASE_DELAY = 1.0
MAX_DELAY = 30.0


async def retry_with_backoff(operation_name: str, coro_factory, max_retries: int = MAX_RETRIES):
    """Execute an async operation with exponential backoff retry.

    coro_factory is a callable that returns a new coroutine on each attempt.
    """
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            return await coro_factory()
        except Exception as e:
            last_exc = e
            if attempt < max_retries:
                delay = min(BASE_DELAY * (2 ** attempt), MAX_DELAY)
                logger.warning(
                    "%s failed (attempt %d/%d): %s — retrying in %.1fs",
                    operation_name, attempt + 1, max_retries + 1, e, delay,
                )
                await asyncio.sleep(delay)
            else:
                logger.error(
                    "%s failed after %d retries: %s",
                    operation_name, max_retries + 1, e,
                )
    raise last_exc


class BaseExchangeService(ABC):
    """Abstract exchange service. All exchange implementations inherit this."""

    exchange_id: str = ""

    def __init__(self, api_key: str = "", secret: str = "", testnet: bool = True, hedge_mode: bool = True):
        self.api_key = api_key
        self.secret = secret
        self.testnet = testnet
        self.hedge_mode = hedge_mode

    # ---- Symbol formatting (override per exchange) ----

    @abstractmethod
    def _format_symbol(self, symbol: str) -> str:
        """Convert internal symbol (e.g. 'BTCUSDT') to exchange-specific format."""

    @staticmethod
    def _norm_sym(symbol: str) -> str:
        """Normalize any symbol format to plain uppercase: BTC/USDT:USDT -> BTCUSDT."""
        s = (symbol or "").replace("/", "").replace(":USDT", "").replace("-SWAP", "").upper()
        return s

    # ---- Market Data (Public) ----

    @abstractmethod
    async def fetch_balance(self) -> dict:
        ...

    @abstractmethod
    async def fetch_ticker(self, symbol: str) -> dict:
        ...

    @abstractmethod
    async def fetch_tickers(self, symbols: Optional[list[str]] = None) -> dict:
        ...

    @abstractmethod
    async def fetch_positions(self, symbols: Optional[list[str]] = None) -> list[dict]:
        ...

    @abstractmethod
    async def fetch_leverage(self, symbol: str) -> float:
        ...

    @abstractmethod
    async def fetch_order(self, order_id: str, symbol: str) -> dict:
        ...

    @abstractmethod
    async def fetch_open_orders(self, symbol: Optional[str] = None) -> list[dict]:
        ...

    @abstractmethod
    async def fetch_order_book(self, symbol: str, limit: int = 20) -> dict:
        ...

    @abstractmethod
    async def fetch_markets(self) -> list:
        """Fetch all available markets from the exchange. Returns list of market dicts."""

    # ---- Orders (Private) ----

    @abstractmethod
    async def create_market_order(
        self, symbol: str, side: str, amount: float,
        reduce_only: bool = False, position_side: str = "LONG",
    ) -> dict:
        ...

    @abstractmethod
    async def create_limit_order(
        self, symbol: str, side: str, amount: float, price: float,
        reduce_only: bool = False, position_side: str = "LONG",
    ) -> dict:
        ...

    @abstractmethod
    async def create_stop_loss_order(
        self, symbol: str, side: str, amount: float, stop_price: float,
        reduce_only: bool = True, position_side: str = "LONG",
    ) -> dict:
        """Create a stop-loss market order that triggers at stop_price."""
        ...

    @abstractmethod
    async def cancel_order(self, order_id: str, symbol: str) -> dict:
        ...

    @abstractmethod
    async def close_position(self, symbol: str, side: str) -> dict:
        """Close all positions for symbol+side. Returns order dict."""

    @abstractmethod
    async def set_leverage(self, symbol: str, leverage: int) -> None:
        """Set leverage for a symbol on the exchange."""

    # ---- WebSocket (Public) ----

    @abstractmethod
    async def watch_tickers(self, symbols: Optional[list[str]] = None):
        """WebSocket ticker stream. Must be iterable (async generator)."""

    # ---- Lifecycle ----

    @abstractmethod
    async def close(self):
        """Close exchange connections."""
