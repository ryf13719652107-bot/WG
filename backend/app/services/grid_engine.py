"""Pure martingale grid calculation engine. No side effects, no I/O."""
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class GridLevel:
    level: int           # 0 = initial, 1, 2, ...
    trigger_price: float  # price at which this grid add triggers
    quantity: float       # position size for this level
    drop_pct: float       # cumulative drop % from base for this level


class GridStrategyEngine:
    """Calculates grid levels, TP prices, and stop trigger price for martingale grid strategy.

    Rules:
    - Initial open: immediately at strategy start (market order)
    - TP: fixed % limit order above/below average entry (default 1%)
    - Grid add: limit orders anchored to the initial entry price; only one pending
      add-limit order exists at a time (next layer is placed after the previous fills).
      Level 1: base_price * (1 - drop_base_pct)                                   [1%]
      Level 2: base_price * (1 - drop_base_pct - drop_base_pct * interval_mult)   [1% + 1.5% = 2.5%]
      Level N: base_price * (1 - sum of intervals)
      where interval_n = drop_base_pct * interval_multiplier^(n-1)
    - Position sizing: level_n = base_qty * position_multiplier^n
    - Stop loss: optional exchange algo order at a price derived from fixed USDT loss
      (cumulative_loss_threshold_u). When triggered, closes stop_loss_close_pct % of position
      quantity; 0% disables. After stop loss the strategy never auto-reopens (TP may still).
    """

    def __init__(self, strategy):
        self.base_qty = float(strategy.base_qty_value)
        self.base_qty_type = strategy.base_qty_type
        self.tp_pct = float(strategy.tp_pct)
        self.drop_base_pct = float(strategy.grid_drop_base_pct)
        self.interval_mult = float(strategy.grid_interval_multiplier)
        self.pos_mult = float(strategy.position_multiplier)
        self.max_layers = int(strategy.max_layers)
        self.loss_threshold = float(strategy.cumulative_loss_threshold_u or 0)
        self.sl_close_pct = float(getattr(strategy, "stop_loss_close_pct", 100) or 0)

    def calculate_grid_level_at(self, base_price: float, side: str, level: int) -> Optional[GridLevel]:
        """单层网格限价参数。``base_price`` 固定为首仓成交价（不因加仓成交而漂移）。"""
        if level < 1 or level > self.max_layers or base_price <= 0:
            return None
        cumulative_drop = 0.0
        for n in range(1, level + 1):
            if n == 1:
                interval = self.drop_base_pct
            else:
                interval = self.drop_base_pct * (self.interval_mult ** (n - 1))
            cumulative_drop += interval

        qty = self.calculate_position_size(level)
        if side == "long":
            trigger_price = base_price * (1.0 - cumulative_drop / 100.0)
        else:
            trigger_price = base_price * (1.0 + cumulative_drop / 100.0)
        return GridLevel(
            level=level,
            trigger_price=round(trigger_price, 8),
            quantity=round(qty, 8),
            drop_pct=round(cumulative_drop, 4),
        )

    def calculate_grid_levels(self, base_price: float, side: str) -> list[GridLevel]:
        """Calculate all grid add trigger prices and quantities from base entry price.

        Level 0 is the initial entry (not returned since it's always market-open).
        Returns levels 1..max_layers (user-set grid add count).

        Prefer :meth:`calculate_grid_level_at` for a single layer when ``max_layers`` is large.
        """
        levels: list[GridLevel] = []
        for n in range(1, self.max_layers + 1):
            gl = self.calculate_grid_level_at(base_price, side, n)
            if gl:
                levels.append(gl)
        return levels

    def calculate_position_size(self, level: int) -> float:
        """Level N size = base_qty * position_multiplier^N."""
        return self.base_qty * (self.pos_mult ** level)

    def calculate_tp_price(self, avg_entry: float, side: str) -> float:
        """Calculate take-profit price (1% above/below average entry)."""
        if side == "long":
            tp = avg_entry * (1.0 + self.tp_pct / 100.0)
        else:
            tp = avg_entry * (1.0 - self.tp_pct / 100.0)
        return round(tp, 8)

    def calculate_avg_entry(self, positions: list) -> float:
        """Weighted average entry price across all open positions."""
        total_qty = 0.0
        total_cost = 0.0
        for p in positions:
            qty = float(p.quantity)
            if qty <= 0:
                continue
            price = float(p.entry_price)
            total_qty += qty
            total_cost += qty * price
        if total_qty <= 0:
            return 0.0
        return total_cost / total_qty

    @staticmethod
    def stop_loss_price_for_fixed_usdt_loss(
        weighted_avg_entry: float,
        contract_qty: float,
        loss_u: float,
        side: str,
        *,
        ct_val: float = 1.0,
    ) -> float:
        """线性 USDT 永续：在触发价全平 contract_qty 张时，浮动亏损约 loss_u（U）。

        多仓近似 (avg − P)×张数×ctVal = loss_u → P = avg − loss_u/(张数×ctVal)；空仓对称。
        ct_val 取交易所合约 ctVal/contractSize；为 1 时退化为旧式 (avg − loss_u/张数)。
        """
        if weighted_avg_entry <= 0 or contract_qty <= 0 or loss_u <= 0:
            return 0.0
        cv = float(ct_val) if ct_val is not None else 1.0
        if cv <= 0:
            cv = 1.0
        denom = contract_qty * cv
        if denom <= 0:
            return 0.0
        step = loss_u / denom
        s = side.lower()
        if s == "long":
            return round(weighted_avg_entry - step, 8)
        if s == "short":
            return round(weighted_avg_entry + step, 8)
        return 0.0

    def get_next_grid_add(
        self, anchor_base_price: float, current_max_grid_level: int, side: str,
    ) -> Optional[GridLevel]:
        """下一层加仓挂单参数。锚定价 ``anchor_base_price`` 为首仓入场价 ``grid_level==0`` 的成交价。"""
        next_level = current_max_grid_level + 1
        if next_level > self.max_layers:
            return None
        return self.calculate_grid_level_at(anchor_base_price, side, next_level)
