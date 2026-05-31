"""策略 tick 与 ORM 会话的辅助方法（兼容 TickDbSession 与普通 AsyncSession）。"""
from __future__ import annotations

from typing import Any, Optional

from .database import TickDbSession


def is_tick_session(session: Any) -> bool:
    return isinstance(session, TickDbSession)


async def db_add(session: Any, obj: Any) -> None:
    if is_tick_session(session):
        await session.add(obj)
    else:
        session.add(obj)


async def db_refresh(session: Any, obj: Any) -> Any:
    """刷新 ORM 状态；TickDbSession 下返回 merge 后的实例（请用返回值覆盖原引用）。"""
    if is_tick_session(session):
        return await session.refresh(obj)
    await session.refresh(obj)
    return obj


async def db_commit(
    session: Any,
    *,
    strategy: Any = None,
    positions: Optional[list] = None,
) -> Any:
    """提交事务。TickDbSession 下会 reattach 后提交；返回 merge 后的 strategy（若有）。"""
    if is_tick_session(session):
        merged = strategy
        if strategy is not None or positions:
            merged = await session.reattach(strategy, positions)
        await session.commit()
        return merged
    await session.commit()
    return strategy


async def db_rollback(session: Any) -> None:
    if is_tick_session(session):
        await session.rollback()
    else:
        await session.rollback()
