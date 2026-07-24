from pydantic import BaseModel, Field, field_validator
from datetime import datetime
from typing import Optional, Literal


class StrategyParamTemplateParams(BaseModel):
    base_qty_value: float = Field(default=6.0, gt=0)
    max_layers: int = Field(default=6, ge=1, le=99999)
    tp_pct: float = Field(default=1.0, gt=0, le=50)
    grid_drop_base_pct: float = Field(default=1.0, gt=0, le=100)
    grid_interval_multiplier: float = Field(default=1.5, ge=1.0, le=10.0)
    position_multiplier: float = Field(default=1.5, ge=1.0, le=10.0)
    cumulative_loss_threshold_u: float = Field(default=0.0, ge=0)
    reopen_after_close: bool = True


class StrategyParamTemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    params: StrategyParamTemplateParams


class StrategyParamTemplateResponse(BaseModel):
    id: str
    name: str
    params: StrategyParamTemplateParams
    created_at: str


class SlEvent(BaseModel):
    time: str
    exit_price: float
    quantity: float


class StrategyStatsResponse(BaseModel):
    tp_total: int = 0
    tp_today: int = 0
    sl_events: list[SlEvent] = []


class StrategyCreate(BaseModel):
    account_id: int
    direction: Literal["long", "short"]
    symbol: str = Field(min_length=1, max_length=50)
    # Entry (固定 USDT 名义)
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
    reopen_after_close: bool = True
    source: Literal["manual", "decline_rank"] = "manual"


class StrategyUpdate(BaseModel):
    """可编辑的策略参数,运行中也能修改(自动停启生效)."""
    base_qty_value: Optional[float] = Field(default=None, gt=0)
    max_layers: Optional[int] = Field(default=None, ge=1, le=99999)
    tp_pct: Optional[float] = Field(default=None, gt=0, le=50)
    grid_drop_base_pct: Optional[float] = Field(default=None, gt=0, le=100)
    grid_interval_multiplier: Optional[float] = Field(default=None, ge=1.0, le=10.0)
    position_multiplier: Optional[float] = Field(default=None, ge=1.0, le=10.0)
    cumulative_loss_threshold_u: Optional[float] = Field(
        default=None, ge=0, description="条件止损名义亏损额 U；0=不挂",
    )
    reopen_after_close: Optional[bool] = None


class StrategyResponse(BaseModel):
    id: int
    account_id: int
    direction: str
    symbol: str
    base_qty_type: str = "usdt"
    base_qty_value: float
    max_layers: int
    tp_pct: float
    grid_drop_base_pct: float
    grid_interval_multiplier: float
    position_multiplier: float
    cumulative_loss_threshold_u: float
    reopen_after_close: bool
    source: str = "manual"
    status: str
    started_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

    @field_validator("base_qty_type", mode="before")
    @classmethod
    def _default_qty_type(cls, v):
        return v or "usdt"

    @field_validator("reopen_after_close", mode="before")
    @classmethod
    def _coerce_reopen(cls, v):
        if v is None:
            return True
        return bool(v)

    @field_validator("source", mode="before")
    @classmethod
    def _default_source(cls, v):
        return v or "manual"


class DeclineRankAutoConfig(BaseModel):
    """跌幅榜自动策略配置（BotConfig JSON）。"""
    enabled: bool = False
    account_id: Optional[int] = None
    direction: Literal["long", "short"] = "short"
    start_time: str = Field(default="03:00", description="北京时间 HH:MM")
    end_time: str = Field(default="00:00", description="北京时间 HH:MM；可等于开始时间表示跨午夜至次日")
    refresh_interval_min: int = Field(default=15, ge=1, le=1440)
    top_n: int = Field(default=10, ge=1, le=100)
    params: StrategyParamTemplateParams = Field(default_factory=StrategyParamTemplateParams)

    @field_validator("start_time", "end_time")
    @classmethod
    def _validate_hhmm(cls, v: str) -> str:
        raw = (v or "").strip()
        parts = raw.split(":")
        if len(parts) != 2:
            raise ValueError("时间格式须为 HH:MM")
        try:
            h, m = int(parts[0]), int(parts[1])
        except ValueError as e:
            raise ValueError("时间格式须为 HH:MM") from e
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError("时间超出范围")
        return f"{h:02d}:{m:02d}"


class DeclineRankAutoStatus(BaseModel):
    """跌幅榜自动策略运行状态。"""
    enabled: bool = False
    in_window: bool = False
    window_id: Optional[str] = None
    last_refresh_at: Optional[str] = None
    next_refresh_at: Optional[str] = None
    current_symbols: list[str] = Field(default_factory=list)
    active_symbols: list[str] = Field(default_factory=list, description="已创建的自动策略币种")
    auto_strategy_count: int = 0
    last_error: Optional[str] = None
    cleaned_for_window: Optional[str] = None
    last_ranked_count: int = 0
    last_created: int = 0
    last_skipped: int = 0
    last_failed: int = 0
    last_skip_reasons: list[str] = Field(default_factory=list)
