"""Strategy model for martingale grid trading."""
import logging
import traceback
from datetime import datetime
from sqlalchemy import String, Float, Integer, Boolean, DateTime, ForeignKey, Index, event
from ..config import now_beijing
from sqlalchemy.orm import Mapped, mapped_column
from ..database import Base

logger = logging.getLogger(__name__)


class Strategy(Base):
    __tablename__ = "strategies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    direction: Mapped[str] = mapped_column(String(10), nullable=False)
    symbol: Mapped[str] = mapped_column(String(50), nullable=False)

    # Entry position params
    base_qty_type: Mapped[str] = mapped_column(String(20), default="margin_pct")  # 'margin_pct' or 'usdt'
    base_qty_value: Mapped[float] = mapped_column(Float, default=6.0)

    # Martingale grid params
    max_layers: Mapped[int] = mapped_column(Integer, default=6)

    # Grid-specific params
    tp_pct: Mapped[float] = mapped_column(Float, default=1.0)  # take profit % (default 1%)
    grid_drop_base_pct: Mapped[float] = mapped_column(Float, default=1.0)  # base drop % for first grid level
    grid_interval_multiplier: Mapped[float] = mapped_column(Float, default=1.5)  # drop interval multiplier
    position_multiplier: Mapped[float] = mapped_column(Float, default=1.5)  # position size multiplier per layer
    cumulative_loss_threshold_u: Mapped[float] = mapped_column(Float, default=0.0)  # stop loss U threshold (0=disabled)
    reopen_after_close: Mapped[bool] = mapped_column(Boolean, default=True)  # reopen after TP/SL close

    # Runtime state
    status: Mapped[str] = mapped_column(String(20), default="stopped")  # 'running', 'stopped', 'error'
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_beijing)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_beijing, onupdate=now_beijing)


Index("idx_strategies_account", Strategy.account_id)
Index("idx_strategies_status", Strategy.status)
Index("idx_strategies_symbol", Strategy.symbol)


@event.listens_for(Strategy, "before_update")
def _track_status_change(mapper, connection, target):
    state = target._sa_instance_state
    hist = state.get_history("status", state.attrs.status.loaded_value)
    if hist.deleted and hist.deleted[0] != target.status:
        logger.warning(
            "STATUS CHANGE: strategy_id=%d '%s' -> '%s'\n%s",
            target.id, hist.deleted[0], target.status,
            "".join(traceback.format_stack()[-8:-1])
        )
