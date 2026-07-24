"""Reusable strategy create / teardown helpers (no FastAPI HTTPException)."""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.strategy import Strategy
from ..models.account import Account
from ..models.position import Position
from ..schemas.strategy import StrategyCreate
from .exchange_base import BaseExchangeService
from .exchange_factory import get_exchange_service

logger = logging.getLogger(__name__)

SOURCE_MANUAL = "manual"
SOURCE_DECLINE_RANK = "decline_rank"


class StrategyLifecycleError(Exception):
    def __init__(self, message: str, *, code: str = "error"):
        super().__init__(message)
        self.code = code
        self.message = message


def _norm_sym(s: str) -> str:
    return BaseExchangeService._norm_sym(s)


async def create_strategy_record(
    data: StrategyCreate,
    db: AsyncSession,
    *,
    commit: bool = True,
) -> Strategy:
    """Validate uniqueness and persist a strategy row."""
    account = await db.get(Account, data.account_id)
    if not account:
        raise StrategyLifecycleError("Account not found", code="not_found")

    norm_sym = _norm_sym(data.symbol)
    result = await db.execute(select(Strategy).where(Strategy.account_id == data.account_id))
    existing = result.scalars().all()
    same_sym = [s for s in existing if _norm_sym(s.symbol or "") == norm_sym]
    if len(same_sym) >= 2:
        raise StrategyLifecycleError(
            f"币种 {data.symbol} 已有 2 个策略（一多一空），不可再创建",
            code="conflict",
        )
    for s in same_sym:
        if s.direction == data.direction:
            raise StrategyLifecycleError(
                f"币种 {data.symbol} 已存在同方向策略（ID={s.id}），每币种只允许一多一空",
                code="conflict",
            )

    payload = data.model_dump()
    payload["base_qty_type"] = "usdt"
    payload["symbol"] = norm_sym
    payload.setdefault("source", SOURCE_MANUAL)
    payload.setdefault("stop_loss_close_pct", 100.0)
    strategy = Strategy(**payload)
    db.add(strategy)
    if commit:
        await db.commit()
        await db.refresh(strategy)
    else:
        await db.flush()
        await db.refresh(strategy)
    return strategy


async def teardown_strategy(
    strategy_id: int,
    db: AsyncSession,
    *,
    close_reason: str = "strategy_deleted",
    require_exchange_if_open: bool = False,
) -> None:
    """Stop scheduler, flatten orders/positions, purge related rows, delete strategy."""
    from ..routers.strategies import (
        _flatten_strategy_orders_and_positions,
        _purge_strategy_records,
    )
    from .scheduler import strategy_scheduler

    strategy = await db.get(Strategy, strategy_id)
    if not strategy:
        raise StrategyLifecycleError("Strategy not found", code="not_found")

    open_pos = await db.execute(
        select(Position).where(
            Position.strategy_id == strategy_id,
            Position.closed_at.is_(None),
        )
    )
    has_open = open_pos.scalars().first() is not None
    exchange = await get_exchange_service(strategy.account_id)

    was_running = strategy.status == "running"
    if was_running:
        if not exchange and require_exchange_if_open:
            raise StrategyLifecycleError(
                "策略运行中但无法连接交易所",
                code="exchange_unavailable",
            )
        try:
            await strategy_scheduler.remove_strategy(strategy_id)
        except Exception as e:
            logger.warning("teardown remove_strategy %d: %s", strategy_id, e)
        strategy = await db.get(Strategy, strategy_id)
        if not strategy:
            return

    if has_open and not exchange:
        # 有未平仓却连不上交易所时绝不能只删库，否则会留下交易所真实仓位
        raise StrategyLifecycleError(
            "仍有未平持仓但无法连接交易所",
            code="exchange_unavailable",
        )

    if exchange:
        try:
            await _flatten_strategy_orders_and_positions(
                strategy,
                db,
                close_reason=close_reason,
                use_order_tracker=not was_running,
            )
        except Exception as e:
            logger.exception("teardown flatten strategy %d: %s", strategy_id, e)
            if has_open:
                raise StrategyLifecycleError(
                    f"平仓失败，已中止删除: {e}",
                    code="flatten_failed",
                ) from e

    await _purge_strategy_records(strategy_id, db)
    await db.delete(strategy)
    await db.commit()


async def find_existing_symbol_direction(
    db: AsyncSession,
    account_id: int,
    symbol: str,
    direction: str,
) -> Optional[Strategy]:
    norm = _norm_sym(symbol)
    result = await db.execute(select(Strategy).where(Strategy.account_id == account_id))
    for s in result.scalars().all():
        if _norm_sym(s.symbol or "") == norm and s.direction == direction:
            return s
    return None
