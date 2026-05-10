import asyncio
import time
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, delete as sql_delete
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional
from ..database import get_db
from ..models.strategy import Strategy
from ..models.position import Position
from ..schemas.strategy import StrategyCreate, StrategyUpdate, StrategyResponse
from ..services.scheduler import strategy_scheduler
from ..services.exchange_factory import get_exchange_service, clear_all_cache
from ..services.exchange_base import BaseExchangeService

router = APIRouter(prefix="/api/strategies", tags=["strategies"])


def _panic_symbol_key(sym: str) -> str:
    return BaseExchangeService._norm_sym(sym)


def _norm_sym(s: str) -> str:
    return BaseExchangeService._norm_sym(s)


async def _flatten_strategy_orders_and_positions(
    strategy: Strategy,
    db: AsyncSession,
    *,
    close_reason: str,
    use_order_tracker: bool,
) -> tuple[bool, float, float]:
    """紧急平仓等价逻辑：撤单（普通+条件/algo）、按 DB 开仓记录市价全平、写 trades、标记 positions 关闭。

    返回 (平仓是否名义成功, 平仓总数量, 成交均价或0).
    """
    from ..models.trade import Trade
    from ..config import now_beijing
    from ..services.exchange_factory import get_exchange_service
    from ..services.log_service import strategy_log_service
    from ..services.order_tracker import order_tracker
    import logging

    strategy_id = strategy.id
    symbol = strategy.symbol
    direction = strategy.direction
    position_side = "LONG" if direction == "long" else "SHORT"
    close_side = "sell" if direction == "long" else "buy"

    exchange = await get_exchange_service(strategy.account_id)
    if not exchange:
        logging.error("_flatten_strategy: no exchange strategy=%d", strategy_id)
        return False, 0.0, 0.0

    cancel_tasks: list = []

    if use_order_tracker:
        pending = order_tracker.get_active_for_strategy(strategy_id)
        for o in pending:
            if BaseExchangeService._norm_sym(o.symbol) != BaseExchangeService._norm_sym(symbol):
                continue
            if o.purpose == "stop_loss":
                cancel_tasks.append(exchange.cancel_algo_order(o.order_id, o.symbol))
            else:
                cancel_tasks.append(exchange.cancel_order(o.order_id, o.symbol))
        order_tracker.clear_strategy(strategy_id)

    if cancel_tasks:
        await asyncio.gather(*cancel_tasks, return_exceptions=True)

    # OKX：普通限价与触发/计划单分域，仅靠 tracker + 单次 fetch_open_orders 撤不干净
    if getattr(exchange, "exchange_id", None) == "okx" and hasattr(
        exchange, "cancel_all_pending_orders_for_symbol"
    ):
        try:
            n_bulk = await exchange.cancel_all_pending_orders_for_symbol(symbol)
            if n_bulk:
                logging.info(
                    "_flatten_strategy: OKX cancelled %d pending order ops strategy=%d",
                    n_bulk,
                    strategy_id,
                )
        except Exception as e:
            logging.warning("_flatten_strategy: OKX cancel_all_pending_orders strategy=%d: %s", strategy_id, e)

    try:
        oo_list = await exchange.fetch_open_orders(symbol)
        oo_cancel = []
        for oo in oo_list or []:
            oid = str(oo.get("id") or oo.get("orderId") or "")
            if oid:
                oo_cancel.append(exchange.cancel_order(oid, symbol))
        if oo_cancel:
            await asyncio.gather(*oo_cancel, return_exceptions=True)
    except Exception as e:
        logging.warning("_flatten_strategy: fetch_open_orders/cancel strategy=%d: %s", strategy_id, e)

    if hasattr(exchange, "cancel_all_open_algo_orders"):
        try:
            n = await exchange.cancel_all_open_algo_orders(symbol)
            if n:
                logging.info("_flatten_strategy: cancelled %d open algo orders strategy=%d", n, strategy_id)
        except Exception as e:
            logging.warning("_flatten_strategy: cancel_all_open_algo_orders strategy=%d: %s", strategy_id, e)

    stmt = select(Position).where(
        Position.strategy_id == strategy_id,
        Position.closed_at.is_(None),
    )
    result = await db.execute(stmt)
    positions = list(result.scalars().all())
    total_qty = sum(float(p.quantity) for p in positions)

    # 以交易所实时持仓为准（OKX 合约张数、hedge 下 side 常与 DB 有偏差），避免紧急平仓数量为 0 或下单被拒
    exchange_qty = 0.0
    try:
        raw_pos = await exchange.fetch_positions([symbol])
        want = BaseExchangeService._norm_sym(symbol)
        d = direction.lower()
        for rp in raw_pos or []:
            if BaseExchangeService._norm_sym(str(rp.get("symbol") or "")) != want:
                continue
            p_side = BaseExchangeService.position_row_side_lower(rp)
            if p_side != d:
                continue
            exchange_qty += BaseExchangeService.position_row_contracts_abs(rp)
    except Exception as e:
        logging.warning("_flatten_strategy fetch_positions strategy=%d: %s", strategy_id, e)

    close_qty = exchange_qty if exchange_qty > 1e-12 else total_qty
    close_qty = await exchange.normalize_order_amount(symbol, close_qty)

    close_success = False
    exit_price = 0.0
    if close_qty > 1e-12:
        try:
            order = await exchange.create_market_order(
                symbol, close_side, close_qty,
                reduce_only=True, position_side=position_side,
            )
            close_success = True
            exit_price = float(order.get("average", 0) or order.get("price", 0) or 0)
            if close_reason == "panic_close":
                strategy_log_service.success(
                    strategy_id,
                    f"紧急平仓成功: {symbol} 数量={close_qty:.4f} 价格={exit_price:.4f}",
                )
            else:
                strategy_log_service.success(
                    strategy_id,
                    f"策略删除平仓: {symbol} 数量={close_qty:.4f} 价格={exit_price:.4f}",
                )
        except Exception as e:
            logging.error("_flatten_strategy market close failed strategy=%d %s: %s", strategy_id, symbol, e)
            try:
                order2 = await exchange.close_position(symbol, direction)
                if order2:
                    close_success = True
                    exit_price = float(order2.get("average", 0) or order2.get("price", 0) or 0)
                    if close_reason == "panic_close":
                        strategy_log_service.success(
                            strategy_id,
                            f"紧急平仓成功(交易所 close_position): {symbol} 价格={exit_price:.4f}",
                        )
                    else:
                        strategy_log_service.success(
                            strategy_id,
                            f"策略删除平仓成功(交易所 close_position): {symbol} 价格={exit_price:.4f}",
                        )
                else:
                    if close_reason == "panic_close":
                        strategy_log_service.error(
                            strategy_id,
                            f"紧急平仓失败: {e}（close_position 未找到持仓或返回空）",
                        )
                    else:
                        strategy_log_service.error(
                            strategy_id,
                            f"删除策略平仓失败: {e}（close_position 未找到持仓或返回空）",
                        )
            except Exception as e2:
                logging.error("_flatten_strategy close_position fallback failed strategy=%d: %s", strategy_id, e2)
                if close_reason == "panic_close":
                    strategy_log_service.error(strategy_id, f"紧急平仓失败: {e}；备用: {e2}")
                else:
                    strategy_log_service.error(strategy_id, f"删除策略平仓失败: {e}；备用: {e2}")
    else:
        close_success = True
        if close_reason == "panic_close":
            strategy_log_service.info(strategy_id, "紧急平仓: 无持仓需要平仓")
        else:
            strategy_log_service.info(strategy_id, "删除策略: 无持仓需要平仓")

    now = now_beijing()
    for p in positions:
        ep = exit_price if exit_price > 0 else (p.mark_price or p.entry_price)
        pnl = (ep - p.entry_price) * p.quantity if p.side == "long" else (p.entry_price - ep) * p.quantity
        pct = (
            ((ep - p.entry_price) / p.entry_price * 100)
            if p.side == "long" and p.entry_price > 0
            else ((p.entry_price - ep) / p.entry_price * 100)
            if p.entry_price > 0
            else 0
        )
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
            entry_time=p.opened_at or now,
            exit_time=now,
            layer=p.layer,
            grid_level=p.grid_level if hasattr(p, "grid_level") else 0,
            close_reason=close_reason,
        )
        db.add(trade)
        p.closed_at = now

    await db.flush()
    qty_report = close_qty if close_qty > 1e-12 else total_qty
    return close_success, qty_report, exit_price


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

    if not await get_exchange_service(strategy.account_id):
        raise HTTPException(status_code=502, detail="Exchange service not available")

    was_running = strategy.status == "running"
    if was_running:
        await strategy_scheduler.remove_strategy(strategy_id)
        strategy = await db.get(Strategy, strategy_id)
        if not strategy:
            raise HTTPException(status_code=404, detail="Strategy not found")

    await _flatten_strategy_orders_and_positions(
        strategy,
        db,
        close_reason="策略删除",
        use_order_tracker=not was_running,
    )
    # 平仓记录已写入 trades；删除持仓行，避免仅存 closed_at 脏数据
    await db.execute(sql_delete(Position).where(Position.strategy_id == strategy_id))
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
    """Emergency close: 只处理当前策略的持仓和订单，平仓后暂停策略."""
    strategy = await db.get(Strategy, strategy_id)
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")

    if not await get_exchange_service(strategy.account_id):
        raise HTTPException(status_code=502, detail="Exchange service not available")

    close_success, total_qty, exit_price = await _flatten_strategy_orders_and_positions(
        strategy,
        db,
        close_reason="panic_close",
        use_order_tracker=True,
    )

    symbol = strategy.symbol
    await strategy_scheduler.remove_strategy(strategy_id)
    strategy.status = "stopped"
    await db.commit()

    return {
        "closed": 1 if close_success else 0,
        "failed": 0 if close_success else 1,
        "symbol": symbol,
        "quantity": total_qty,
        "exit_price": exit_price,
        "id": strategy_id,
    }


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
        contracts = BaseExchangeService.position_row_contracts_abs(p)
        if contracts <= 0:
            continue
        symbol = _panic_symbol_key(p.get("symbol") or "")
        side = BaseExchangeService.position_row_side_lower(p)
        if side not in ("long", "short"):
            continue
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
