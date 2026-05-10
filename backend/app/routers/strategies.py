import time
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional
from ..database import get_db
from ..models.strategy import Strategy
from ..models.position import Position
from ..schemas.strategy import StrategyCreate, StrategyUpdate, StrategyResponse
from ..services.scheduler import strategy_scheduler
from ..services.exchange_factory import get_exchange_service, clear_all_cache

router = APIRouter(prefix="/api/strategies", tags=["strategies"])


def _panic_symbol_key(sym: str) -> str:
    return (sym or "").replace("/", "").replace(":USDT", "").replace("-SWAP", "").upper()


def _norm_sym(s: str) -> str:
    return (s or "").replace("/", "").replace(":USDT", "").replace("-SWAP", "").upper().strip()


@router.post("", response_model=StrategyResponse)
async def create_strategy(data: StrategyCreate, db: AsyncSession = Depends(get_db)):
    from ..models.account import Account
    account = await db.get(Account, data.account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    # Constraint: max 2 strategies per symbol (one long + one short)
    norm_sym = _norm_sym(data.symbol)
    result = await db.execute(
        select(Strategy).where(
            Strategy.account_id == data.account_id,
            Strategy.symbol.is_not(None),
        )
    )
    existing = result.scalars().all()
    same_sym = [s for s in existing if _norm_sym(s.symbol or "") == norm_sym]
    if len(same_sym) >= 2:
        raise HTTPException(
            status_code=400,
            detail=f"币种 {data.symbol} 已有 2 个策略（一多一空），不可再创建",
        )
    # Check direction conflict
    for s in same_sym:
        if s.direction == data.direction:
            raise HTTPException(
                status_code=400,
                detail=f"币种 {data.symbol} 已存在同方向策略（ID={s.id}），每币种只允许一多一空",
            )

    strategy = Strategy(**data.model_dump())
    db.add(strategy)
    await db.commit()
    await db.refresh(strategy)
    return StrategyResponse.model_validate(strategy)


@router.get("", response_model=list[StrategyResponse])
async def list_strategies(
    status: Optional[str] = None,
    account_id: Optional[int] = None,
    symbol: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Strategy)
    if status:
        stmt = stmt.where(Strategy.status == status)
    if account_id is not None:
        stmt = stmt.where(Strategy.account_id == account_id)
    if symbol:
        stmt = stmt.where(Strategy.symbol == symbol)
    result = await db.execute(stmt)
    return [StrategyResponse.model_validate(s) for s in result.scalars().all()]


@router.get("/{strategy_id}", response_model=StrategyResponse)
async def get_strategy(strategy_id: int, db: AsyncSession = Depends(get_db)):
    strategy = await db.get(Strategy, strategy_id)
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return StrategyResponse.model_validate(strategy)


@router.put("/{strategy_id}", response_model=StrategyResponse)
async def update_strategy(
    strategy_id: int, data: StrategyUpdate, db: AsyncSession = Depends(get_db)
):
    strategy = await db.get(Strategy, strategy_id)
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")

    was_running = strategy.status == "running"
    if was_running:
        await strategy_scheduler.remove_strategy(strategy_id)

    for key, val in data.model_dump(exclude_unset=True).items():
        setattr(strategy, key, val)
    await db.commit()
    await db.refresh(strategy)

    if was_running:
        strategy_scheduler.start()
        await strategy_scheduler.add_strategy(strategy_id, session=db)

    return StrategyResponse.model_validate(strategy)


@router.delete("/{strategy_id}", status_code=204)
async def delete_strategy(strategy_id: int, db: AsyncSession = Depends(get_db)):
    strategy = await db.get(Strategy, strategy_id)
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")
    if strategy.status == "running":
        await strategy_scheduler.remove_strategy(strategy_id)
    await db.delete(strategy)
    await db.commit()


@router.post("/{strategy_id}/start")
async def start_strategy(strategy_id: int, db: AsyncSession = Depends(get_db)):
    strategy = await db.get(Strategy, strategy_id)
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")

    await strategy_scheduler.add_strategy(strategy_id, session=db)
    return {"status": "running", "id": strategy_id}


@router.post("/{strategy_id}/stop")
async def stop_strategy(strategy_id: int, db: AsyncSession = Depends(get_db)):
    strategy = await db.get(Strategy, strategy_id)
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")
    await strategy_scheduler.remove_strategy(strategy_id)
    await db.refresh(strategy)
    await db.commit()
    return {"status": "stopped", "id": strategy_id}


@router.post("/{strategy_id}/panic-close")
async def panic_close_strategy(strategy_id: int, db: AsyncSession = Depends(get_db)):
    """Emergency close: 直接市价平仓，不等待撤单."""
    from ..models.account import Account
    from ..models.trade import Trade
    from ..config import now_beijing
    import logging
    import asyncio

    strategy = await db.get(Strategy, strategy_id)
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")

    exchange = await get_exchange_service(strategy.account_id)
    if not exchange:
        raise HTTPException(status_code=404, detail="Exchange service not available")

    # 先快速清除内存追踪
    from ..services.order_tracker import order_tracker
    order_tracker.clear_strategy(strategy_id)

    # 异步取消挂单（不等待结果，让平仓先走）
    async def _cancel_bg():
        try:
            open_orders = await exchange.fetch_open_orders(strategy.symbol)
            tasks = []
            for oo in (open_orders or []):
                oid = str(oo.get("id", ""))
                if oid:
                    tasks.append(exchange.cancel_order(oid, strategy.symbol))
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        except Exception:
            pass
    asyncio.create_task(_cancel_bg())

    try:
        raw_positions = await exchange.fetch_positions()
    except Exception as e:
        logging.error("Panic close: fetch_positions failed: %s", e)
        raise HTTPException(status_code=502, detail=f"无法获取交易所持仓: {e}")

    exchange_map: dict[tuple[str, str], float] = {}
    for ep in raw_positions:
        contracts = float(ep.get("contracts", 0) or 0)
        if contracts <= 0:
            continue
        sym = (ep.get("symbol") or "").replace("/", "").replace(":USDT", "").replace("-SWAP", "")
        sd = (ep.get("side") or "").lower()
        exchange_map[(sym, sd)] = exchange_map.get((sym, sd), 0) + contracts

    if not exchange_map:
        stmt_ling = select(Position).where(
            Position.account_id == strategy.account_id,
            Position.closed_at.is_(None),
        )
        lingering = list((await db.execute(stmt_ling)).scalars().all())
        now0 = now_beijing()
        for p in lingering:
            exit_price = float(p.mark_price or p.entry_price or 0)
            if exit_price <= 0:
                exit_price = p.entry_price
            ep = exit_price
            pnl = (ep - p.entry_price) * p.quantity if p.side == "long" else (p.entry_price - ep) * p.quantity
            pct = ((ep - p.entry_price) / p.entry_price * 100) if p.side == "long" and p.entry_price > 0 else ((p.entry_price - ep) / p.entry_price * 100) if p.entry_price > 0 else 0
            trade = Trade(
                strategy_id=p.strategy_id,
                account_id=strategy.account_id,
                symbol=p.symbol,
                side=p.side,
                quantity=p.quantity,
                entry_price=p.entry_price,
                exit_price=ep,
                realized_pnl=pnl,
                pnl_pct=round(pct, 2),
                entry_time=p.opened_at or now0,
                exit_time=now0,
                layer=p.layer,
                grid_level=p.grid_level if hasattr(p, 'grid_level') else 0,
                close_reason="panic_close",
            )
            db.add(trade)
            p.closed_at = now0
        if lingering:
            await db.commit()
        await strategy_scheduler.remove_strategy(strategy_id)
        return {"closed": 0, "failed": 0, "results": [], "id": strategy_id, "db_cleaned": len(lingering)}

    results = []
    now = now_beijing()

    for (symbol, side), contracts in exchange_map.items():
        close_side = "sell" if side == "long" else "buy"
        ps = "LONG" if side == "long" else "SHORT"
        try:
            order = await exchange.create_market_order(
                symbol, close_side, contracts, reduce_only=True, position_side=ps,
            )
            exit_price = float(order.get("average", 0) or order.get("price", 0) or 0)
            results.append({"symbol": symbol, "side": side, "status": "ok", "exit_price": exit_price})
        except Exception as e:
            results.append({"symbol": symbol, "side": side, "status": "failed", "error": str(e)})
            logging.error("Panic close: failed %s %s: %s", symbol, side, e)

    for r in results:
        if r["status"] != "ok":
            continue
        symbol = r["symbol"]
        side = r["side"]
        exit_price_v = r.get("exit_price", 0) or 0
        stmt_open = select(Position).where(
            Position.account_id == strategy.account_id,
            Position.closed_at.is_(None),
        )
        open_rows = list((await db.execute(stmt_open)).scalars().all())
        sym_u = _panic_symbol_key(symbol)
        matching = [p for p in open_rows if _panic_symbol_key(p.symbol) == sym_u and p.side.lower() == side.lower()]
        for p in matching:
            ep = exit_price_v if exit_price_v > 0 else (p.mark_price or p.entry_price)
            pnl = (ep - p.entry_price) * p.quantity if p.side == "long" else (p.entry_price - ep) * p.quantity
            pct = ((ep - p.entry_price) / p.entry_price * 100) if p.side == "long" else ((p.entry_price - ep) / p.entry_price * 100)
            trade = Trade(
                strategy_id=p.strategy_id, account_id=strategy.account_id,
                symbol=p.symbol, side=p.side, quantity=p.quantity,
                entry_price=p.entry_price, exit_price=ep,
                realized_pnl=pnl, pnl_pct=round(pct, 2),
                entry_time=p.opened_at or now, exit_time=now,
                layer=p.layer,
                grid_level=p.grid_level if hasattr(p, 'grid_level') else 0,
                close_reason="panic_close",
            )
            db.add(trade)
            p.closed_at = now

    await db.commit()
    await strategy_scheduler.remove_strategy(strategy_id)

    closed_count = sum(1 for r in results if r["status"] == "ok")
    failed_count = sum(1 for r in results if r["status"] == "failed")
    return {"closed": closed_count, "failed": failed_count, "results": results, "id": strategy_id}


@router.get("/{strategy_id}/exchange-positions")
async def get_exchange_positions(strategy_id: int, db: AsyncSession = Depends(get_db)):
    strategy = await db.get(Strategy, strategy_id)
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")

    exchange = await get_exchange_service(strategy.account_id)
    if not exchange:
        raise HTTPException(status_code=502, detail="Exchange service not available")

    try:
        positions = await exchange.fetch_positions()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch positions: {e}")

    result = []
    for p in positions:
        contracts = float(p.get("contracts", 0) or 0)
        if contracts > 0:
            symbol = _panic_symbol_key(p.get("symbol") or "")
            side = (p.get("side") or "").lower()
            entry_price = float(p.get("entryPrice", 0) or 0)
            mark_price = float(p.get("markPrice", 0) or 0)
            notional = float(p.get("notional", 0) or 0)
            pnl = float(p.get("unrealizedPnl", 0) or 0)
            pnl_pct = 0.0
            if entry_price > 0:
                if side == "short":
                    pnl_pct = (entry_price - mark_price) / entry_price * 100
                else:
                    pnl_pct = (mark_price - entry_price) / entry_price * 100
            result.append({
                "symbol": symbol,
                "side": side,
                "usdt": round(notional, 0),
                "entry_price": round(entry_price, 4),
                "mark_price": round(mark_price, 4),
                "unrealized_pnl": round(pnl, 2),
                "pnl_pct": round(pnl_pct, 2),
            })
    return result


@router.get("/{strategy_id}/logs")
async def get_strategy_logs(strategy_id: int, limit: int = 50):
    from ..services.log_service import strategy_log_service
    return strategy_log_service.get(strategy_id, limit)


@router.get("/{strategy_id}/health")
async def get_strategy_health(strategy_id: int):
    from ..services.health_monitor import health_monitor
    h = health_monitor.get_health(strategy_id)
    return {
        "strategy_id": strategy_id,
        "status": h.status.value,
        "consecutive_failures": h.consecutive_failures,
        "last_successful_tick_age": round(time.time() - h.last_successful_tick, 1),
        "last_order_latency_ms": round(h.last_order_latency_ms, 1),
        "checks": h.checks,
        "messages": h.messages[-10:],
    }
