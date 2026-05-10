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
        """Normalize any symbol format to plain uppercase: BTC/USDT:USDT -> BTCUSDT.

        Also folds OKX/展示用的「BASE-QUOTE」写法（如 SUI-USDT）为 BASEQUOTE，避免
        SUI-USDT 被误当成以 USDT 结尾而切片出 SUI-，进而得到非法 ccxt 符号 SUI-/USDT:USDT。
        """
        s = (symbol or "").strip().upper()
        s = s.replace("/", "").replace(":USDT", "")
        s = s.replace("-SWAP", "")
        s = s.replace("-", "")
        return s

    @staticmethod
    def position_row_matches_leg(
        pos: dict, symbol: str, side_lower: str, formatted_symbol: str
    ) -> bool:
        """判断 ccxt 返回的持仓行是否属于给定交易对与方向（兼容 hedge 下 side 在 info.posSide / positionSide）。"""
        psym = BaseExchangeService._norm_sym(str(pos.get("symbol") or ""))
        want = BaseExchangeService._norm_sym(symbol)
        if psym != want and (pos.get("symbol") or "") != formatted_symbol:
            return False
        p_side = BaseExchangeService.position_row_side_lower(pos)
        return p_side == (side_lower or "").lower()

    @staticmethod
    def position_row_side_lower(pos: dict) -> str:
        """ccxt 持仓行的多空方向；OKX 等常在 info.posSide / positionSide。"""
        s = (pos.get("side") or "").strip().lower()
        if s in ("long", "short"):
            return s
        info = pos.get("info") or {}
        if isinstance(info, dict):
            raw = str(info.get("posSide") or info.get("positionSide") or "").strip().lower()
            if raw in ("long", "short"):
                return raw
        return ""

    @staticmethod
    def position_row_contracts_abs(pos: dict) -> float:
        """持仓张数/数量；部分所对空头为负 contracts，统一取绝对值。"""
        return abs(float(pos.get("contracts", 0) or 0))

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
    async def cancel_algo_order(self, algo_id: str, symbol: str) -> dict:
        """Cancel an Algo Order (conditional orders like STOP_MARKET)."""
        ...

    @abstractmethod
    async def close_position(self, symbol: str, side: str) -> dict:
        """Close all positions for symbol+side. Returns order dict."""

    @abstractmethod
    async def set_leverage(self, symbol: str, leverage: int) -> None:
        """Set leverage for a symbol on the exchange."""

    async def normalize_order_amount(self, symbol: str, amount: float) -> float:
        """Contracts amount stepped to exchange lot size (stop-loss math uses order qty).

        Override on concrete exchanges via ccxt ``amount_to_precision`` (after ``load_markets``).
        """
        try:
            return float(amount)
        except (TypeError, ValueError):
            return 0.0

    async def quote_usdt_to_order_amount(self, symbol: str, quote_usdt: float, ref_price: float) -> float:
        """将「约多少 USDT 名义」换算为下单 amount。默认按现货式 qty=U/价；永续合约所（如 OKX）应覆写为张数。"""
        if quote_usdt <= 0 or ref_price <= 0:
            return 0.0
        return float(quote_usdt) / float(ref_price)

    async def fetch_open_algo_orders(self, symbol: str) -> list:
        """Open conditional (algo) orders for this symbol. Default empty; Binance USD-M overrides."""
        return []

    # ---- WebSocket (Public) ----

    @abstractmethod
    async def watch_tickers(self, symbols: Optional[list[str]] = None):
        """WebSocket ticker stream. Must be iterable (async generator)."""

    # ---- Lifecycle ----

    @abstractmethod
    async def close(self):
        """Close exchange connections."""
