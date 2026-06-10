"""USDT 永续交易对提取与策略创建兼容测试。"""
from app.services.exchange_base import BaseExchangeService


def test_extract_usdt_perp_symbols_binance_style():
    raw = [
        {"id": "BTCUSDT", "symbol": "BTC/USDT:USDT", "type": "swap", "linear": True, "active": True},
        {"id": "BTCUSD_PERP", "symbol": "BTC/USD:BTC", "type": "swap", "linear": False, "active": True},
        {"id": "GPSUSDT", "symbol": "GPS/USDT:USDT", "type": "future", "contract": True, "active": True},
    ]
    syms = BaseExchangeService.extract_usdt_perp_symbols(raw)
    assert "BTCUSDT" in syms
    assert "GPSUSDT" in syms
    assert "BTCUSD_PERP" not in syms


def test_extract_usdt_perp_symbols_okx_style():
    raw = [
        {"id": "GPS-USDT-SWAP", "symbol": "GPS/USDT:USDT", "type": "swap", "swap": True, "active": True},
    ]
    assert "GPSUSDT" in BaseExchangeService.extract_usdt_perp_symbols(raw)


def test_extract_usdt_perp_symbols_minimal_new_listing():
    """新上架币种 ccxt 可能缺少 type/swap 字段。"""
    raw = [
        {"id": "LABUSDT", "symbol": "LAB/USDT:USDT", "active": True},
    ]
    assert "LABUSDT" in BaseExchangeService.extract_usdt_perp_symbols(raw)
