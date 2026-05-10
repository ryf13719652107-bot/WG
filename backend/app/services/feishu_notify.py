"""飞书 (Lark) 群机器人 Webhook — 异步推送交易事件，不阻塞策略主流程。"""
import asyncio
import json
import logging
from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import now_beijing, settings
from ..database import async_session
from ..models.account import Account
from ..models.bot_config import BotConfig

logger = logging.getLogger(__name__)

FEISHU_CFG_WEBHOOK = "feishu_webhook_url"
FEISHU_CFG_PREFIX = "feishu_keyword_prefix"


async def _bot_config_value(session: AsyncSession, key: str) -> Optional[str]:
    stmt = select(BotConfig.value).where(BotConfig.key == key)
    r = await session.execute(stmt)
    val = r.scalar_one_or_none()
    return val if val is None else str(val)


async def resolve_feishu_config(session: AsyncSession) -> tuple[str, str]:
    """返回 (webhook_url, keyword_prefix)。数据库非空 webhook 优先于环境变量。"""
    wh_db = await _bot_config_value(session, FEISHU_CFG_WEBHOOK)
    pr_db = await _bot_config_value(session, FEISHU_CFG_PREFIX)

    if wh_db is not None and wh_db.strip():
        url = wh_db.strip()
    else:
        url = (getattr(settings, "feishu_webhook_url", None) or "").strip()

    if pr_db is not None:
        prefix = pr_db
    else:
        prefix = (getattr(settings, "feishu_keyword_prefix", None) or "").strip()

    return url, prefix


def mask_webhook_url(url: str) -> str:
    u = (url or "").strip()
    if not u:
        return ""
    if len(u) <= 20:
        return "(已配置) ***" + u[-min(4, len(u)) :]
    return u[:16] + "…" + u[-8:]


async def notify_trade_event(
    strategy_id: int,
    account_id: int,
    symbol: str,
    direction: str,
    title: str,
    body_lines: Optional[list[str]] = None,
) -> None:
    """写入飞书自定义机器人（msg_type=text）。在未配置 webhook 时为 no-op."""
    body_lines = body_lines or []
    ts = now_beijing().strftime("%Y-%m-%d %H:%M:%S")

    acc_name = f"id={account_id}"
    exch = "?"
    url = ""
    prefix = ""
    try:
        async with async_session() as session:
            url, prefix = await resolve_feishu_config(session)
            acc = await session.get(Account, account_id)
            if acc:
                acc_name = acc.name or acc_name
                exch = acc.exchange or exch
    except Exception as e:
        logger.debug("Feishu: load session failed: %s", e)
        url = (getattr(settings, "feishu_webhook_url", None) or "").strip()
        prefix = (getattr(settings, "feishu_keyword_prefix", None) or "").strip()

    if not url:
        return
    lines = [
        f"时间: {ts}",
        title,
        f"账户: {acc_name}（{exch}）",
        f"策略: #{strategy_id}  {symbol}  {direction}",
        *body_lines,
    ]
    text = "\n".join(lines)
    pref = (prefix or "").strip()
    if pref and pref not in text:
        text = f"{pref}\n{text}"

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                url,
                json={
                    "msg_type": "text",
                    "content": {"text": text[:19000]},
                },
            )
        body = ""
        try:
            body = (r.text or "")[:400]
            data = json.loads(r.text or "{}") if r.text else {}
        except json.JSONDecodeError:
            data = {}

        ok = r.status_code < 400
        code = None
        if isinstance(data, dict):
            code = data.get("code")
            ok = ok and code in (None, 0)

        if not ok:
            logger.warning("Feishu webhook HTTP=%s body=%s", r.status_code, body)
    except Exception as e:
        logger.warning("Feishu webhook error: %s", e)


def schedule_trade_notify(
    *,
    strategy_id: int,
    account_id: int,
    symbol: str,
    direction: str,
    title: str,
    body_lines: Optional[list[str]] = None,
) -> None:
    """fire-and-forget，不阻塞策略 tick。实际是否发送在 notify 内查库+环境变量后决定。"""

    async def _run():
        try:
            await notify_trade_event(
                strategy_id, account_id,
                symbol or "-", direction or "-",
                title, body_lines,
            )
        except Exception as e:
            logger.warning("Feishu schedule task failed: %s", e)

    try:
        asyncio.create_task(_run())
    except RuntimeError:
        logger.debug("Feishu: no event loop — skip")

