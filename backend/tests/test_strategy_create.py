"""策略创建回归测试。"""
from app.models.strategy import Strategy
from app.schemas.strategy import StrategyCreate, StrategyResponse
from app.config import now_beijing
from app.services.exchange_base import BaseExchangeService


def test_strategy_create_payload_merge():
    data = StrategyCreate(
        account_id=1,
        direction="long",
        symbol="BTCUSDT",
        base_qty_value=6.0,
    )
    payload = data.model_dump()
    payload["base_qty_type"] = "usdt"
    s = Strategy(**payload)
    assert s.base_qty_type == "usdt"
    assert s.stop_loss_close_pct == 100.0


def test_strategy_create_no_duplicate_base_qty_type():
    data = StrategyCreate(account_id=1, direction="short", symbol="ETHUSDT", base_qty_value=10)
    payload = {**data.model_dump(), "base_qty_type": "margin_pct"}
    payload["base_qty_type"] = "usdt"
    s = Strategy(**payload)
    assert s.base_qty_type == "usdt"
    assert s.stop_loss_close_pct == 100.0


def test_strategy_response_nullable_fields():
    now = now_beijing()
    resp = StrategyResponse.model_validate({
        "id": 1,
        "account_id": 1,
        "direction": "long",
        "symbol": "BTCUSDT",
        "base_qty_type": None,
        "base_qty_value": 6.0,
        "max_layers": 6,
        "tp_pct": 1.0,
        "grid_drop_base_pct": 1.0,
        "grid_interval_multiplier": 1.5,
        "position_multiplier": 1.5,
        "cumulative_loss_threshold_u": 0.0,
        "reopen_after_close": 1,
        "status": "stopped",
        "started_at": None,
        "created_at": now,
        "updated_at": now,
    })
    assert resp.base_qty_type == "usdt"
    assert resp.reopen_after_close is True


def test_create_symbol_normalized():
  """创建策略应统一存 LABUSDT 格式，避免与策略计数键不一致。"""
  raw = "LAB/USDT:USDT"
  norm = BaseExchangeService._norm_sym(raw)
  assert norm == "LABUSDT"
