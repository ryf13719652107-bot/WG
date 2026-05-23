from datetime import datetime
from sqlalchemy import String, Numeric, Integer, DateTime, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column
from ..database import Base
from ..config import now_beijing


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_id: Mapped[int] = mapped_column(Integer, ForeignKey("strategies.id", ondelete="SET NULL"), nullable=True)
    account_id: Mapped[int] = mapped_column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    side: Mapped[str] = mapped_column(String(10), nullable=False)
    quantity: Mapped[float] = mapped_column(Numeric(20, 8), nullable=False)
    entry_price: Mapped[float] = mapped_column(Numeric(20, 8), nullable=False)
    mark_price: Mapped[float | None] = mapped_column(Numeric(20, 8), nullable=True)
    unrealized_pnl: Mapped[float | None] = mapped_column(Numeric(20, 8), nullable=True, default=0.0)
    layer: Mapped[int] = mapped_column(Integer, default=0)

    grid_level: Mapped[int] = mapped_column(Integer, default=0)
    grid_trigger_price: Mapped[float | None] = mapped_column(Numeric(20, 8), nullable=True)
    tp_limit_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    add_limit_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    sl_algo_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True)

    take_profit_price: Mapped[float | None] = mapped_column(Numeric(20, 8), nullable=True)
    exchange_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=now_beijing)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


Index("idx_positions_open", Position.closed_at)
Index("idx_positions_strategy", Position.strategy_id)
Index("idx_positions_symbol", Position.symbol)
