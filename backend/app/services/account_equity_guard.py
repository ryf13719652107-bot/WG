"""账户总资产止损：策略启动时记录基准权益，每分钟检查，跌破下限则停止该账户全部策略。"""
import asyncio
import logging
import time
from typing import Optional

from sqlalchemy import func, select

from ..config import now_beijing
from ..database import async_session
from ..models.account import Account
from ..models.strategy import Strategy
from .exchange_factory import get_exchange_service

logger = logging.getLogger(__name__)

_HALT_LOCKS: dict[int, asyncio.Lock] = {}
# 策略 tick 内检查节流：避免单账户 50+ 策略每 30s 各打一次 fetch_balance
_LAST_TICK_EQUITY_CHECK: dict[int, float] = {}
_TICK_EQUITY_CHECK_INTERVAL_SEC = 55.0


def _halt_lock(account_id: int) -> asyncio.Lock:
    if account_id not in _HALT_LOCKS:
        _HALT_LOCKS[account_id] = asyncio.Lock()
    return _HALT_LOCKS[account_id]


async def fetch_total_equity_usdt(exchange) -> float:
    """合约账户 USDT 总权益（与仪表盘 total_balance 一致）。"""
    balance = await exchange.fetch_balance()
    return float(balance.get("total", {}).get("USDT", 0) or 0)


async def record_baseline_on_strategy_start(
    account_id: int,
    session,
    *,
    strategy_id: int,
) -> Optional[float]:
    """本账户首个运行中策略启动时记入初始总权益。返回写入的基准值。"""
    account = await session.get(Account, account_id)
    if not account:
        return None
    floor_u = float(getattr(account, "equity_stop_floor_u", 0) or 0)
    if floor_u <= 0:
        return None

    r = await session.execute(
        select(func.count(Strategy.id)).where(
            Strategy.account_id == account_id,
            Strategy.status == "running",
            Strategy.id != strategy_id,
        )
    )
    if int(r.scalar() or 0) > 0:
        return float(account.equity_baseline_u or 0) or None

    exchange = await get_exchange_service(account_id)
    if not exchange:
        logger.warning("equity guard: no exchange for account %d, skip baseline", account_id)
        return None

    try:
        equity = await fetch_total_equity_usdt(exchange)
    except Exception as e:
        logger.warning("equity guard: baseline fetch failed account=%d: %s", account_id, e)
        return None

    if equity <= 0:
        logger.warning("equity guard: baseline equity<=0 account=%d", account_id)
        return None

    account.equity_baseline_u = equity
    account.equity_baseline_at = now_beijing()
    account.equity_stop_triggered = False
    await session.flush()
    logger.info(
        "Account %d equity baseline recorded: %.4f USDT (floor=%.4f)",
        account_id, equity, floor_u,
    )
    return equity


async def is_account_trading_halted(account_id: int, session=None) -> bool:
    if session is not None:
        acc = await session.get(Account, account_id)
        return bool(acc and getattr(acc, "equity_stop_triggered", False))
    async with async_session() as s:
        acc = await s.get(Account, account_id)
        return bool(acc and getattr(acc, "equity_stop_triggered", False))


