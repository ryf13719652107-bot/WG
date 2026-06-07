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
    def avg_fill_price_from_order(raw: dict | None) -> float:
        """从 ccxt 订单 dict 解析成交均价（开仓/平仓/日志用）。

        OKX 等 USDT 本位永续：`filled` 常为合约张数，`cost` 为成交支付/收取的计价额，
        此时 ``cost/filled`` 约等于 ``单价 × ctVal``（例如单价 0.692、ctVal≈10 会得到 6.92），
        不能排在真实均价之前。优先使用所侧 ``avgPx`` / ccxt ``average``，最后再退回 ``cost/filled``。
        """
        if not raw:
            return 0.0
        info = raw.get("info") if isinstance(raw.get("info"), dict) else {}
        for key in ("avgPx", "fillPx", "avgPrice", "ap"):
            v = info.get(key)
            if v is None or v == "":
                continue
            try:
                px = float(v)
                if px > 0:
                    return px
            except (TypeError, ValueError):
                continue
        avg = float(raw.get("average", 0) or 0)
        if avg > 0:
            return avg
        filled = float(raw.get("filled", 0) or 0)
        cost = float(raw.get("cost", 0) or 0)
        if filled > 1e-12 and cost > 0:
            v = cost / filled
            if v > 0:
                return float(v)
        for key in ("px", "price"):
            v = info.get(key)
            if v is None or v == "":
                continue
            try:
                px = float(v)
                if px > 0:
                    return px
            except (TypeError, ValueError):
                continue
        return 0.0

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

    @staticmethod
    def open_order_leg_side_lower(order: dict) -> str:
        """挂单所属持仓腿 long/short（双向模式下 info 或顶层常有 positionSide / posSide）。"""
        info = order.get("info") or {}
        if isinstance(info, dict):
            raw = str(info.get("posSide") or info.get("positionSide") or "").strip()
            if raw.upper() in ("LONG", "SHORT"):
                return "long" if raw.upper() == "LONG" else "short"
            rl = raw.lower()
            if rl in ("long", "short"):
                return rl
        for key in ("positionSide", "posSide"):
            raw = str(order.get(key) or "").strip()
            if not raw:
                continue
            if raw.upper() in ("LONG", "SHORT"):
                return "long" if raw.upper() == "LONG" else "short"
            rl = raw.lower()
            if rl in ("long", "short"):
                return rl
        return ""

    @staticmethod
    def open_order_matches_strategy_leg(order: dict, direction_lower: str, hedge_mode: bool) -> bool:
        """双向持仓下同币种对向策略并行时，仅匹配本报单方向的挂单，避免误撤对方挂单。"""
        if not hedge_mode:
            return True
        want = (direction_lower or "").strip().lower()
        if want not in ("long", "short"):
            return True
        leg = BaseExchangeService.open_order_leg_side_lower(order)
        if not leg:
            return False
        return leg == want

    @staticmethod
    def open_order_matches_strategy_symbol(order: dict, strategy_symbol: str) -> bool:
        """挂单是否属于策略运行的合约（防御：接口偶发混入其它交易对时不撤）。"""
        osym = str(order.get("symbol") or "").strip()
        if not osym:
            return True
        return BaseExchangeService._norm_sym(osym) == BaseExchangeService._norm_sym(strategy_symbol)

    @staticmethod
    def open_order_matches_strategy_scope(
        order: dict,
        strategy_symbol: str,
        direction_lower: str,
        hedge_mode: bool,
    ) -> bool:
        """仅撤销当前策略合约 + 同持仓方向的交易所挂单（币种 ∩ 方向）。"""
        if not BaseExchangeService.open_order_matches_strategy_symbol(order, strategy_symbol):
            return False
        return BaseExchangeService.open_order_matches_strategy_leg(order, direction_lower, hedge_mode)

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

    async def normalize_limit_price(
        self, symbol: str, side: str, price: float,
    ) -> tuple[float, Optional[str]]:
        """将限价钳在交易所允许的价格带内。返回 (价格, 调整说明或 None)。"""
        if price <= 0:
            return 0.0, None
        return float(price), None

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

    async def min_order_amount(self, symbol: str) -> float:
        """交易所该品种最小下单量（张数/合约数）。未知时返回 0。"""
        return 0.0

    async def resolve_order_qty_from_usdt_or_min(
        self, symbol: str, quote_usdt: float, ref_price: float,
    ) -> tuple[float, str]:
        """按基础 USDT 换算下单量；若低于最小量则用最小量。

        返回 (数量, 'base_usdt' | 'exchange_min' | 'invalid')。
        """
        if quote_usdt <= 0 or ref_price <= 0:
            return 0.0, "invalid"
        qty_base = await self.quote_usdt_to_order_amount(symbol, quote_usdt, ref_price)
        min_qty = await self.min_order_amount(symbol)
        if min_qty <= 0:
            q = await self.normalize_order_amount(symbol, qty_base)
            return q, "base_usdt"
        if qty_base >= min_qty:
            q = await self.normalize_order_amount(symbol, qty_base)
            return q, "base_usdt"
        q = await self.normalize_order_amount(symbol, min_qty)
        return q, "exchange_min"

    async def linear_contract_ct_val(self, symbol: str) -> float:
        """线性 USDT 永续：未实现盈亏对价格的敏感度里，每张合约的「基础数量」系数（OKX/BN 多为 ctVal/contractSize）。

        近似 USDT 盈亏变化 ≈ (标记价 − 开仓价) × 张数 × 该系数（多仓）。未知时返回 1.0，与仅按张数估算的旧逻辑一致。
        """
        return 1.0

    async def fetch_open_algo_orders(self, symbol: str) -> list:
        """Open conditional (algo) orders for this symbol. Default empty; Binance USD-M overrides."""
        return []

    async def fetch_algo_order(self, algo_id: str, symbol: str) -> dict:
        """Fetch a conditional/algo order by id. Default falls back to fetch_order."""
        return await self.fetch_order(algo_id, symbol)

    # ---- WebSocket (Public) ----

    @abstractmethod
    async def watch_tickers(self, symbols: Optional[list[str]] = None):
        """WebSocket ticker stream. Must be iterable (async generator)."""

    # ---- Lifecycle ----

    @abstractmethod
    async def close(self):
        """Close exchange connections."""
