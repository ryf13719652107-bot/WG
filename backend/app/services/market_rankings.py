"""USDT-M perpetual 24h decline rankings (top losers)."""
from __future__ import annotations

import logging
import time
from typing import Optional

from .exchange_base import BaseExchangeService
from .exchange_factory import get_public_exchange

logger = logging.getLogger(__name__)

_CACHE: dict[str, tuple[float, list[dict]]] = {}
_CACHE_TTL_SEC = 60.0


def _parse_percentage(ticker: dict) -> Optional[float]:
    """Parse 24h change % from ccxt ticker; fallback to (last-open)/open."""
    pct = ticker.get("percentage")
    if pct is not None:
        try:
            return float(pct)
        except (TypeError, ValueError):
            pass
    try:
        last = float(ticker.get("last") or ticker.get("close") or 0)
        open_p = float(ticker.get("open") or 0)
        if open_p > 0 and last > 0:
            return (last - open_p) / open_p * 100.0
    except (TypeError, ValueError):
        pass
    return None


def rank_tickers_to_losers(
    tickers: dict,
    perp_set: set[str],
    *,
    limit: int = 10,
    min_quote_volume: float = 0.0,
) -> list[dict]:
    """Pure ranking helper (testable without exchange I/O)."""
    rows: list[dict] = []
    for key, t in (tickers or {}).items():
        if not isinstance(t, dict):
            continue
        norm = BaseExchangeService._norm_sym(str(t.get("symbol") or key))
        if not norm or norm not in perp_set:
            continue
        pct = _parse_percentage(t)
        if pct is None:
            continue
        try:
            qv = float(t.get("quoteVolume") or 0)
        except (TypeError, ValueError):
            qv = 0.0
        if min_quote_volume > 0 and qv < min_quote_volume:
            continue
        try:
            last = float(t.get("last") or t.get("close") or 0)
        except (TypeError, ValueError):
            last = 0.0
        rows.append(
            {
                "symbol": norm,
                "price_change_pct": pct,
                "last": last,
                "quote_volume_24h": qv,
                "timestamp": t.get("timestamp"),
            }
        )
    rows.sort(key=lambda r: r["price_change_pct"])
    return rows[: max(1, int(limit))]


async def fetch_top_losers(
    exchange: str,
    *,
    limit: int = 10,
    min_quote_volume: float = 0.0,
    use_cache: bool = True,
) -> list[dict]:
    """Fetch USDT-M perpetual top-N 24h losers for binance|okx."""
    ex_id = (exchange or "binance").strip().lower()
    cache_key = f"{ex_id}:{limit}:{min_quote_volume}"
    now = time.monotonic()
    if use_cache and cache_key in _CACHE:
        ts, data = _CACHE[cache_key]
        if now - ts < _CACHE_TTL_SEC:
            return list(data)

    pub = await get_public_exchange(ex_id)
    if not pub:
        raise RuntimeError(f"无法创建公开行情客户端: {ex_id}")

    perp_list = await pub.list_usdt_perp_symbols()
    perp_set = set(perp_list)
    tickers = await pub.fetch_tickers()
    ranked = rank_tickers_to_losers(
        tickers,
        perp_set,
        limit=limit,
        min_quote_volume=min_quote_volume,
    )
    for row in ranked:
        row["exchange"] = ex_id
    _CACHE[cache_key] = (now, ranked)
    logger.info(
        "top losers %s limit=%d -> %s",
        ex_id,
        limit,
        [f"{r['symbol']}({r['price_change_pct']:.2f}%)" for r in ranked[:5]],
    )
    return list(ranked)


def clear_rankings_cache() -> None:
    _CACHE.clear()
