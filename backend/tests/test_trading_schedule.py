"""交易时段判断单元测试。"""
from datetime import datetime

from app.services.trading_schedule import (
    TradingWindowConfig,
    is_within_trading_window,
    _parse_hm,
)


def test_parse_hm():
    assert _parse_hm("06:00") is not None
    assert _parse_hm("bad") is None


def test_same_day_window():
    cfg = TradingWindowConfig(enabled=True, start_hm="06:00", end_hm="21:00")
    assert is_within_trading_window(datetime(2026, 5, 31, 6, 0), cfg=cfg)
    assert is_within_trading_window(datetime(2026, 5, 31, 20, 59), cfg=cfg)
    assert not is_within_trading_window(datetime(2026, 5, 31, 21, 0), cfg=cfg)
    assert not is_within_trading_window(datetime(2026, 5, 31, 5, 59), cfg=cfg)


def test_equal_start_end_always_closed():
    cfg = TradingWindowConfig(enabled=True, start_hm="21:00", end_hm="21:00")
    assert not is_within_trading_window(datetime(2026, 5, 31, 12, 0), cfg=cfg)


def test_overnight_window():
    cfg = TradingWindowConfig(enabled=True, start_hm="22:00", end_hm="06:00")
    assert is_within_trading_window(datetime(2026, 5, 31, 23, 0), cfg=cfg)
    assert is_within_trading_window(datetime(2026, 5, 31, 5, 0), cfg=cfg)
    assert not is_within_trading_window(datetime(2026, 5, 31, 12, 0), cfg=cfg)
