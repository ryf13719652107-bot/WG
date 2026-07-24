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


def _naive_time(now: datetime) -> dtime:
    t = now.timetz().replace(tzinfo=None) if hasattr(now, "timetz") else now.time()
    if getattr(t, "tzinfo", None) is not None:
        t = t.replace(tzinfo=None)
    return t


def is_in_window(now: datetime, start_hhmm: str, end_hhmm: str) -> bool:
    """日历时间是否落在 [start, end)（支持跨午夜；start==end 表示全天）。"""
    t = _naive_time(now)
    start = _parse_hhmm(start_hhmm)
    end = _parse_hhmm(end_hhmm)
    if start == end:
        return True
    if start < end:
        return start <= t < end
    # 跨午夜：如 03:00→00:00，表示当天 03:00 起至次日 00:00 前
    return t >= start or t < end


def window_id_for(now: datetime, start_hhmm: str, end_hhmm: str) -> str:
    """Stable id for the active window, or the window that most recently ended (idle)."""
    start = _parse_hhmm(start_hhmm)
    end = _parse_hhmm(end_hhmm)
    d = now.date()
    t = _naive_time(now)
    if start == end:
        return d.isoformat()
    if start < end:
        if t < start:
            return (d - timedelta(days=1)).isoformat()
        return d.isoformat()
    if t >= start:
        return d.isoformat()
    if t < end:
        return (d - timedelta(days=1)).isoformat()
    return (d - timedelta(days=1)).isoformat()


def window_start_datetime(now: datetime, start_hhmm: str, end_hhmm: str) -> datetime:
    """当前日历窗口的开始时刻（若此刻不在窗口内，则为「即将开始的下一窗口」开始时刻）。"""
    start = _parse_hhmm(start_hhmm)
    if is_in_window(now, start_hhmm, end_hhmm):
        wid = window_id_for(now, start_hhmm, end_hhmm)
        return datetime.combine(datetime.fromisoformat(wid).date(), start)
    return next_start_datetime(now, start_hhmm, end_hhmm)


def next_start_datetime(now: datetime, start_hhmm: str, end_hhmm: str) -> datetime:
    """下一次允许「开盘进场」的开始时间。

    规则：每天只在 start_time 开盘。若今天的 start 已过且尚未进入本窗口会话，
    则等到明天的 start（例如 start=03:00、现在=18:42 → 次日 03:00）。
    """
    start = _parse_hhmm(start_hhmm)
    today_start = datetime.combine(now.date(), start)
    if now < today_start:
        return today_start
    # 今天开盘时刻已过 → 下一交易日开盘
    return today_start + timedelta(days=1)


def is_near_window_start(
    now: datetime,
    start_hhmm: str,
    end_hhmm: str,
    *,
    grace_minutes: int = 3,
) -> bool:
    """是否刚到达本窗口开盘（供分钟级调度捕捉 start 边界）。"""
    if not is_in_window(now, start_hhmm, end_hhmm):
        return False
    start_dt = window_start_datetime(now, start_hhmm, end_hhmm)
    if now < start_dt:
        return False
    return (now - start_dt) <= timedelta(minutes=max(1, grace_minutes))


