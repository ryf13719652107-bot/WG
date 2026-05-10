import csv
import io
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select, func, delete
from sqlalchemy.ext.asyncio import AsyncSession
from ..database import get_db
from ..models.trade import Trade
from ..schemas.trade import TradeResponse, TradeListResponse

router = APIRouter(prefix="/api/trades", tags=["trades"])


def _symbol_search_clause(symbol: str | None):
    """按交易对模糊匹配：输入 ETH 可匹配 ETH/USDT:USDT、ETHUSDT 等。"""
    s = (symbol or "").strip()
    if not s:
        return None
    esc = s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pat = f"%{esc}%"
    return Trade.symbol.ilike(pat, escape="\\")


@router.get("", response_model=TradeListResponse)
async def list_trades(
    symbol: str | None = None,
    side: str | None = None,  # 'long' or 'short'
    strategy_id: int | None = None,
    account_id: int | None = None,
    close_reason: str | None = Query(
        default=None,
        description="平仓原因精确匹配；传 tp_sl 表示仅止盈或止损",
    ),
    limit: int = Query(default=50, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Trade).order_by(Trade.exit_time.desc())
    count_stmt = select(func.count(Trade.id))

    sym_clause = _symbol_search_clause(symbol)
    if sym_clause is not None:
        stmt = stmt.where(sym_clause)
        count_stmt = count_stmt.where(sym_clause)

    if side:
        stmt = stmt.where(Trade.side == side)
        count_stmt = count_stmt.where(Trade.side == side)

    if strategy_id is not None:
        stmt = stmt.where(Trade.strategy_id == strategy_id)
        count_stmt = count_stmt.where(Trade.strategy_id == strategy_id)

    if account_id is not None:
        stmt = stmt.where(Trade.account_id == account_id)
        count_stmt = count_stmt.where(Trade.account_id == account_id)

    if close_reason:
        if close_reason == "tp_sl":
            cr_clause = Trade.close_reason.in_(("take_profit", "stop_loss"))
            stmt = stmt.where(cr_clause)
            count_stmt = count_stmt.where(cr_clause)
        else:
            stmt = stmt.where(Trade.close_reason == close_reason)
            count_stmt = count_stmt.where(Trade.close_reason == close_reason)

    stmt = stmt.offset(offset).limit(limit)
    result = await db.execute(stmt)
    trades = result.scalars().all()

    count_result = await db.execute(count_stmt)
    total = count_result.scalar() or 0

    return TradeListResponse(
        trades=[TradeResponse.model_validate(t) for t in trades],
        total=total,
    )


@router.delete("/{trade_id}", status_code=204)
async def delete_trade(trade_id: int, db: AsyncSession = Depends(get_db)):
    trade = await db.get(Trade, trade_id)
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")
    await db.delete(trade)
    await db.commit()


@router.delete("", status_code=204)
async def delete_filtered_trades(
    symbol: str | None = None,
    side: str | None = None,
    account_id: int | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Delete trades matching filters. If no filters provided, deletes ALL."""
    stmt = delete(Trade)
    sym_clause = _symbol_search_clause(symbol)
    if sym_clause is not None:
        stmt = stmt.where(sym_clause)
    if side:
        stmt = stmt.where(Trade.side == side)
    if account_id is not None:
        stmt = stmt.where(Trade.account_id == account_id)
    await db.execute(stmt)
    await db.commit()


@router.get("/export")
async def export_trades(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Trade).order_by(Trade.exit_time.desc()).limit(10000))
    trades = result.scalars().all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "ID", "Symbol", "Side", "Quantity", "Entry Price", "Exit Price",
        "Realized PnL", "PnL %", "Entry Time", "Exit Time", "Layer", "Close Reason"
    ])
    for t in trades:
        writer.writerow([
            t.id, t.symbol, t.side, t.quantity, t.entry_price, t.exit_price,
            t.realized_pnl, t.pnl_pct, t.entry_time, t.exit_time, t.layer, t.close_reason
        ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=trades.csv"},
    )
