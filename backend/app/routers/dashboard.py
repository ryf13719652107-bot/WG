import asyncio
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func, case
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import timedelta
from ..database import get_db
from ..config import now_beijing
from ..models.strategy import Strategy
from ..models.trade import Trade
from ..models.bot_config import BotConfig
from ..models.account import Account
from ..schemas.dashboard import (
    DashboardSnapshot,
    StrategyStatItem,
    SlEventItem,
    SpecialSlRestartItem,
    TradingWindowStatus,
)
from ..services.exchange_factory import get_exchange_service
from ..services.exchange_base import BaseExchangeService

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

# 仪表盘余额/全仓持仓走 REST，易被限流；短时缓存可显著减少 fetch_balance + fetch_positions
_DASHBOARD_EXCHANGE_CACHE_TTL_SEC = 60.0
_dashboard_exchange_cache: dict[int, tuple[float, dict[str, Any]]] = {}


def _dashboard_exchange_cache_get(account_id: int) -> dict[str, Any] | None:
    row = _dashboard_exchange_cache.get(account_id)
    if not row:
        return None
    ts, payload = row
    if time.monotonic() - ts > _DASHBOARD_EXCHANGE_CACHE_TTL_SEC:
        return None
    return payload


def _dashboard_exchange_cache_set(account_id: int, payload: dict[str, Any]) -> None:
    _dashboard_exchange_cache[account_id] = (time.monotonic(), payload)


async def _fetch_dashboard_exchange_slice(exchange) -> dict[str, Any]:
    """REST: balance + positions for dashboard header. Does not touch DB."""
    total_balance = 0.0
    available_balance = 0.0
    balance_status = "error"
    open_positions = 0
    unrealized_pnl = 0.0
    unrealized_pnl_long = 0.0
    unrealized_pnl_short = 0.0
    total_notional = 0.0
    exchange_positions: list[dict] = []
    ex_id = getattr(exchange, "exchange_id", "") or type(exchange).__name__
    try:
        # OKX 首次请求会 load_markets()，在海外或弱网下易超过 8s，导致误判「余额获取失败」
        balance = await asyncio.wait_for(exchange.fetch_balance(), timeout=25.0)
        total_balance = float(balance.get("total", {}).get("USDT", 0) or 0)
        available_balance = float(balance.get("free", {}).get("USDT", 0) or 0)
        balance_status = "ok"
    except asyncio.TimeoutError:
        logging.error("Balance fetch timeout in dashboard slice (%s)", ex_id)
        balance_status = "error"
    except Exception as e:
        logging.error("Balance fetch error in dashboard slice (%s): %s", ex_id, e)
        balance_status = "error"

    if balance_status == "ok":
        try:
            positions = await asyncio.wait_for(exchange.fetch_positions(), timeout=25.0)
            for p in positions:
                contracts = BaseExchangeService.position_row_contracts_abs(p)
                if contracts <= 0:
                    continue
                side = BaseExchangeService.position_row_side_lower(p)
                if side not in ("long", "short"):
                    continue
                open_positions += 1
                entry_price = float(p.get("entryPrice", 0) or 0)
                mark_price = float(p.get("markPrice", 0) or 0)
                symbol = BaseExchangeService._norm_sym(str(p.get("symbol") or ""))
                upnl = float(p.get("unrealizedPnl", 0) or 0)
                unrealized_pnl += upnl
                if side == "short":
                    unrealized_pnl_short += upnl
                else:
                    unrealized_pnl_long += upnl
                pnl_pct = 0.0
                if entry_price > 0:
                    if side == "short":
                        pnl_pct = (entry_price - mark_price) / entry_price * 100
                    else:
                        pnl_pct = (mark_price - entry_price) / entry_price * 100
                notional = float(p.get("notional", 0) or 0)
                if abs(notional) < 1e-12 and contracts > 0 and mark_price > 0:
                    cs = float(p.get("contractSize", 1) or 1)
                    notional = abs(contracts * mark_price * cs)
                total_notional += notional
                exchange_positions.append({
                    "symbol": symbol,
                    "side": side,
                    "usdt": round(notional, 2),
                    "contracts": contracts,
                    "entry_price": round(entry_price, 4),
                    "mark_price": round(mark_price, 4),
                    "unrealized_pnl": round(float(p.get("unrealizedPnl", 0) or 0), 2),
                    "pnl_pct": round(pnl_pct, 2),
                })
        except Exception as e:
            logging.error("Position fetch error for dashboard (%s): %s", ex_id, e)

    leverage_multiplier = 0.0
    if total_balance > 0 and total_notional > 0:
        leverage_multiplier = round(total_notional / total_balance, 2)

    return {
        "total_balance": total_balance,
        "available_balance": available_balance,
        "balance_status": balance_status,
        "open_positions": open_positions,
        "unrealized_pnl": unrealized_pnl,
        "unrealized_pnl_long": unrealized_pnl_long,
        "unrealized_pnl_short": unrealized_pnl_short,
        "leverage_multiplier": leverage_multiplier,
        "exchange_positions": exchange_positions,
    }


