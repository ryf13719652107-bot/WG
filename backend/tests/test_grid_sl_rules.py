"""马丁网格止损规则：加仓层数门槛与 dust 全平原因常量。"""
from types import SimpleNamespace

from app.services.grid_executor import GridExecutor


def _pos(grid_level: int, qty: float = 1.0):
    return SimpleNamespace(grid_level=grid_level, quantity=qty)


def _strat(loss_u=100.0, pct=50.0):
    return SimpleNamespace(
        cumulative_loss_threshold_u=loss_u,
        stop_loss_close_pct=pct,
        reopen_after_close=True,
    )


def test_exchange_sl_requires_min_grid_level():
    ex = GridExecutor(None)
    s = _strat()
    assert ex._exchange_sl_allowed(s, [_pos(0)]) is False
    assert ex._exchange_sl_allowed(s, [_pos(1), _pos(2)]) is False
    assert ex._exchange_sl_allowed(s, [_pos(0), _pos(3)]) is True


def test_exchange_sl_disabled_when_threshold_zero():
    ex = GridExecutor(None)
    s = _strat(loss_u=0, pct=50)
    assert ex._exchange_sl_allowed(s, [_pos(5)]) is False


def test_dust_restart_close_reason_constant():
    assert GridExecutor.CLOSE_REASON_SL_DUST_RESTART == "stop_loss_dust_restart"


def test_min_grid_level_constant_matches_three_adds():
    assert GridExecutor.MIN_GRID_LEVEL_FOR_EXCHANGE_SL == 3
