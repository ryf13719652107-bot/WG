from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional, Literal


class StrategyCreate(BaseModel):
    account_id: int
    direction: Literal["long", "short"]
    symbol: str = Field(min_length=1, max_length=50)
    # Entry
    base_qty_type: Literal["margin_pct", "usdt"] = "margin_pct"
    base_qty_value: float = Field(default=6.0, gt=0)
    # Grid params
    max_layers: int = Field(default=6, ge=1, le=99999)
    tp_pct: float = Field(default=1.0, gt=0, le=50)
    grid_drop_base_pct: float = Field(default=1.0, gt=0, le=100)
    grid_interval_multiplier: float = Field(default=1.5, ge=1.0, le=10.0)
    position_multiplier: float = Field(default=1.5, ge=1.0, le=10.0)
    cumulative_loss_threshold_u: float = Field(
        default=0.0, ge=0, description="条件止损名义亏损额 U（推算触发价）；0=不挂止损单",
    )
    stop_loss_close_pct: float = Field(
        default=100.0, ge=0, le=100,
        description="触发止损时在持仓数量维度平仓的比例%；0=不挂止损单",
    )
    reopen_after_close: bool = True


class StrategyUpdate(BaseModel):
    """可编辑的策略参数,运行中也能修改(自动停启生效)."""
    base_qty_type: Optional[Literal["margin_pct", "usdt"]] = None
    base_qty_value: Optional[float] = Field(default=None, gt=0)
    max_layers: Optional[int] = Field(default=None, ge=1, le=99999)
    tp_pct: Optional[float] = Field(default=None, gt=0, le=50)
    grid_drop_base_pct: Optional[float] = Field(default=None, gt=0, le=100)
    grid_interval_multiplier: Optional[float] = Field(default=None, ge=1.0, le=10.0)
    position_multiplier: Optional[float] = Field(default=None, ge=1.0, le=10.0)
    cumulative_loss_threshold_u: Optional[float] = Field(
        default=None, ge=0, description="条件止损名义亏损额 U；0=不挂",
    )
    stop_loss_close_pct: Optional[float] = Field(default=None, ge=0, le=100)
    reopen_after_close: Optional[bool] = None


class StrategyResponse(BaseModel):
    id: int
    account_id: int
    direction: str
    symbol: str
    base_qty_type: str
    base_qty_value: float
    max_layers: int
    tp_pct: float
    grid_drop_base_pct: float
    grid_interval_multiplier: float
    position_multiplier: float
    cumulative_loss_threshold_u: float
    stop_loss_close_pct: float
    reopen_after_close: bool
    status: str
    started_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