def resolve_session(
    now: datetime,
    start_hhmm: str,
    end_hhmm: str,
    *,
    session_window_id: Optional[str],
    has_auto_strategies: bool,
    grace_minutes: int = 3,
) -> dict:
    """决定此刻是否应真正跑自动建仓。

    - 日历不在窗口：结束/等待
    - 已有本窗口 session，或已有自动策略（重启恢复）：继续
    - 刚到开盘时刻：开新 session
    - 否则（开盘已过才启用）：等待次日开盘，不中途进场
    """
    cal_in = is_in_window(now, start_hhmm, end_hhmm)
    wid = window_id_for(now, start_hhmm, end_hhmm)
    nxt = next_start_datetime(now, start_hhmm, end_hhmm)

    if not cal_in:
        return {
            "calendar_in_window": False,
            "session_active": False,
            "window_id": wid,
            "session_window_id": None,
            "waiting_next_start": True,
            "next_session_at": nxt.isoformat(sep=" ", timespec="minutes"),
            "enter_session": False,
        }

    if session_window_id == wid:
        return {
            "calendar_in_window": True,
            "session_active": True,
            "window_id": wid,
            "session_window_id": wid,
            "waiting_next_start": False,
            "next_session_at": None,
            "enter_session": False,
        }

    if has_auto_strategies:
        # 进程重启等：窗口内已有自动策略则恢复会话，避免误等次日
        return {
            "calendar_in_window": True,
            "session_active": True,
            "window_id": wid,
            "session_window_id": wid,
            "waiting_next_start": False,
            "next_session_at": None,
            "enter_session": True,
        }

    if is_near_window_start(now, start_hhmm, end_hhmm, grace_minutes=grace_minutes):
        return {
            "calendar_in_window": True,
            "session_active": True,
            "window_id": wid,
            "session_window_id": wid,
            "waiting_next_start": False,
            "next_session_at": None,
            "enter_session": True,
        }

    # 开盘已过才启用：等到次日 start
    return {
        "calendar_in_window": True,
        "session_active": False,
        "window_id": wid,
        "session_window_id": session_window_id,
        "waiting_next_start": True,
        "next_session_at": nxt.isoformat(sep=" ", timespec="minutes"),
        "enter_session": False,
    }


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


