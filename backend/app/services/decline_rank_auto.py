"""Decline-rank auto strategy coordinator: windowed ranking scan + create/teardown."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, time as dtime
from typing import Optional

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import now_beijing, BEIJING_TZ
from ..database import async_session
from ..models.bot_config import BotConfig
from ..models.account import Account
from ..models.strategy import Strategy
from ..schemas.strategy import (
    DeclineRankAutoConfig,
    DeclineRankAutoStatus,
    StrategyCreate,
    StrategyParamTemplateParams,
)
from .market_rankings import fetch_top_losers
from .strategy_lifecycle import (
    SOURCE_DECLINE_RANK,
    StrategyLifecycleError,
    create_strategy_record,
    find_existing_symbol_direction,
    teardown_strategy,
)
from .account_equity_guard import is_account_trading_halted

logger = logging.getLogger(__name__)

CONFIG_KEY = "decline_rank_auto_config"
STATE_KEY = "decline_rank_auto_state"

_lock = asyncio.Lock()


def _parse_hhmm(s: str) -> dtime:
    parts = (s or "00:00").strip().split(":")
    return dtime(hour=int(parts[0]), minute=int(parts[1]))


def is_in_window(now: datetime, start_hhmm: str, end_hhmm: str) -> bool:
    """Beijing-time window. Equal start/end = full day. Cross-midnight when start > end."""
    t = now.timetz().replace(tzinfo=None) if hasattr(now, "timetz") else now.time()
    # normalize to time without tz
    if getattr(t, "tzinfo", None) is not None:
        t = t.replace(tzinfo=None)
    start = _parse_hhmm(start_hhmm)
    end = _parse_hhmm(end_hhmm)
    if start == end:
        return True
    if start < end:
        return start <= t < end
    # cross midnight: e.g. 03:00 -> 00:00 means [03:00, 24:00) U [00:00, 00:00) i.e. t >= 03:00
    # For end=00:00: in window when t >= start (from start until midnight exclusive of next day's 00:00
    # which is equivalent to t >= start OR ... wait:
    # User case: start 03:00, end 00:00 (midnight). Active from 03:00 inclusive until 00:00 exclusive.
    # So active when t >= 03:00 (same calendar day) — at 00:00 window ends.
    # Cross-midnight general: in window if t >= start OR t < end.
    # For end=00:00: t >= start OR t < 00:00 → only t >= start (since t < 00:00 is never).
    return t >= start or t < end


def window_id_for(now: datetime, start_hhmm: str, end_hhmm: str) -> str:
    """Stable id for the active window, or the window that most recently ended (idle)."""
    start = _parse_hhmm(start_hhmm)
    end = _parse_hhmm(end_hhmm)
    d = now.date()
    t = now.time().replace(tzinfo=None) if now.tzinfo else now.time()
    if start == end:
        return d.isoformat()
    if start < end:
        # same-day window [start, end)
        if t < start:
            return (d - timedelta(days=1)).isoformat()
        return d.isoformat()
    # cross-midnight: window started on date D at `start`, ends next day at `end`
    if t >= start:
        return d.isoformat()
    if t < end:
        return (d - timedelta(days=1)).isoformat()
    # idle gap [end, start): last window started yesterday
    return (d - timedelta(days=1)).isoformat()


def default_config() -> DeclineRankAutoConfig:
    return DeclineRankAutoConfig()


async def load_config(db: AsyncSession) -> DeclineRankAutoConfig:
    row = await db.execute(select(BotConfig).where(BotConfig.key == CONFIG_KEY))
    cfg = row.scalar_one_or_none()
    if not cfg or not cfg.value:
        return default_config()
    try:
        data = json.loads(cfg.value)
        return DeclineRankAutoConfig.model_validate(data)
    except Exception as e:
        logger.warning("Invalid decline_rank_auto_config: %s", e)
        return default_config()


async def save_config(db: AsyncSession, config: DeclineRankAutoConfig) -> DeclineRankAutoConfig:
    if config.enabled:
        if not config.account_id:
            raise ValueError("启用自动策略时必须选择账户")
        account = await db.get(Account, config.account_id)
        if not account:
            raise ValueError("账户不存在")
    payload = config.model_dump_json()
    row = await db.execute(select(BotConfig).where(BotConfig.key == CONFIG_KEY))
    cfg = row.scalar_one_or_none()
    if cfg:
        cfg.value = payload
    else:
        db.add(BotConfig(key=CONFIG_KEY, value=payload))
    await db.commit()
    return config


async def _load_state(db: AsyncSession) -> dict:
    row = await db.execute(select(BotConfig).where(BotConfig.key == STATE_KEY))
    cfg = row.scalar_one_or_none()
    if not cfg or not cfg.value:
        return {}
    try:
        data = json.loads(cfg.value)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


async def _save_state(db: AsyncSession, state: dict) -> None:
    payload = json.dumps(state, ensure_ascii=False)
    row = await db.execute(select(BotConfig).where(BotConfig.key == STATE_KEY))
    cfg = row.scalar_one_or_none()
    if cfg:
        cfg.value = payload
    else:
        db.add(BotConfig(key=STATE_KEY, value=payload))
    await db.commit()


async def _master_switch_on(db: AsyncSession) -> bool:
    row = await db.execute(select(BotConfig).where(BotConfig.key == "master_switch"))
    sw = row.scalar_one_or_none()
    if not sw:
        return True
    return sw.value != "false"


async def count_auto_strategies(db: AsyncSession, account_id: Optional[int] = None) -> int:
    stmt = select(func.count()).select_from(Strategy).where(Strategy.source == SOURCE_DECLINE_RANK)
    if account_id is not None:
        stmt = stmt.where(Strategy.account_id == account_id)
    return int((await db.execute(stmt)).scalar() or 0)


def _parse_state_datetime(raw: str | None) -> Optional[datetime]:
    """Parse state timestamps as naive Beijing time (matches now_beijing())."""
    if not raw:
        return None
    text = str(raw).strip()
    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(BEIJING_TZ).replace(tzinfo=None)
    return dt


async def get_status(db: AsyncSession) -> DeclineRankAutoStatus:
    config = await load_config(db)
    state = await _load_state(db)
    now = now_beijing()
    in_win = False
    wid = None
    if config.enabled and config.account_id:
        in_win = is_in_window(now, config.start_time, config.end_time)
        wid = window_id_for(now, config.start_time, config.end_time)

    next_refresh = None
    last_refresh = state.get("last_refresh_at")
    if in_win and last_refresh and config.refresh_interval_min > 0:
        lr = _parse_state_datetime(last_refresh)
        if lr is not None:
            nr = lr + timedelta(minutes=config.refresh_interval_min)
            next_refresh = nr.isoformat(sep=" ", timespec="seconds")
    elif in_win and not last_refresh:
        next_refresh = "立即"

    auto_count = await count_auto_strategies(db, config.account_id)
    return DeclineRankAutoStatus(
        enabled=config.enabled,
        in_window=in_win,
        window_id=wid,
        last_refresh_at=last_refresh,
        next_refresh_at=next_refresh,
        current_symbols=list(state.get("current_symbols") or []),
        auto_strategy_count=auto_count,
        last_error=state.get("last_error"),
        cleaned_for_window=state.get("cleaned_for_window"),
    )


async def cleanup_auto_strategies(
    db: AsyncSession,
    account_id: int,
    *,
    close_reason: str = "decline_rank_window_end",
) -> dict:
    """Teardown all decline_rank strategies for account. Isolates per-strategy failures."""
    result = await db.execute(
        select(Strategy).where(
            Strategy.account_id == account_id,
            Strategy.source == SOURCE_DECLINE_RANK,
        )
    )
    strategies = list(result.scalars().all())
    deleted, failed = 0, 0
    errors: list[str] = []
    for s in strategies:
        sid = s.id
        sym = s.symbol
        try:
            await teardown_strategy(sid, db, close_reason=close_reason, require_exchange_if_open=False)
            deleted += 1
        except Exception as e:
            failed += 1
            errors.append(f"{sym}(id={sid}): {e}")
            logger.exception("decline_rank cleanup strategy %d failed: %s", sid, e)
            try:
                await db.rollback()
            except Exception:
                pass
    return {"deleted": deleted, "failed": failed, "total": len(strategies), "errors": errors[:20]}


async def _create_and_start_from_rank(
    db: AsyncSession,
    config: DeclineRankAutoConfig,
    symbols: list[str],
) -> dict:
    created, skipped, failed = 0, 0, 0
    errors: list[str] = []
    params: StrategyParamTemplateParams = config.params
    direction = config.direction
    account_id = int(config.account_id)

    if await is_account_trading_halted(account_id, db):
        return {
            "created": 0,
            "skipped": 0,
            "failed": 0,
            "errors": ["账户总资产止损已触发，跳过自动建仓"],
        }

    from .scheduler import strategy_scheduler

    for sym in symbols:
        existing = await find_existing_symbol_direction(db, account_id, sym, direction)
        if existing:
            # 已存在同币同向：不重复创建；若是自动策略且已停止则补启
            if (
                getattr(existing, "source", None) == SOURCE_DECLINE_RANK
                and existing.status in ("stopped", "error")
            ):
                ok = await strategy_scheduler.add_strategy(existing.id, session=db)
                if ok:
                    created += 1
                    logger.info(
                        "decline_rank restarted existing strategy %d %s %s",
                        existing.id,
                        sym,
                        direction,
                    )
                else:
                    failed += 1
                    errors.append(f"{sym}: 已有策略但启动失败")
            else:
                skipped += 1
            continue
        try:
            data = StrategyCreate(
                account_id=account_id,
                direction=direction,
                symbol=sym,
                base_qty_value=params.base_qty_value,
                max_layers=params.max_layers,
                tp_pct=params.tp_pct,
                grid_drop_base_pct=params.grid_drop_base_pct,
                grid_interval_multiplier=params.grid_interval_multiplier,
                position_multiplier=params.position_multiplier,
                cumulative_loss_threshold_u=params.cumulative_loss_threshold_u,
                reopen_after_close=params.reopen_after_close,
                source=SOURCE_DECLINE_RANK,
            )
            strategy = await create_strategy_record(data, db, commit=True)
            ok = await strategy_scheduler.add_strategy(strategy.id, session=db)
            if ok:
                created += 1
                logger.info(
                    "decline_rank created+started strategy %d %s %s",
                    strategy.id,
                    sym,
                    direction,
                )
            else:
                failed += 1
                errors.append(f"{sym}: 创建成功但启动失败")
        except StrategyLifecycleError as e:
            if e.code == "conflict":
                skipped += 1
            else:
                failed += 1
                errors.append(f"{sym}: {e.message}")
        except Exception as e:
            failed += 1
            errors.append(f"{sym}: {e}")
            logger.exception("decline_rank create %s failed", sym)
            try:
                await db.rollback()
            except Exception:
                pass
    return {"created": created, "skipped": skipped, "failed": failed, "errors": errors[:20]}


async def refresh_once(db: AsyncSession, config: Optional[DeclineRankAutoConfig] = None) -> dict:
    """Fetch losers and create missing strategies."""
    config = config or await load_config(db)
    if not config.enabled or not config.account_id:
        return {"ok": False, "reason": "disabled"}

    account = await db.get(Account, config.account_id)
    if not account:
        return {"ok": False, "reason": "account_missing"}

    exchange = (account.exchange or "binance").lower()
    ranked = await fetch_top_losers(exchange, limit=config.top_n, use_cache=True)
    symbols = [r["symbol"] for r in ranked]
    create_stats = await _create_and_start_from_rank(db, config, symbols)

    state = await _load_state(db)
    now = now_beijing()
    state["last_refresh_at"] = now.isoformat(sep=" ", timespec="seconds")
    state["current_symbols"] = symbols
    state["last_error"] = None
    if create_stats.get("errors"):
        state["last_error"] = "; ".join(create_stats["errors"][:3])
    state["active_window_id"] = window_id_for(now, config.start_time, config.end_time)
    await _save_state(db, state)
    return {"ok": True, "symbols": symbols, **create_stats}


async def tick() -> None:
    """Minute-level supervisor: enter window / refresh / leave window cleanup."""
    if _lock.locked():
        logger.debug("decline_rank tick skipped (busy)")
        return
    async with _lock:
        async with async_session() as db:
            try:
                config = await load_config(db)
                if not config.enabled or not config.account_id:
                    return

                now = now_beijing()
                in_win = is_in_window(now, config.start_time, config.end_time)
                wid = window_id_for(now, config.start_time, config.end_time)
                state = await _load_state(db)

                if not in_win:
                    # 窗口结束清理不依赖总开关，避免总开关关闭导致自动策略残留
                    cleaned = state.get("cleaned_for_window")
                    auto_n = await count_auto_strategies(db, config.account_id)
                    target_clean_id = wid
                    if auto_n > 0 and cleaned != target_clean_id:
                        logger.info(
                            "decline_rank window end cleanup account=%s window=%s count=%d",
                            config.account_id,
                            target_clean_id,
                            auto_n,
                        )
                        stats = await cleanup_auto_strategies(db, config.account_id)
                        state = await _load_state(db)
                        # 仅在全部成功时标记已清理，失败则下次 tick 重试
                        if not stats.get("failed"):
                            state["cleaned_for_window"] = target_clean_id
                            state["current_symbols"] = []
                            state["last_error"] = None
                        else:
                            state["last_error"] = (
                                f"清理失败 {stats['failed']}: "
                                f"{';'.join(stats.get('errors') or [])}"
                            )
                        await _save_state(db, state)
                    return

                # 建仓/刷新仍遵守总开关
                if not await _master_switch_on(db):
                    return

                # Inside window: clear cleaned marker for this window so end can clean again
                if state.get("cleaned_for_window") == wid:
                    state["cleaned_for_window"] = None
                    await _save_state(db, state)
                    state = await _load_state(db)

                last_refresh = state.get("last_refresh_at")
                need_refresh = False
                if not last_refresh or state.get("active_window_id") != wid:
                    need_refresh = True
                else:
                    lr = _parse_state_datetime(last_refresh)
                    if lr is None:
                        need_refresh = True
                    else:
                        elapsed = (now - lr).total_seconds()
                        if elapsed >= config.refresh_interval_min * 60:
                            need_refresh = True

                if need_refresh:
                    await refresh_once(db, config)
            except Exception as e:
                logger.exception("decline_rank tick failed: %s", e)
                try:
                    state = await _load_state(db)
                    state["last_error"] = str(e)
                    await _save_state(db, state)
                except Exception:
                    pass


async def pause_auto(*, cleanup: bool = True) -> dict:
    """Disable auto mode; optionally teardown all decline_rank strategies immediately."""
    async with async_session() as db:
        config = await load_config(db)
        account_id = config.account_id
        config.enabled = False
        await save_config(db, config)
        cleanup_stats = {"deleted": 0, "failed": 0, "total": 0, "errors": []}
        if cleanup and account_id:
            cleanup_stats = await cleanup_auto_strategies(
                db, int(account_id), close_reason="decline_rank_paused",
            )
            state = await _load_state(db)
            state["current_symbols"] = []
            if not cleanup_stats.get("failed"):
                state["last_error"] = None
                state["cleaned_for_window"] = window_id_for(
                    now_beijing(), config.start_time, config.end_time,
                )
            else:
                state["last_error"] = (
                    f"暂停清理失败 {cleanup_stats['failed']}: "
                    f"{';'.join(cleanup_stats.get('errors') or [])}"
                )
            await _save_state(db, state)
        return {"enabled": False, "cleanup": cleanup, **cleanup_stats}


# singleton-style export for scheduler
async def decline_rank_auto_tick() -> None:
    await tick()
