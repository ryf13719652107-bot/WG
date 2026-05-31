from datetime import datetime
from sqlalchemy import String, Float, Integer, Boolean, DateTime, Text, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship
from ..database import Base
from ..config import now_beijing


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    api_key_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    api_secret_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    exchange: Mapped[str] = mapped_column(String(20), default="binance", server_default="binance")
    testnet: Mapped[bool] = mapped_column(Boolean, default=True)
    hedge_mode: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    okx_passphrase_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    # 账户总资产止损：equity_stop_floor_u=0 表示关闭；当前总权益 < 下限时停止本账户全部策略
    equity_stop_floor_u: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    equity_baseline_u: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    equity_baseline_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    equity_stop_triggered: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_beijing)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_beijing, onupdate=now_beijing)
