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


class AccountResponse(BaseModel):
    id: int
    name: str
    exchange: str
    masked_key: str
    testnet: bool
    hedge_mode: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
