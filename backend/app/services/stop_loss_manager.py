"""Multi-level stop loss manager for grid strategies."""
import logging
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class StopLossLevel(Enum):
    NONE = "none"
    SOFT = "soft"      # 80% of threshold — alert only
    HARD = "hard"      # 100% of threshold — full close
    PANIC = "panic"    # single layer loss >50%


@dataclass
class StopLossDecision:
    level: StopLossLevel
    message: str
    should_close: bool = False
    affected_layer: int | None = None


class StopLossManager:
    """Evaluates stop loss conditions at multiple levels.

    SOFT (80%): Alert but continue trading
    HARD (100%): Immediate full position close
    PANIC: Single layer unrealized loss exceeds 50% of entry value
    """

    def __init__(self, threshold_u: float = 0.0):
        self.threshold_u = threshold_u
        self._soft_trigger_ratio = 0.8

    def evaluate(self, cumulative_u_pnl: float, positions: list, current_price: float) -> StopLossDecision:
        """Evaluate all stop loss levels. Returns the most severe decision."""
        decisions = [
            self._check_panic(positions, current_price),
            self._check_hard(cumulative_u_pnl),
            self._check_soft(cumulative_u_pnl),
        ]

        # Return the most severe
        for d in [StopLossLevel.PANIC, StopLossLevel.HARD, StopLossLevel.SOFT, StopLossLevel.NONE]:
            for decision in decisions:
                if decision.level == d:
                    return decision

        return StopLossDecision(
            level=StopLossLevel.NONE,
            message="OK",
        )

    def _check_soft(self, cumulative_u: float) -> StopLossDecision:
        if self.threshold_u <= 0:
            return StopLossDecision(level=StopLossLevel.NONE, message="SL disabled")
        ratio = abs(cumulative_u) / self.threshold_u if self.threshold_u > 0 else 0
        if cumulative_u < 0 and ratio >= self._soft_trigger_ratio and ratio < 1.0:
            return StopLossDecision(
                level=StopLossLevel.SOFT,
                message=f"WARNING: cumulative loss {abs(cumulative_u):.2f}U at {ratio*100:.0f}% of threshold",
            )
        return StopLossDecision(level=StopLossLevel.NONE, message="OK")

    def _check_hard(self, cumulative_u: float) -> StopLossDecision:
        if self.threshold_u <= 0:
            return StopLossDecision(level=StopLossLevel.NONE, message="SL disabled")
        if cumulative_u < 0 and abs(cumulative_u) >= self.threshold_u:
            return StopLossDecision(
                level=StopLossLevel.HARD,
                message=f"STOP LOSS: cumulative loss {abs(cumulative_u):.2f}U reached threshold {self.threshold_u}U",
                should_close=True,
            )
        return StopLossDecision(level=StopLossLevel.NONE, message="OK")

    def _check_panic(self, positions: list, current_price: float) -> StopLossDecision:
        """Check if any single layer has >50% unrealized loss."""
        for pos in positions:
            qty = float(pos.quantity)
            entry = float(pos.entry_price)
            if entry <= 0 or qty <= 0:
                continue

            entry_val = qty * entry
            pnl = float(pos.unrealized_pnl or 0)

            if entry_val > 0 and pnl < 0 and abs(pnl) / entry_val >= 0.5:
                return StopLossDecision(
                    level=StopLossLevel.PANIC,
                    message=f"PANIC: layer {pos.grid_level} loss {abs(pnl):.2f}U >50% of entry value",
                    should_close=True,
                    affected_layer=pos.grid_level,
                )

        return StopLossDecision(level=StopLossLevel.NONE, message="OK")
