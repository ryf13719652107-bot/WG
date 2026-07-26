"""Detect TradFi / stock-type USDT perpetual contracts (Binance STOCK, OKX equity perps)."""
from __future__ import annotations

from typing import Iterable, Optional

# 常见美股/ETF 合成永续 base（无 API 标签时的兜底；故意偏保守，避免误伤主流加密币）
KNOWN_STOCK_BASES: frozenset[str] = frozenset({
    # Mag7 / 大盘
    "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "META", "NVDA", "TSLA",
    # 热门个股 / 券商 / 矿企股
    "COIN", "MSTR", "HOOD", "PLTR", "CRCL", "ARM", "CRWD", "SNOW", "NET",
    "DDOG", "ZM", "ROKU", "PATH", "AI", "SOUN", "SMCI", "DELL", "IBM",
    "INTC", "AMD", "AVGO", "ORCL", "CSCO", "MU", "SNDK", "WDC", "COHR",
    "NFLX", "COST", "EBAY", "HIMS", "BE", "NBIS", "OPEN", "GME", "AMC",
    "DJT", "SNAP", "UBER", "LYFT", "SHOP", "BABA", "JD", "PDD", "NIO",
    "XPEV", "LI", "RIVN", "LCID", "SOFI", "MARA", "RIOT", "CLSK",
    # ETF
    "QQQ", "SPY", "URNM", "XLE", "TQQQ", "SOXL",
})

_STOCK_HINTS = ("STOCK", "EQUITY", "TRADFI")


def _norm_base_from_symbol(symbol: str) -> str:
    s = (symbol or "").strip().upper()
    s = s.replace("/", "").replace(":USDT", "").replace("-SWAP", "").replace("-", "")
    if s.endswith("USDT") and len(s) > 4:
        return s[:-4]
    return s


def is_stock_type_symbol(symbol: str) -> bool:
    """按归一化符号/base 判断是否为已知股票型合约。"""
    base = _norm_base_from_symbol(symbol)
    return bool(base) and base in KNOWN_STOCK_BASES


def is_stock_type_market(m: dict) -> bool:
    """从 ccxt market（含 info）判断是否为股票/TradFi 永续。"""
    if not isinstance(m, dict):
        return False
    info = m.get("info") if isinstance(m.get("info"), dict) else {}
    for key in (
        "underlyingType",
        "underlyingSubType",
        "category",
        "ruleType",
        "contractType",
        "instFamily",
    ):
        raw = info.get(key)
        if raw is None:
            raw = m.get(key)
        val = str(raw or "").upper()
        if any(h in val for h in _STOCK_HINTS):
            return True

    base = str(m.get("base") or "").upper().replace("-", "").replace("/", "")
    if base in KNOWN_STOCK_BASES:
        return True

    sym = str(m.get("id") or m.get("symbol") or "")
    if is_stock_type_symbol(sym):
        return True
    return False


def filter_stock_symbols(
    symbols: Iterable[str],
    markets: Optional[Iterable[dict]] = None,
) -> list[str]:
    """去掉股票型合约，保留加密货币 USDT 永续。"""
    stock_from_markets: set[str] = set()
    if markets is not None:
        from .exchange_base import BaseExchangeService

        for m in markets:
            if not is_stock_type_market(m):
                continue
            norm = BaseExchangeService._market_is_usdt_perp(m, permissive=True)
            if norm:
                stock_from_markets.add(norm)
            # 即使 _market_is_usdt_perp 未命中，也按 id/symbol 归一化记入
            raw = str((m or {}).get("id") or (m or {}).get("symbol") or "")
            if raw:
                n2 = BaseExchangeService._norm_sym(raw)
                if n2.endswith("USDT"):
                    stock_from_markets.add(n2)

    out: list[str] = []
    seen: set[str] = set()
    for sym in symbols:
        from .exchange_base import BaseExchangeService

        norm = BaseExchangeService._norm_sym(str(sym))
        if not norm or norm in seen:
            continue
        if norm in stock_from_markets or is_stock_type_symbol(norm):
            continue
        seen.add(norm)
        out.append(norm)
    return out
