from pydantic import BaseModel, Field, model_validator
from datetime import datetime
from typing import Optional


class AccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    exchange: str = Field(default="binance", pattern="^(binance|okx)$")
    api_key: str = Field(min_length=1)
    api_secret: str = Field(min_length=1)
    okx_passphrase: str | None = None
    testnet: bool = True
    hedge_mode: bool = True

    @model_validator(mode="after")
    def okx_requires_passphrase(self):
        if self.exchange == "okx" and not (self.okx_passphrase or "").strip():
            raise ValueError("OKX 必须填写 API 口令（Passphrase），否则无法签名请求")
        return self


class AccountEquityGuardUpdate(BaseModel):
    """账户总资产止损：floor_u=0 关闭；当前总权益 < floor_u 时停止本账户全部策略。"""
    equity_stop_floor_u: float = Field(ge=0, description="止损下限 USDT，0=不启用")

    @model_validator(mode="after")
    def floor_is_usdt(self):
        if self.equity_stop_floor_u < 0:
            raise ValueError("止损下限不能为负数")
        return self


class AccountResponse(BaseModel):
    id: int
    name: str
    exchange: str
    masked_key: str
    testnet: bool
    hedge_mode: bool
    equity_stop_floor_u: float = 0.0
    equity_baseline_u: float | None = None
    equity_baseline_at: datetime | None = None
    equity_stop_triggered: bool = False
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
