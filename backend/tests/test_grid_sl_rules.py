"""马丁网格止损规则：开仓即挂、加仓后更新。"""
from types import SimpleNamespace

from app.services.grid_executor import GridExecutor


def _pos(grid_level: int, qty: float = 1.0):
    return SimpleNamespace(grid_level=grid_level, quantity=qty)


def _strat(loss_u=100.0):
    return SimpleNamespace(
        cumulative_loss_threshold_u=loss_u,
        reopen_after_close=True,
    )


def test_exchange_sl_allowed_from_first_position():
    ex = GridExecutor(None)
    s = _strat()
    assert ex._exchange_sl_allowed(s, []) is False
    assert ex._exchange_sl_allowed(s, [_pos(0)]) is True
    assert ex._exchange_sl_allowed(s, [_pos(0), _pos(2)]) is True


def test_exchange_sl_disabled_when_threshold_zero():
    ex = GridExecutor(None)
    s = _strat(loss_u=0)
    assert ex._exchange_sl_allowed(s, [_pos(5)]) is False
