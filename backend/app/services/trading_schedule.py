"""每日交易时段：收盘停止+市价全平，开盘自动恢复昨日被时段停止的策略。"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time as dt_time

from sqlalchemy import select

from ..config import now_beijing
from ..database import async_session
from ..models.bot_config import BotConfig
from ..models.strategy import Strategy

logger = logging.getLogger(__name__)

CFG_ENABLED = "trading_window_enabled"
CFG_START = "trading_window_start"
CFG_END = "trading_window_end"

_DEFAULT_START = "06:00"
_DEFAULT_END = "21:00"

_window_was_open: bool | None = None


@dataclass
class TradingWindowConfig:
    enabled: bool
    start_hm: str
    end_hm: str


def normalize_hm_str(s: str) -> str:
    """浏览器 time 输入可能是 HH:MM 或 HH:MM:SS，统一为 HH:MM。"""
    raw = (s or "").strip()
    if not raw:
        return ""
    parts = raw.split(":")
    if len(parts) < 2:
        return raw
    try:
        h, m = int(parts[0]), int(parts[1])
        if 0 <= h <= 23 and 0 <= m <= 59:
            return f"{h:02d}:{m:02d}"
    except (TypeError, ValueError):
        pass
    return raw


def _parse_hm(s: str) -> dt_time | None:
    norm = normalize_hm_str(s)
    if not norm:
        return None
    parts = norm.split(":")
    if len(parts) != 2:
        return None
    try:
        h, m = int(parts[0]), int(parts[1])
        if 0 <= h <= 23 and 0 <= m <= 59:
            return dt_time(h, m)
    except (TypeError, ValueError):
        pass
    return None


def is_within_trading_window(now: datetime | None = None, *, cfg: TradingWindowConfig | None = None) -> bool:
    """当前北京时间是否在 [start, end) 内。end 为收市时刻（该分钟起视为盘外）。"""
    if cfg is None:
        return True
    if not cfg.enabled:
        return True
    t_start = _parse_hm(cfg.start_hm)
    t_end = _parse_hm(cfg.end_hm)
    if t_start is None or t_end is None:
        return True
    if t_start == t_end:
        return False
    now = now or now_beijing()
    cur = dt_time(now.hour, now.minute)
    if t_start < t_end:
        return t_start <= cur < t_end
    # 跨午夜：如 22:00 - 06:00
    return cur >= t_start or cur < t_end


async def get_trading_window_config() -> TradingWindowConfig:
    async with async_session() as session:
        r = await session.execute(
            select(BotConfig).where(
                BotConfig.key.in_([CFG_ENABLED, CFG_START, CFG_END])
            )
        )
        rows = {c.key: c.value for c in r.scalars().all()}
    enabled = (rows.get(CFG_ENABLED) or "false").strip().lower() in ("1", "true", "yes")
    return TradingWindowConfig(
        enabled=enabled,
        start_hm=(rows.get(CFG_START) or _DEFAULT_START).strip() or _DEFAULT_START,
        end_hm=(rows.get(CFG_END) or _DEFAULT_END).strip() or _DEFAULT_END,
    )


async def save_trading_window_config(
    *,
    enabled: bool | None = None,
    start_hm: str | None = None,
    end_hm: str | None = None,
) -> TradingWindowConfig:
    async with async_session() as session:
        keys = {
            CFG_ENABLED: None,
            CFG_START: None,
            CFG_END: None,
        }
        r = await session.execute(select(BotConfig).where(BotConfig.key.in_(list(keys))))
        for row in r.scalars().all():
            keys[row.key] = row
        if enabled is not None:
            val = "true" if enabled else "false"
            if keys[CFG_ENABLED]:
                keys[CFG_ENABLED].value = val
            else:
                session.add(BotConfig(key=CFG_ENABLED, value=val))
        if start_hm is not None:
            s = normalize_hm_str(start_hm) or _DEFAULT_START
            if keys[CFG_START]:
                keys[CFG_START].value = s
            else:
                session.add(BotConfig(key=CFG_START, value=s))
        if end_hm is not None:
            e = normalize_hm_str(end_hm) or _DEFAULT_END
            if keys[CFG_END]:
                keys[CFG_END].value = e
            else:
                session.add(BotConfig(key=CFG_END, value=e))
        await session.commit()
    global _window_was_open
    _window_was_open = None
    return await get_trading_window_config()


async def trading_window_allows_start(
    strategy: Strategy | None = None,
    *,
    schedule_participate: bool | None = None,
) -> tuple[bool, str]:
    """仅对 schedule_participate=True 的策略限制盘外启动。"""
    participate = schedule_participate
    if participate is None and strategy is not None:
        participate = bool(getattr(strategy, "schedule_participate", False))
    if not participate:
        return True, ""
    cfg = await get_trading_window_config()
    if not cfg.enabled:
        return True, ""
    if is_within_trading_window(cfg=cfg):
        return True, ""
    return False, (
        f"当前不在允许交易时段（北京时间 {cfg.start_hm}–{cfg.end_hm}）。"
        f"请在时段内启动，或关闭仪表盘该策略的「时段运行」开关。"
    )


async def apply_schedule_participate(strategy_id: int, participate: bool) -> dict:
    """仪表盘开关：开=参与时段；关=正常连续运行（不受 06:00–21:00 约束）。"""
    from .scheduler import strategy_scheduler
    from .log_service import strategy_log_service

    cfg = await get_trading_window_config()
    in_window = is_within_trading_window(cfg=cfg) if cfg.enabled else True

    async with async_session() as session:
        s = await session.get(Strategy, strategy_id)
        if not s:
            return {"ok": False, "detail": "策略不存在"}
        s.schedule_participate = bool(participate)
        if not participate:
            s.stopped_by_schedule = False
        elif cfg.enabled and not in_window and s.status != "running":
            # 盘外打开时段开关且当前未运行：标记次日开盘自动恢复（手动 stop 会清此标志）
            s.stopped_by_schedule = True
        await session.commit()
        symbol = s.symbol
        direction = s.direction
        was_running = s.status == "running"

    if participate and cfg.enabled:
        if in_window and not was_running:
            ok = await strategy_scheduler.add_strategy(strategy_id)
            if ok:
                strategy_log_service.success(
                    strategy_id,
                    f"已启用时段运行（{cfg.start_hm}–{cfg.end_hm}），策略已启动",
                )
            else:
                strategy_log_service.warning(strategy_id, "已启用时段运行，但自动启动未完成")
        elif not in_window and was_running:
            await scheduled_stop_and_flatten(strategy_id)
            strategy_log_service.warning(
                strategy_id,
                f"已启用时段运行，当前盘外已收市停止",
            )
        elif not in_window and not was_running:
            strategy_log_service.info(
                strategy_id,
                f"已启用时段运行：将于次日 {cfg.start_hm} 自动启动，"
                f"每日 {cfg.start_hm}–{cfg.end_hm} 交易、{cfg.end_hm} 收市全平",
            )
        else:
            strategy_log_service.info(
                strategy_id,
                f"已启用时段运行：将在 {cfg.start_hm}–{cfg.end_hm} 内自动交易，"
                f"{cfg.end_hm} 收市全平",
            )
    elif participate:
        strategy_log_service.info(
            strategy_id,
            "已标记为时段运行；请在「系统设置」启用每日交易时段后按配置自动启停",
        )
    else:
        strategy_log_service.info(
            strategy_id,
            "已切换为正常连续运行（不受交易时段收市约束）",
        )

    async with async_session() as session:
        s = await session.get(Strategy, strategy_id)
        status = s.status if s else "stopped"

    return {
        "ok": True,
        "strategy_id": strategy_id,
        "schedule_participate": participate,
        "status": status,
        "symbol": symbol,
        "direction": direction,
    }


async def scheduled_stop_and_flatten(strategy_id: int) -> bool:
    """收市：仅 schedule_participate 策略；撤止盈/止损/加仓挂单并平仓。"""
    from .scheduler import strategy_scheduler
    from .log_service import strategy_log_service
    from ..routers.strategies import _flatten_strategy_orders_and_positions

    async with async_session() as session:
        s = await session.get(Strategy, strategy_id)
        if not s:
            return False
        if not bool(getattr(s, "schedule_participate", False)):
            logger.warning(
                "schedule_stop skipped strategy %d: schedule_participate is off",
                strategy_id,
            )
            return False
        if s.status != "running":
            logger.info("schedule_stop skipped strategy %d: status=%s", strategy_id, s.status)
            return False

    try:
        await strategy_scheduler.detach_strategy(strategy_id)
    except Exception as e:
        logger.error("schedule stop detach_strategy %d: %s", strategy_id, e)

    try:
        async with async_session() as session:
            s = await session.get(Strategy, strategy_id)
            if not s:
                return False
            close_ok, qty, exit_px = await _flatten_strategy_orders_and_positions(
                s,
                session,
                close_reason="schedule_stop",
                use_order_tracker=True,
            )
            s.status = "stopped"
            s.stopped_by_schedule = True
            await session.commit()
            cfg = await get_trading_window_config()
            detail = (
                f"交易时段收市（{cfg.end_hm}）：已撤销止盈/止损/加仓挂单并"
                f"{'市价平仓成功' if close_ok else '尝试市价平仓未完全确认'}"
                f" ({s.symbol} {s.direction}"
                + (f" 数量≈{qty:.4f}" if qty > 0 else "")
                + "），策略已停止，次日开盘将自动恢复"
            )
            if close_ok:
                strategy_log_service.warning(s.id, detail)
            else:
                strategy_log_service.error(s.id, detail)
            return close_ok
    except Exception as e:
        logger.error("schedule flatten strategy %d: %s", strategy_id, e)
        return False


async def _any_running_schedule_participant() -> bool:
    """盘外仍应为收市的：仅检查参与时段且仍在 running 的策略。"""
    async with async_session() as session:
        r = await session.execute(
            select(Strategy.id).where(
                Strategy.status == "running",
                Strategy.schedule_participate.is_(True),
            ).limit(1)
        )
        return r.first() is not None


async def on_trading_window_end(*, notify: bool = True) -> int:
    """收市：停止 schedule_participate=True 且 running 的策略并市价全平。"""
    global _window_was_open
    cfg = await get_trading_window_config()
    if not cfg.enabled:
        return 0

    async with async_session() as session:
        r = await session.execute(
            select(Strategy.id).where(
                Strategy.status == "running",
                Strategy.schedule_participate.is_(True),
            )
        )
        ids = [int(row[0]) for row in r.all()]

    if not ids:
        logger.info("Trading window end: no running schedule-participate strategies")
        _window_was_open = False
        return 0

    logger.warning(
        "Trading window end (%s): stopping %d strategies with market flatten",
        cfg.end_hm, len(ids),
    )
    ok_n = 0
    for sid in ids:
        if await scheduled_stop_and_flatten(sid):
            ok_n += 1

    if notify:
        try:
            from .feishu_notify import notify_trade_event
            await notify_trade_event(
                strategy_id=0,
                account_id=0,
                symbol="—",
                direction="—",
                title="交易时段收市",
                body_lines=[
                    f"北京时间已到 {cfg.end_hm}，已停止 {len(ids)} 个策略并尝试市价全平（成功确认 {ok_n} 个）。",
                    f"次日 {cfg.start_hm} 将自动恢复被时段停止的策略。",
                ],
            )
        except Exception as e:
            logger.debug("Feishu schedule end: %s", e)

    _window_was_open = False
    return len(ids)


async def on_trading_window_start() -> int:
    """开盘：恢复昨日被时段停止且仍开启 master 的策略。"""
    global _window_was_open
    cfg = await get_trading_window_config()
    if not cfg.enabled:
        return 0

    from sqlalchemy import select as sel
    from ..models.bot_config import BotConfig
    from .scheduler import strategy_scheduler
    from .log_service import strategy_log_service
    from .account_equity_guard import is_account_trading_halted

    async with async_session() as session:
        sw = await session.execute(sel(BotConfig).where(BotConfig.key == "master_switch"))
        master = sw.scalar()
        if master and master.value == "false":
            logger.info("Trading window start: master_switch off, skip auto start")
            _window_was_open = True
            return 0

        r = await session.execute(
            select(Strategy).where(
                Strategy.status == "stopped",
                Strategy.stopped_by_schedule.is_(True),
                Strategy.schedule_participate.is_(True),
            )
        )
        strategies = list(r.scalars().all())

    started = 0
    for s in strategies:
        sid = s.id
        try:
            async with async_session() as session:
                if await is_account_trading_halted(s.account_id, session):
                    strategy_log_service.warning(
                        sid,
                        f"交易时段开盘（{cfg.start_hm}）：账户总资产止损未重置，跳过自动启动",
                    )
                    continue
                st = await session.get(Strategy, sid)
                if not st or st.status != "stopped" or not st.stopped_by_schedule:
                    continue

            ok = await strategy_scheduler.add_strategy(sid)
            if ok:
                async with async_session() as session:
                    st = await session.get(Strategy, sid)
                    if st:
                        st.stopped_by_schedule = False
                        await session.commit()
                started += 1
                strategy_log_service.success(
                    sid,
                    f"交易时段开盘（{cfg.start_hm}）：策略已自动启动",
                )
            else:
                strategy_log_service.warning(
                    sid,
                    f"交易时段开盘（{cfg.start_hm}）：自动启动未完成，请手动检查（次日将重试）",
                )
        except Exception as e:
            logger.error("schedule auto start %d: %s", sid, e)

    if started > 0:
        logger.info("Trading window start (%s): auto-started %d strategies", cfg.start_hm, started)
    _window_was_open = True
    return started


async def trading_schedule_tick() -> None:
    """每分钟检查交易时段边界（北京时间）。"""
    global _window_was_open
    cfg = await get_trading_window_config()
    if not cfg.enabled:
        _window_was_open = None
        return

    open_now = is_within_trading_window(cfg=cfg)

    # 兜底：盘外仍有 running（边界漏触发或上次收市部分失败）每分钟重试，不重复飞书
    if not open_now and await _any_running_schedule_participant():
        await on_trading_window_end(notify=False)
        return

    prev = _window_was_open

    if prev is None:
        if not open_now:
            async with async_session() as session:
                r = await session.execute(
                    select(Strategy.id).where(
                        Strategy.status == "running",
                        Strategy.schedule_participate.is_(True),
                    )
                )
                if r.scalars().first() is not None:
                    logger.warning("Trading window: started outside hours, running end handler")
                    await on_trading_window_end(notify=False)
                    return
        else:
            await on_trading_window_start()
        _window_was_open = open_now
        return

    if prev and not open_now:
        await on_trading_window_end()
    elif not prev and open_now:
        await on_trading_window_start()
    else:
        _window_was_open = open_now
