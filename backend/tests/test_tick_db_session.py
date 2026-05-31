"""TickDbSession / db_helpers 回归测试。"""
import pytest

from app.database import TICK_DB_CONCURRENCY, _TICK_DB_SEM, tick_db_session
from app.db_helpers import is_tick_session


@pytest.mark.asyncio
async def test_tick_session_commit_releases_semaphore():
    before = _TICK_DB_SEM._value  # noqa: SLF001
    tick = tick_db_session()
    await tick.commit()
    await tick.close()
    assert _TICK_DB_SEM._value == before  # noqa: SLF001


@pytest.mark.asyncio
async def test_tick_session_rollback_releases_semaphore():
    before = _TICK_DB_SEM._value  # noqa: SLF001
    tick = tick_db_session()
    await tick.rollback()
    await tick.close()
    assert _TICK_DB_SEM._value == before  # noqa: SLF001


def test_tick_db_concurrency_sane():
    assert TICK_DB_CONCURRENCY >= 8


def test_is_tick_session():
    assert is_tick_session(tick_db_session())
    assert not is_tick_session(object())
