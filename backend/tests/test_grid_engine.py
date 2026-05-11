import pytest
from app.services.grid_engine import GridStrategyEngine, GridLevel
from app.services.grid_executor import GridExecutor


class MockStrategy:
    def __init__(self, **kwargs):
        self.base_qty_value = kwargs.get("base_qty_value", 1.0)
        self.base_qty_type = kwargs.get("base_qty_type", "usdt")
        self.tp_pct = kwargs.get("tp_pct", 1.0)
        self.grid_drop_base_pct = kwargs.get("grid_drop_base_pct", 1.0)
        self.grid_interval_multiplier = kwargs.get("grid_interval_multiplier", 1.5)
        self.position_multiplier = kwargs.get("position_multiplier", 1.5)
        self.max_layers = kwargs.get("max_layers", 8)
        self.cumulative_loss_threshold_u = kwargs.get("cumulative_loss_threshold_u", 0.0)
        self.leverage = kwargs.get("leverage", 20)


def test_calculate_grid_levels_long():
    s = MockStrategy(grid_drop_base_pct=1.0, grid_interval_multiplier=1.5, position_multiplier=1.5, max_layers=4)
    eng = GridStrategyEngine(s)
    levels = eng.calculate_grid_levels(100.0, "long")

    assert len(levels) == 4
    assert levels[0].level == 1
    assert levels[0].trigger_price == pytest.approx(99.0, abs=0.01)
    assert levels[0].quantity == pytest.approx(1.5, abs=0.01)
    assert levels[0].drop_pct == pytest.approx(1.0, abs=0.01)

    assert levels[1].level == 2
    assert levels[1].trigger_price == pytest.approx(97.5, abs=0.01)
    assert levels[1].quantity == pytest.approx(2.25, abs=0.01)
    assert levels[1].drop_pct == pytest.approx(2.5, abs=0.01)

    assert levels[2].level == 3
    assert levels[2].trigger_price == pytest.approx(95.25, abs=0.01)
    assert levels[2].quantity == pytest.approx(3.375, abs=0.01)
    assert levels[2].drop_pct == pytest.approx(4.75, abs=0.01)

    assert levels[3].level == 4
    assert levels[3].trigger_price == pytest.approx(91.875, abs=0.01)
    assert levels[3].quantity == pytest.approx(5.0625, abs=0.01)
    assert levels[3].drop_pct == pytest.approx(8.125, abs=0.01)


def test_calculate_grid_levels_short():
    s = MockStrategy(grid_drop_base_pct=1.0, grid_interval_multiplier=1.5, max_layers=3)
    eng = GridStrategyEngine(s)
    levels = eng.calculate_grid_levels(100.0, "short")

    assert len(levels) == 3
    assert levels[0].trigger_price == pytest.approx(101.0, abs=0.01)
    assert levels[1].trigger_price == pytest.approx(102.5, abs=0.01)
    assert levels[2].trigger_price == pytest.approx(104.75, abs=0.01)


def test_calculate_position_size():
    s = MockStrategy(base_qty_value=10.0, position_multiplier=1.5)
    eng = GridStrategyEngine(s)
    assert eng.calculate_position_size(0) == 10.0
    assert eng.calculate_position_size(1) == 15.0
    assert eng.calculate_position_size(2) == 22.5
    assert eng.calculate_position_size(3) == 33.75


def test_calculate_tp_price():
    s = MockStrategy(tp_pct=1.0)
    eng = GridStrategyEngine(s)
    assert eng.calculate_tp_price(100.0, "long") == pytest.approx(101.0, abs=0.01)
    assert eng.calculate_tp_price(100.0, "short") == pytest.approx(99.0, abs=0.01)


def test_calculate_avg_entry():
    s = MockStrategy()
    eng = GridStrategyEngine(s)

    class Pos:
        def __init__(self, qty, price):
            self.quantity = qty
            self.entry_price = price

    positions = [Pos(1.0, 100.0), Pos(1.5, 99.0), Pos(2.25, 97.5)]
    avg = eng.calculate_avg_entry(positions)
    expected = (100.0 + 148.5 + 219.375) / (1.0 + 1.5 + 2.25)
    assert avg == pytest.approx(expected, abs=0.01)


def test_stop_loss_price_matches_cumulative_loss_threshold():
    """在触发价时，线性 USDT 永续多/空：sum((P-e_i)q_i) ≈ -loss_u（用加权均价等价）。"""
    s = MockStrategy(cumulative_loss_threshold_u=5.0)
    eng = GridStrategyEngine(s)
    loss_u = 5.0
    qty = 10.0
    avg = 100.0

    sl_long = eng.stop_loss_price_for_fixed_usdt_loss(avg, qty, loss_u, "long")
    assert sl_long == pytest.approx(99.5, abs=1e-6)
    assert (sl_long - avg) * qty == pytest.approx(-loss_u, abs=1e-4)

    sl_short = eng.stop_loss_price_for_fixed_usdt_loss(avg, qty, loss_u, "short")
    assert sl_short == pytest.approx(100.5, abs=1e-6)
    assert (avg - sl_short) * qty == pytest.approx(-loss_u, abs=1e-4)

    class Pos:
        def __init__(self, qty, price, side):
            self.quantity = qty
            self.entry_price = price
            self.side = side

    legs = [Pos(4.0, 100.0, "long"), Pos(6.0, 101.0, "long")]
    wavg = eng.calculate_avg_entry(legs)
    assert wavg == pytest.approx((400 + 606) / 10.0, abs=1e-6)
    sl2 = eng.stop_loss_price_for_fixed_usdt_loss(wavg, 10.0, loss_u, "long")
    pnl_at_sl = sum((sl2 - p.entry_price) * p.quantity for p in legs)
    assert pnl_at_sl == pytest.approx(-loss_u, abs=0.05)


def test_stop_loss_uses_ct_val_for_contract_linear_pnl():
    """低价币：亏损 U 对应的价格步长应为 loss_u/(张数×ctVal)，不能误用 loss_u/张数。"""
    s = MockStrategy(cumulative_loss_threshold_u=1.0)
    eng = GridStrategyEngine(s)
    avg, qty, loss_u, ct = 0.014693, 3.0, 1.0, 90.0
    sl = eng.stop_loss_price_for_fixed_usdt_loss(avg, qty, loss_u, "long", ct_val=ct)
    assert sl > 0
    assert (avg - sl) * qty * ct == pytest.approx(loss_u, abs=1e-4)
    s = MockStrategy(max_layers=5)
    eng = GridStrategyEngine(s)
    gl = eng.get_next_grid_add(100.0, 0, "long")
    assert gl is not None
    assert gl.level == 1

    gl_mid = eng.get_next_grid_add(100.0, 4, "long")
    assert gl_mid is not None
    assert gl_mid.level == 5

    gl2 = eng.get_next_grid_add(100.0, 5, "long")
    assert gl2 is None


def test_avg_price_cost_over_filled_first():
    raw = {"filled": 3.0, "cost": 2.3913}
    assert GridExecutor._avg_price_from_order_dict(raw) == pytest.approx(0.7971, rel=1e-5)


def test_avg_price_prefers_info_avg_px_over_unified_average():
    """OKX 等：ccxt average 与网页不一致时，应以 info.avgPx 为先（在无 cost 时）。"""
    raw = {"filled": 3.0, "average": 0.7955, "info": {"avgPx": "0.7971"}}
    assert GridExecutor._avg_price_from_order_dict(raw) == pytest.approx(0.7971, rel=1e-5)
