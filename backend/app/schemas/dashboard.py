from pydantic import BaseModel
from typing import Optional


class SlEventItem(BaseModel):
    time: str
    exit_price: float
    quantity: float


class StrategyStatItem(BaseModel):
    strategy_id: int
    symbol: str
    direction: str
    status: str
    tp_total: int = 0
    tp_today: int = 0
    sl_events: list[SlEventItem] = []


class DashboardSnapshot(BaseModel):
    total_balance: float = 0.0
    available_balance: float = 0.0
    unrealized_pnl: float = 0.0
    unrealized_pnl_long: float = 0.0
    unrealized_pnl_short: float = 0.0
    daily_pnl: float = 0.0
    daily_pnl_long: float = 0.0
    daily_pnl_short: float = 0.0
    daily_pnl_pct: float = 0.0
    active_strategies: int = 0
    open_positions: int = 0
    daily_trades: int = 0
    win_rate_pct: float = 0.0
    total_realized_pnl: float = 0.0
    total_trades: int = 0
    total_win_rate_pct: float = 0.0
    total_pnl_long: float = 0.0
    total_pnl_short: float = 0.0
    leverage_multiplier: float = 0.0
    master_switch: bool = False
    account_name: str = ""
    balance_status: str = ""
    exchange_positions: list[dict] = []
    strategy_stats: list[StrategyStatItem] = []
