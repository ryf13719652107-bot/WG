from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ..database import get_db
from ..config import now_beijing
from ..models.position import Position
from ..schemas.position import PositionResponse
from ..services.exchange_factory import get_exchange_service
from ..services.exchange_base import BaseExchangeService

router = APIRouter(prefix="/api/positions", tags=["positions"])


def _total_contracts_on_leg(rows: list | None, symbol: str, side: str) -> float:
    want = BaseExchangeService._norm_sym(symbol)
    sl = (side or "").lower()
    total = 0.0
    for pos in rows or []:
        if BaseExchangeService._norm_sym(pos.get("symbol") or "") != want:
            continue
        if (pos.get("side") or "").lower() != sl:
            continue
        total += float(pos.get("contracts", 0) or 0)
    return total


@router.get("", response_model=list[PositionResponse])
async def list_positions(
    strategy_id: int | None = None,
    symbol: str | None = None,
    account_id: int | None = None,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Position).where(Position.closed_at.is_(None))
    if strategy_id is not None:
        stmt = stmt.where(Position.strategy_id == strategy_id)
    if symbol:
        stmt = stmt.where(Position.symbol == symbol)
    if account_id is not None:
        stmt = stmt.where(Position.account_id == account_id)
    result = await db.execute(stmt)
    positions = list(result.scalars().all())

    now = now_beijing()
    dirty = False
    for p in positions:
        if p.opened_at is None:
            p.opened_at = now
            dirty = True
    if dirty:
        await db.commit()

    return [PositionResponse.model_validate(p) for p in positions]


@router.post("/{position_id}/close")
async def close_position(position_id: int, db: AsyncSession = Depends(get_db)):
    position = await db.get(Position, position_id)
    if not position or position.closed_at:
        raise HTTPException(status_code=404, detail="Position not found or already closed")

    exchange = await get_exchange_service(position.account_id)
    if not exchange:
        raise HTTPException(status_code=404, detail="Exchange service not available")

    # Cancel existing TP limit order before closing
    if position.tp_limit_order_id:
        try:
            await exchange.cancel_order(position.tp_limit_order_id, position.symbol)
        except Exception:
            pass

    result = await exchange.close_position(position.symbol, position.side)

    exit_price = 0.0
    close_reason = "manual"

    if result and result.get("id"):
        exit_price = float(result.get("average", 0) or result.get("price", 0) or 0)
    else:
        # close_position 在交易所已无持仓时返回空 —— 仍可清理本地未完成记录（常见：手工在交易所平仓后）
        fetched = await exchange.fetch_positions([position.symbol])
        if _total_contracts_on_leg(fetched, position.symbol, position.side) > 1e-12:
            raise HTTPException(
                status_code=500,
                detail="交易所仍能查到该方向的持仓数量，无法在本地清除；请稍后重试或仍在交易所平仓",
            )
        exit_price = float(position.mark_price or position.entry_price or 0)
        close_reason = "exchange_already_flat"

    if exit_price <= 0:
        exit_price = float(position.mark_price or position.entry_price or 0)

    from ..models.trade import Trade
    trade = Trade(
        strategy_id=position.strategy_id,
        account_id=position.account_id,
        symbol=position.symbol,
        side=position.side,
        quantity=position.quantity,
        entry_price=position.entry_price,
        exit_price=exit_price,
        realized_pnl=(exit_price - position.entry_price) * position.quantity if position.side == "long" else (position.entry_price - exit_price) * position.quantity,
        pnl_pct=round(((exit_price - position.entry_price) / position.entry_price * 100) if position.side == "long" else ((position.entry_price - exit_price) / position.entry_price * 100), 2),
        entry_time=position.opened_at or now_beijing(),
        exit_time=now_beijing(),
        layer=position.layer,
        grid_level=getattr(position, 'grid_level', 0),
        close_reason=close_reason,
    )
    db.add(trade)
    position.closed_at = now_beijing()
    await db.commit()

    return {"status": "closed", "id": position_id}
