"""交易对列表：交易所公开 HTTP + 本地兜底（ccxt 失败时）。"""
import logging

import httpx

from ..config import settings
from .exchange_base import BaseExchangeService

logger = logging.getLogger(__name__)

FALLBACK_USDT_PERP: list[str] = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT",
    "UNIUSDT", "ATOMUSDT", "LTCUSDT", "ETCUSDT",
    "OPUSDT", "ARBUSDT", "FILUSDT", "APTUSDT", "NEARUSDT",
    "SUIUSDT", "PEPEUSDT", "WIFUSDT", "SEIUSDT", "GPSUSDT",
    "LABUSDT", "RIVERUSDT", "1INCHUSDT", "AAVEUSDT", "ALGOUSDT",
    "APEUSDT", "ARUSDT", "TRUMPUSDT", "FETUSDT", "INJUSDT",
    "TIAUSDT", "RENDERUSDT", "WLDUSDT", "ENAUSDT", "ONDOUSDT",
]


async def _http_get_json(url: str) -> dict:
    proxy = settings.http_proxy or None
    async with httpx.AsyncClient(proxy=proxy, timeout=30.0) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.json()


async def fetch_binance_usdm_perp_symbols() -> list[str]:
    try:
        data = await _http_get_json("https://fapi.binance.com/fapi/v1/exchangeInfo")
        out: list[str] = []
        for s in data.get("symbols") or []:
            if s.get("status") != "TRADING":
                continue
            if s.get("contractType") != "PERPETUAL":
                continue
            if s.get("quoteAsset") != "USDT":
                continue
            sym = str(s.get("symbol") or "").upper()
            if sym.endswith("USDT"):
                out.append(sym)
        return sorted(set(out))
    except Exception as e:
        logger.warning("binance exchangeInfo http failed: %s", e)
        return []


async def fetch_okx_swap_usdt_symbols() -> list[str]:
    try:
        data = await _http_get_json(
            "https://www.okx.com/api/v5/public/instruments?instType=SWAP"
        )
        out: list[str] = []
        for row in data.get("data") or []:
            if str(row.get("state") or "").lower() not in ("live", "trading"):
                continue
            settle = str(row.get("settleCcy") or "").upper()
            if settle and settle != "USDT":
                continue
            inst = str(row.get("instId") or "")
            norm = BaseExchangeService._norm_sym(inst)
            if norm.endswith("USDT"):
                out.append(norm)
        return sorted(set(out))
    except Exception as e:
        logger.warning("okx instruments http failed: %s", e)
        return []


async def fetch_perp_symbols_http(exchange: str) -> list[str]:
    if exchange == "okx":
        return await fetch_okx_swap_usdt_symbols()
    return await fetch_binance_usdm_perp_symbols()


def filter_symbols_by_query(query: str, pool: list[str], limit: int = 50) -> list[str]:
    q = (query or "").strip().upper()
    if not q:
        return []
    hits = sorted({s for s in pool if q in s})
    if hits:
        return hits[:limit]
    cand = q if q.endswith("USDT") else f"{q}USDT"
    if cand in pool:
        return [cand]
    return []
