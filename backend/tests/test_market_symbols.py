"""market_symbols 搜索兜底测试。"""
from app.services.market_symbols import FALLBACK_USDT_PERP, filter_symbols_by_query


def test_filter_symbols_by_query_lab():
    pool = FALLBACK_USDT_PERP
    assert "LABUSDT" in filter_symbols_by_query("LAB", pool)
    assert "ETHUSDT" in filter_symbols_by_query("ETH", pool)


def test_filter_symbols_by_query_partial():
    hits = filter_symbols_by_query("BTC", FALLBACK_USDT_PERP)
    assert "BTCUSDT" in hits