async def halt_account_trading(
    account_id: int,
    *,
    current_equity: float,
    floor_u: float,
    baseline_u: float | None = None,
) -> int:
    """总资产止损触发：市价平掉各策略持仓、撤单并停止本账户全部 running 策略。"""
    async with _halt_lock(account_id):
        async with async_session() as session:
            account = await session.get(Account, account_id)
            if not account:
                return 0
            if account.equity_stop_triggered:
                return 0
            account.equity_stop_triggered = True
            r = await session.execute(
                select(Strategy).where(
                    Strategy.account_id == account_id,
                    Strategy.status == "running",
                )
            )
            strategies = list(r.scalars().all())
            await session.commit()

        if not strategies:
            return 0

        from .scheduler import strategy_scheduler
        from .log_service import strategy_log_service
        from ..routers.strategies import _flatten_strategy_orders_and_positions

        close_ok_n = 0
        for strategy in strategies:
            sid = strategy.id
            try:
                await strategy_scheduler.remove_strategy(sid)
            except Exception as e:
                logger.error("halt account %d: remove_strategy %d failed: %s", account_id, sid, e)

            try:
                async with async_session() as session:
                    s = await session.get(Strategy, sid)
                    if not s:
                        continue
                    close_ok, qty, exit_px = await _flatten_strategy_orders_and_positions(
                        s,
                        session,
                        close_reason="equity_stop",
                        use_order_tracker=True,
                    )
                    s.status = "stopped"
                    await session.commit()
                    if close_ok:
                        close_ok_n += 1
                    detail = (
                        f"账户总资产止损：当前权益≈{current_equity:.4f}U < 下限{floor_u:.4f}U；"
                        f"已撤单并{'市价平仓成功' if close_ok else '尝试市价平仓未完全确认'} "
                        f"({s.symbol} {s.direction}"
                        + (f" 数量≈{qty:.4f} 价≈{exit_px:.4f}" if qty > 0 else "")
                        + ")，策略已停止"
                    )
                    if close_ok:
                        strategy_log_service.error(sid, detail)
                    else:
                        strategy_log_service.warning(sid, detail)
            except Exception as e:
                logger.error(
                    "halt account %d: flatten strategy %d failed: %s", account_id, sid, e,
                )
                strategy_log_service.error(
                    sid,
                    f"账户总资产止损：市价平仓异常 {e}，策略已停止，请到交易所核对持仓",
                )

        msg = (
            f"当前总权益≈{current_equity:.4f} USDT，已低于止损下限 {floor_u:.4f} USDT；"
            f"已停止本账户全部 {len(strategies)} 个运行中策略，"
            f"其中 {close_ok_n} 个策略交易所确认市价平仓成功。"
        )
        if baseline_u and baseline_u > 0:
            msg += f" 启动时基准权益≈{baseline_u:.4f} USDT。"
        logger.warning("Account %d equity stop triggered: %s", account_id, msg)

        return len(strategies)


async def should_run_equity_check_on_tick(account_id: int) -> bool:
    """策略 tick 是否应做完整权益拉取（已节流；分钟任务不受此限制）。"""
    async with async_session() as session:
        acc = await session.get(Account, account_id)
        if not acc or float(getattr(acc, "equity_stop_floor_u", 0) or 0) <= 0:
            return False
    now = time.monotonic()
    last = _LAST_TICK_EQUITY_CHECK.get(account_id, 0.0)
    return (now - last) >= _TICK_EQUITY_CHECK_INTERVAL_SEC


async def check_account_equity_guard(account_id: int, *, from_tick: bool = False) -> bool:
    """检查账户权益止损。返回 True 表示已触发/已处于停止状态。"""
    if from_tick and not await should_run_equity_check_on_tick(account_id):
        return await is_account_trading_halted(account_id)

    async with async_session() as session:
        account = await session.get(Account, account_id)
        if not account:
            return False
        floor_u = float(getattr(account, "equity_stop_floor_u", 0) or 0)
        if floor_u <= 0:
            return False
        if account.equity_stop_triggered:
            return True

        r = await session.execute(
            select(func.count(Strategy.id)).where(
                Strategy.account_id == account_id,
                Strategy.status == "running",
            )
        )
        if int(r.scalar() or 0) <= 0:
            return False

        baseline = float(account.equity_baseline_u or 0)
        exchange = await get_exchange_service(account_id)
        if not exchange:
            return False

        if baseline <= 0:
            try:
                equity0 = await fetch_total_equity_usdt(exchange)
            except Exception as e:
                logger.warning("equity guard: backfill baseline account=%d: %s", account_id, e)
                return False
            if equity0 <= 0:
                return False
            account.equity_baseline_u = equity0
            account.equity_baseline_at = now_beijing()
            await session.commit()
            baseline = equity0
            logger.info("Account %d equity baseline backfilled: %.4f", account_id, equity0)

    try:
        equity = await fetch_total_equity_usdt(exchange)
    except Exception as e:
        logger.warning("equity guard check account=%d fetch failed: %s", account_id, e)
        return False

    if from_tick:
        _LAST_TICK_EQUITY_CHECK[account_id] = time.monotonic()

    if equity >= floor_u:
        return False

    await halt_account_trading(
        account_id,
        current_equity=equity,
        floor_u=floor_u,
        baseline_u=baseline,
    )
    return True


async def equity_guard_tick() -> None:
    """每分钟：对所有配置了止损下限的账户做权益检查。"""
    async with async_session() as session:
        r = await session.execute(
            select(Account.id).where(Account.equity_stop_floor_u > 0)
        )
        account_ids = [int(row[0]) for row in r.all()]

    for aid in account_ids:
        try:
            await check_account_equity_guard(aid)
        except Exception as e:
            logger.warning("equity_guard_tick account %d: %s", aid, e)
