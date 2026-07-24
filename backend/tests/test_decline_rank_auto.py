"""Tests for decline-rank auto strategy helpers."""
from datetime import datetime

import pytest

from app.services.market_rankings import rank_tickers_to_losers
from app.services.decline_rank_auto import is_in_window, window_id_for


def test_rank_tickers_to_losers_sorts_and_filters():
    tickers = {
        "AAA/USDT:USDT": {
            "symbol": "AAA/USDT:USDT",
            "percentage": -20.5,
            "last": 1.0,
            "quoteVolume": 1_000_000,
        },
        "BBB/USDT:USDT": {
            "symbol": "BBB/USDT:USDT",
            "percentage": -5.0,
            "last": 2.0,
            "quoteVolume": 500_000,
        },
        "CCC/USDT:USDT": {
            "symbol": "CCC/USDT:USDT",
            "percentage": 3.0,
            "last": 3.0,
            "quoteVolume": 800_000,
        },
        "DDD/USDT:USDT": {
            "symbol": "DDD/USDT:USDT",
            "percentage": -50.0,
            "last": 0.1,
            "quoteVolume": 100,  # below min
        },
        "SPOTIGNORE": {
            "symbol": "EEE/USDT",
            "percentage": -99.0,
            "last": 1.0,
            "quoteVolume": 9_000_000,
        },
    }
    perp = {"AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT"}
    ranked = rank_tickers_to_losers(tickers, perp, limit=2, min_quote_volume=1000)
    assert [r["symbol"] for r in ranked] == ["AAAUSDT", "BBBUSDT"]
    assert ranked[0]["price_change_pct"] == -20.5


def test_rank_tickers_fallback_open_last():
    tickers = {
        "AAA/USDT:USDT": {
            "symbol": "AAA/USDT:USDT",
            "percentage": None,
            "open": 100.0,
            "last": 80.0,
            "quoteVolume": 10_000,
        },
    }
    ranked = rank_tickers_to_losers(tickers, {"AAAUSDT"}, limit=5)
    assert len(ranked) == 1
    assert ranked[0]["price_change_pct"] == pytest.approx(-20.0)


@pytest.mark.parametrize(
    "now,start,end,expected",
    [
        (datetime(2026, 7, 24, 3, 0), "03:00", "00:00", True),
        (datetime(2026, 7, 24, 23, 59), "03:00", "00:00", True),
        (datetime(2026, 7, 24, 0, 0), "03:00", "00:00", False),
        (datetime(2026, 7, 24, 2, 59), "03:00", "00:00", False),
        (datetime(2026, 7, 24, 10, 0), "09:00", "18:00", True),
        (datetime(2026, 7, 24, 8, 59), "09:00", "18:00", False),
        (datetime(2026, 7, 24, 18, 0), "09:00", "18:00", False),
        (datetime(2026, 7, 24, 12, 0), "12:00", "12:00", True),  # full day
    ],
)
def test_is_in_window(now, start, end, expected):
    assert is_in_window(now, start, end) is expected


def test_window_id_cross_midnight():
    # Inside window after start → today's id
    assert window_id_for(datetime(2026, 7, 24, 4, 0), "03:00", "00:00") == "2026-07-24"
    # Idle after midnight end → yesterday's window id for cleanup
    assert window_id_for(datetime(2026, 7, 24, 0, 30), "03:00", "00:00") == "2026-07-23"
    # Same-day after end
    assert window_id_for(datetime(2026, 7, 24, 19, 0), "09:00", "18:00") == "2026-07-24"
    # Same-day before start → yesterday
    assert window_id_for(datetime(2026, 7, 24, 8, 0), "09:00", "18:00") == "2026-07-23"


def test_parse_state_datetime_naive_compatible():
    """刷新间隔计算必须全程 naive，避免 TypeError 导致每分钟强制刷新。"""
    from datetime import timezone, timedelta
    from app.services.decline_rank_auto import _parse_state_datetime
    from app.config import now_beijing

    lr = _parse_state_datetime("2026-07-24 03:00:00")
    assert lr is not None
    assert lr.tzinfo is None
    now = now_beijing()
    _ = (now.replace(year=2026, month=7, day=24, hour=3, minute=15) - lr).total_seconds()

    aware = datetime(2026, 7, 24, 3, 0, tzinfo=timezone(timedelta(hours=8)))
    lr2 = _parse_state_datetime(aware.isoformat())
    assert lr2 is not None and lr2.tzinfo is None
    assert lr2.hour == 3


def test_next_start_waits_until_tomorrow_if_start_passed():
    from app.services.decline_rank_auto import next_start_datetime, resolve_session

    now = datetime(2026, 7, 24, 18, 42)
    nxt = next_start_datetime(now, "03:00", "00:00")
    assert nxt == datetime(2026, 7, 25, 3, 0)

    r = resolve_session(
        now, "03:00", "00:00",
        session_window_id=None,
        has_auto_strategies=False,
    )
    assert r["calendar_in_window"] is True
    assert r["session_active"] is False
    assert r["waiting_next_start"] is True
    assert "2026-07-25 03:00" in (r["next_session_at"] or "")

    r2 = resolve_session(
        datetime(2026, 7, 25, 3, 1), "03:00", "00:00",
        session_window_id=None,
        has_auto_strategies=False,
    )
    assert r2["session_active"] is True
    assert r2["enter_session"] is True

    r3 = resolve_session(
        now, "03:00", "00:00",
        session_window_id="2026-07-24",
        has_auto_strategies=False,
    )
    assert r3["session_active"] is True
