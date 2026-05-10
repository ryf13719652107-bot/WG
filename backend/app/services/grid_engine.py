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
    """Calculates grid levels, TP prices, and cumulative PnL for martingale grid strategy.

    Rules:
    - Initial open: immediately at strategy start (market order)
    - TP: fixed % limit order above/below average entry (default 1%)
    - Grid add: triggered at pre-calculated price levels
      Level 1: base_price * (1 - drop_base_pct)                                   [1%]
      Level 2: base_price * (1 - drop_base_pct - drop_base_pct * interval_mult)   [1% + 1.5% = 2.5%]
      Level N: base_price * (1 - sum of intervals)
      where interval_n = drop_base_pct * interval_multiplier^(n-1)
    - Position sizing: level_n = base_qty * position_multiplier^n
    - Stop loss: per-strategy cumulative U loss threshold
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

    def calculate_grid_levels(self, base_price: float, side: str) -> list[GridLevel]:
        """Calculate all grid add trigger prices and quantities from base entry price.

        Level 0 is the initial entry (not returned since it's always market-open).
        Returns levels 1..max_layers (user-set grid add count).
        """
        levels = []
        cumulative_drop = 0.0

        for n in range(1, self.max_layers + 1):
            # Interval for level n
            if n == 1:
                interval = self.drop_base_pct
            else:
                interval = self.drop_base_pct * (self.interval_mult ** (n - 1))

            cumulative_drop += interval
            qty = self.calculate_position_size(n)

            if side == "long":
                trigger_price = base_price * (1.0 - cumulative_drop / 100.0)
            else:
                trigger_price = base_price * (1.0 + cumulative_drop / 100.0)

            levels.append(GridLevel(
                level=n,
                trigger_price=round(trigger_price, 8),
                quantity=round(qty, 8),
                drop_pct=round(cumulative_drop, 4),
            ))

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
            price = float(p.entry_price)
            total_qty += qty
            total_cost += qty * price
        if total_qty <= 0:
            return 0.0
        return total_cost / total_qty

    def calculate_cumulative_loss(self, positions: list, current_price: float) -> float:
        """Calculate cumulative unrealized U PnL across all layers for this strategy.

        Returns value in USDT (U). Negative = loss.
        """
        total_u = 0.0
        for p in positions:
            qty = float(p.quantity)
            entry = float(p.entry_price)
            side = p.side

            if side == "long":
                upnl = (current_price - entry) * qty
            else:
                upnl = (entry - current_price) * qty

            total_u += upnl

        return total_u

    def should_stop_loss(self, cumulative_u_pnl: float) -> bool:
        """Check if cumulative U loss exceeds threshold."""
        if self.loss_threshold <= 0:
            return False
        return abs(cumulative_u_pnl) >= self.loss_threshold and cumulative_u_pnl < 0

    def get_next_grid_add(self, last_entry_price: float, current_layer: int, side: str) -> Optional[GridLevel]:
        """Get the next grid add level parameters (for placing limit order after an add fills)."""
        next_level = current_layer + 1
        if next_level > self.max_layers:
            return None

        levels = self.calculate_grid_levels(last_entry_price, side)
        for gl in levels:
            if gl.level == next_level:
                return gl
        return None