async def save_config(
    db: AsyncSession,
    config: DeclineRankAutoConfig,
    *,
    cleanup_on_disable: bool = False,
) -> DeclineRankAutoConfig:
    prev = await load_config(db)
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

    # 关闭启用时可选立即清理（走 PUT，避免旧进程无 POST /pause 导致 405）
    if cleanup_on_disable and prev.enabled and not config.enabled:
        aid = prev.account_id or config.account_id
        if aid:
            stats = await cleanup_auto_strategies(
                db, int(aid), close_reason="decline_rank_paused",
            )
            state = await _load_state(db)
            state["current_symbols"] = []
            if not stats.get("failed"):
                state["last_error"] = None
                state["cleaned_for_window"] = window_id_for(
                    now_beijing(), config.start_time or prev.start_time, config.end_time or prev.end_time,
                )
            else:
                state["last_error"] = (
                    f"暂停清理失败 {stats['failed']}: "
                    f"{';'.join(stats.get('errors') or [])}"
                )
            await _save_state(db, state)
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
    waiting = False
    next_session = None
    wid = None
    if config.enabled and config.account_id:
        auto_n = await count_auto_strategies(db, config.account_id)
        resolved = resolve_session(
            now,
            config.start_time,
            config.end_time,
            session_window_id=state.get("session_window_id"),
            has_auto_strategies=auto_n > 0,
            grace_minutes=3,
        )
        in_win = bool(resolved["session_active"])
        waiting = bool(resolved["waiting_next_start"])
        next_session = resolved.get("next_session_at")
        wid = resolved.get("window_id")

    next_refresh = None
    last_refresh = state.get("last_refresh_at")
    if in_win and last_refresh and config.refresh_interval_min > 0:
        lr = _parse_state_datetime(last_refresh)
        if lr is not None:
            nr = lr + timedelta(minutes=config.refresh_interval_min)
            next_refresh = nr.isoformat(sep=" ", timespec="seconds")
    elif in_win and not last_refresh:
        next_refresh = "立即"
    elif waiting and next_session:
        next_refresh = f"次日开盘 {next_session}"

    auto_count = await count_auto_strategies(db, config.account_id)
    stats = state.get("last_refresh_stats") if isinstance(state.get("last_refresh_stats"), dict) else {}
    active_syms: list[str] = []
    if config.account_id:
        rows = await db.execute(
            select(Strategy.symbol).where(
                Strategy.account_id == config.account_id,
                Strategy.source == SOURCE_DECLINE_RANK,
            )
        )
        active_syms = [str(r[0]) for r in rows.all() if r[0]]
        active_syms.sort()
    return DeclineRankAutoStatus(
        enabled=config.enabled,
        in_window=in_win,
        waiting_next_start=waiting,
        next_session_at=next_session,
        window_id=wid,
        last_refresh_at=last_refresh,
        next_refresh_at=next_refresh,
        current_symbols=list(state.get("current_symbols") or []),
        active_symbols=active_syms,
        auto_strategy_count=auto_count,
        last_error=state.get("last_error"),
        cleaned_for_window=state.get("cleaned_for_window"),
        last_ranked_count=int(stats.get("ranked") or 0),
        last_created=int(stats.get("created") or 0),
        last_skipped=int(stats.get("skipped") or 0),
        last_failed=int(stats.get("failed") or 0),
        last_skip_reasons=list(stats.get("skip_reasons") or [])[:20],
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
    skip_reasons: list[str] = []

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
                src = getattr(existing, "source", None) or "manual"
                skip_reasons.append(
                    f"{sym}: 已有同向策略(ID={existing.id},source={src},status={existing.status})"
                )
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
                skip_reasons.append(f"{sym}: {e.message}")
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
    return {
        "created": created,
        "skipped": skipped,
        "failed": failed,
        "errors": errors[:20],
        "skip_reasons": skip_reasons[:20],
    }


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
    state["last_refresh_stats"] = {
        "ranked": len(symbols),
        "created": create_stats.get("created", 0),
        "skipped": create_stats.get("skipped", 0),
        "failed": create_stats.get("failed", 0),
        "errors": list(create_stats.get("errors") or [])[:20],
        "skip_reasons": list(create_stats.get("skip_reasons") or [])[:20],
    }
    state["last_error"] = None
    err_parts = []
    if create_stats.get("errors"):
        err_parts.extend(create_stats["errors"][:3])
    if create_stats.get("skip_reasons") and create_stats.get("skipped"):
        # 仅当有失败时把跳过也写入 last_error 易混淆；跳过单独展示
        pass
    if err_parts:
        state["last_error"] = "; ".join(err_parts)
    state["active_window_id"] = window_id_for(now, config.start_time, config.end_time)
    state["session_window_id"] = state["active_window_id"]
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
                state = await _load_state(db)
                auto_n = await count_auto_strategies(db, config.account_id)
                resolved = resolve_session(
                    now,
                    config.start_time,
                    config.end_time,
                    session_window_id=state.get("session_window_id"),
                    has_auto_strategies=auto_n > 0,
                    grace_minutes=3,
                )
                cal_in = bool(resolved["calendar_in_window"])
                session_active = bool(resolved["session_active"])
                wid = resolved["window_id"]

                if not cal_in:
                    # 窗口结束：清理并清空会话
                    cleaned = state.get("cleaned_for_window")
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
                        if not stats.get("failed"):
                            state["cleaned_for_window"] = target_clean_id
                            state["current_symbols"] = []
                            state["session_window_id"] = None
                            state["last_error"] = None
                        else:
                            state["last_error"] = (
                                f"清理失败 {stats['failed']}: "
                                f"{';'.join(stats.get('errors') or [])}"
                            )
                        await _save_state(db, state)
                    elif state.get("session_window_id"):
                        state["session_window_id"] = None
                        await _save_state(db, state)
                    return

                # 日历在窗口内，但开盘已过且尚未进场 → 等待次日开盘
                if not session_active:
                    if state.get("waiting_note") != resolved.get("next_session_at"):
                        state["waiting_note"] = resolved.get("next_session_at")
                        logger.info(
                            "decline_rank waiting next start account=%s next=%s (start already passed today)",
                            config.account_id,
                            resolved.get("next_session_at"),
                        )
                        await _save_state(db, state)
                    return

                # 正式会话中
                if resolved.get("enter_session") and state.get("session_window_id") != wid:
                    state["session_window_id"] = wid
                    state["waiting_note"] = None
                    await _save_state(db, state)
                    state = await _load_state(db)

                if not await _master_switch_on(db):
                    return

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
            state["session_window_id"] = None
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
        else:
            state = await _load_state(db)
            state["session_window_id"] = None
            await _save_state(db, state)
        return {"enabled": False, "cleanup": cleanup, **cleanup_stats}


# singleton-style export for scheduler
async def decline_rank_auto_tick() -> None:
    await tick()