@router.get("", response_model=DashboardSnapshot)
async def get_dashboard(
    account_id: int | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    total_balance = 0.0
    available_balance = 0.0
    leverage_multiplier = 0.0
    account_name = ""
    balance_status = "no_account"
    account = None
    exchange = None
    filter_account_id = account_id
    open_positions = 0
    unrealized_pnl = 0.0
    unrealized_pnl_long = 0.0
    unrealized_pnl_short = 0.0
    exchange_positions: list[dict] = []

    # Fetch balance and positions from Binance
    try:
        if filter_account_id:
            result = await db.execute(select(Account).where(Account.id == filter_account_id))
            account = result.scalar()
        else:
            result = await db.execute(select(Account).order_by(Account.id).limit(1))
            account = result.scalar()

        if account:
            account_name = account.name
            filter_account_id = account.id
            try:
                exchange = await get_exchange_service(account.id)
                if exchange:
                    cached = (
                        _dashboard_exchange_cache_get(filter_account_id)
                        if filter_account_id is not None
                        else None
                    )
                    if cached is not None:
                        total_balance = float(cached["total_balance"])
                        available_balance = float(cached["available_balance"])
                        balance_status = str(cached["balance_status"])
                        open_positions = int(cached["open_positions"])
                        unrealized_pnl = float(cached["unrealized_pnl"])
                        unrealized_pnl_long = float(cached["unrealized_pnl_long"])
                        unrealized_pnl_short = float(cached["unrealized_pnl_short"])
                        leverage_multiplier = float(cached["leverage_multiplier"])
                        exchange_positions = list(cached["exchange_positions"])
                    else:
                        ex = await _fetch_dashboard_exchange_slice(exchange)
                        total_balance = float(ex["total_balance"])
                        available_balance = float(ex["available_balance"])
                        balance_status = str(ex["balance_status"])
                        open_positions = int(ex["open_positions"])
                        unrealized_pnl = float(ex["unrealized_pnl"])
                        unrealized_pnl_long = float(ex["unrealized_pnl_long"])
                        unrealized_pnl_short = float(ex["unrealized_pnl_short"])
                        leverage_multiplier = float(ex["leverage_multiplier"])
                        exchange_positions = list(ex["exchange_positions"])
                        if balance_status == "ok" and filter_account_id is not None:
                            _dashboard_exchange_cache_set(filter_account_id, ex)
                else:
                    balance_status = "no_account"
            except asyncio.TimeoutError:
                balance_status = "error"
            except Exception as e:
                logging.error("Balance fetch error for account %s: %s", account.name, e)
                balance_status = "error"
    except Exception:
        balance_status = "error"

    # Active strategies count (filtered by account)
    strat_stmt = select(func.count(Strategy.id)).where(Strategy.status == "running")
    if filter_account_id:
        strat_stmt = strat_stmt.where(Strategy.account_id == filter_account_id)
    result = await db.execute(strat_stmt)
    active_strategies = result.scalar() or 0

    # Open positions & unrealized PnL: filled above from cache or _fetch_dashboard_exchange_slice

    # Daily trades and PnL (today 00:00 Beijing, filtered by account)
    today_start = now_beijing().replace(hour=0, minute=0, second=0, microsecond=0)
    trade_stmt = select(Trade).where(Trade.exit_time >= today_start)
    if filter_account_id:
        trade_stmt = trade_stmt.where(Trade.account_id == filter_account_id)
    result = await db.execute(trade_stmt)
    daily_trades = result.scalars().all()
    daily_trade_count = len(daily_trades)
    daily_pnl = sum(float(t.realized_pnl) for t in daily_trades)
    daily_pnl_long = sum(float(t.realized_pnl) for t in daily_trades if t.side == "long")
    daily_pnl_short = sum(float(t.realized_pnl) for t in daily_trades if t.side == "short")

    # Win rate (today)
    winning = sum(1 for t in daily_trades if t.realized_pnl > 0)
    win_rate = (winning / daily_trade_count * 100) if daily_trade_count > 0 else 0

    # All-time stats from trades (same account filter as daily)
    agg_stmt = select(
        func.count(Trade.id),
        func.coalesce(func.sum(Trade.realized_pnl), 0.0),
        func.coalesce(
            func.sum(case((Trade.realized_pnl > 0, 1), else_=0)),
            0,
        ),
    )
    if filter_account_id:
        agg_stmt = agg_stmt.where(Trade.account_id == filter_account_id)
    agg_row = (await db.execute(agg_stmt)).one()
    total_trades_n = int(agg_row[0] or 0)
    total_realized = float(agg_row[1] or 0)
    total_wins_n = int(agg_row[2] or 0)
    total_win_rate = (total_wins_n / total_trades_n * 100) if total_trades_n > 0 else 0.0

    legs_stmt = select(
        func.coalesce(
            func.sum(case((Trade.side == "long", Trade.realized_pnl), else_=0.0)),
            0.0,
        ),
        func.coalesce(
            func.sum(case((Trade.side == "short", Trade.realized_pnl), else_=0.0)),
            0.0,
        ),
    )
    if filter_account_id:
        legs_stmt = legs_stmt.where(Trade.account_id == filter_account_id)
    legs_row = (await db.execute(legs_stmt)).one()
    total_pnl_long_v = float(legs_row[0] or 0)
    total_pnl_short_v = float(legs_row[1] or 0)

    # Daily PnL %
    daily_pnl_pct = round(daily_pnl / total_balance * 100, 2) if total_balance > 0 else 0.0

    # Master switch
    result = await db.execute(
        select(BotConfig).where(BotConfig.key == "master_switch")
    )
    master_config = result.scalar()
    master_switch = master_config.value == "true" if master_config else False

    # Strategy-level TP/SL stats
    strategy_stats: list[StrategyStatItem] = []
    today_start = now_beijing().replace(hour=0, minute=0, second=0, microsecond=0)
    strat_stmt = select(Strategy)
    if filter_account_id:
        strat_stmt = strat_stmt.where(Strategy.account_id == filter_account_id)
    strat_result = await db.execute(strat_stmt)
    all_strategies = strat_result.scalars().all()
    for s in all_strategies:
        tp_total = 0
        tp_today = 0
        sl_events: list[SlEventItem] = []
        if s.started_at:
            tp_stmt = select(func.count(Trade.exit_time.distinct())).where(
                Trade.strategy_id == s.id,
                Trade.close_reason == "take_profit",
                Trade.exit_time >= s.started_at,
            )
            tp_total = (await db.execute(tp_stmt)).scalar() or 0
            tp_stmt_today = select(func.count(Trade.exit_time.distinct())).where(
                Trade.strategy_id == s.id,
                Trade.close_reason == "take_profit",
                Trade.exit_time >= s.started_at,
                Trade.exit_time >= today_start,
            )
            tp_today = (await db.execute(tp_stmt_today)).scalar() or 0
            sl_stmt = select(Trade).where(
                Trade.strategy_id == s.id,
                Trade.close_reason == "stop_loss",
                Trade.exit_time >= s.started_at,
            ).order_by(Trade.exit_time.desc()).limit(5)
            sl_rows = (await db.execute(sl_stmt)).scalars().all()
            for t in sl_rows:
                sl_events.append(SlEventItem(
                    time=t.exit_time.strftime("%Y-%m-%d %H:%M:%S") if t.exit_time else "",
                    exit_price=float(t.exit_price) if t.exit_price else 0,
                    quantity=float(t.quantity) if t.quantity else 0,
                ))
        strategy_stats.append(StrategyStatItem(
            strategy_id=s.id,
            symbol=s.symbol,
            direction=s.direction,
            status=s.status,
            tp_total=tp_total,
            tp_today=tp_today,
            sl_events=sl_events,
        ))

    special_sl_restarts: list[SpecialSlRestartItem] = []
    dust_stmt = (
        select(
            Trade.strategy_id,
            Trade.exit_time,
            func.max(Trade.symbol).label("sym"),
            func.max(Strategy.symbol).label("strat_sym"),
            func.max(Strategy.direction).label("direction"),
            func.max(Trade.exit_price).label("exit_price"),
            func.coalesce(func.sum(Trade.quantity), 0.0).label("qty_sum"),
            func.coalesce(func.sum(Trade.realized_pnl), 0.0).label("pnl_sum"),
        )
        .join(Strategy, Trade.strategy_id == Strategy.id)
        .where(Trade.close_reason == "stop_loss_dust_restart")
        .group_by(Trade.strategy_id, Trade.exit_time)
        .order_by(Trade.exit_time.desc())
        .limit(50)
    )
    if filter_account_id:
        dust_stmt = dust_stmt.where(Trade.account_id == filter_account_id)
    for row in (await db.execute(dust_stmt)).all():
        ts_key = row.exit_time.strftime("%Y-%m-%d %H:%M:%S") if row.exit_time else ""
        special_sl_restarts.append(SpecialSlRestartItem(
            strategy_id=int(row.strategy_id),
            symbol=str(row.strat_sym or row.sym or ""),
            direction=str(row.direction or ""),
            time=ts_key,
            exit_price=float(row.exit_price or 0.0),
            quantity=float(row.qty_sum or 0.0),
            realized_pnl=float(row.pnl_sum or 0.0),
        ))

    from ..services.trading_schedule import (
        get_trading_window_config,
        is_within_trading_window,
    )
    tw_cfg = await get_trading_window_config()
    trading_window = TradingWindowStatus(
        enabled=tw_cfg.enabled,
        start_hm=tw_cfg.start_hm,
        end_hm=tw_cfg.end_hm,
        within_window=is_within_trading_window(cfg=tw_cfg),
    )

    return DashboardSnapshot(
        total_balance=round(total_balance, 2),
        available_balance=round(available_balance, 2),
        unrealized_pnl=round(unrealized_pnl, 2),
        unrealized_pnl_long=round(unrealized_pnl_long, 2),
        unrealized_pnl_short=round(unrealized_pnl_short, 2),
        daily_pnl=round(daily_pnl, 2),
        daily_pnl_long=round(daily_pnl_long, 2),
        daily_pnl_short=round(daily_pnl_short, 2),
        daily_pnl_pct=daily_pnl_pct,
        active_strategies=active_strategies,
        open_positions=open_positions,
        daily_trades=daily_trade_count,
        win_rate_pct=round(win_rate, 2),
        total_realized_pnl=round(total_realized, 2),
        total_trades=total_trades_n,
        total_win_rate_pct=round(total_win_rate, 2),
        total_pnl_long=round(total_pnl_long_v, 2),
        total_pnl_short=round(total_pnl_short_v, 2),
        leverage_multiplier=leverage_multiplier,
        master_switch=master_switch,
        account_name=account_name,
        balance_status=balance_status,
        exchange_positions=exchange_positions,
        strategy_stats=strategy_stats,
        special_sl_restarts=special_sl_restarts,
        trading_window=trading_window,
    )
