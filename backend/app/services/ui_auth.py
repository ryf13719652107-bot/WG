"""前端 Web UI 登录 Cookie（HMAC 签名，无第三方 JWT 依赖）。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

from ..config import settings

COOKIE_NAME = "wg_ui_token"
TTL_SEC = 7 * 24 * 3600

# bot_config.key：无环境变量时仍可启用门禁（系统设置页写入）
BOT_CFG_WEB_UI_PASSWORD_KEY = "web_ui_password"

_db_password_overlay: str | None = None


def set_db_password_overlay(raw: str | None) -> None:
    """由启动与 /api/bot/web-ui-password 更新；非空则参与门禁（优先级低于进程环境变量）。"""
    global _db_password_overlay
    s = (raw or "").strip()
    _db_password_overlay = s or None


def database_password_configured() -> bool:
    return bool(_db_password_overlay)


def _effective_web_ui_password() -> str:
    """优先级：进程环境变量 WEB_UI_PASSWORD > 数据库 bot_config > pydantic（.env 已合并进 settings）。"""
    v = (os.environ.get("WEB_UI_PASSWORD") or "").strip()
    if v:
        return v
    if _db_password_overlay:
        return _db_password_overlay
    return (getattr(settings, "web_ui_password", None) or "").strip()


def auth_enabled() -> bool:
    return bool(_effective_web_ui_password())


def _signing_key() -> bytes:
    base = (
        (settings.encryption_key or "")
        + "|"
        + _effective_web_ui_password()
        + "|wg-ui-session-v1"
    )
    return hashlib.sha256(base.encode("utf-8")).digest()


def issue_token() -> str:
    body = {"exp": int(time.time()) + TTL_SEC}
    pt = json.dumps(body, separators=(",", ":")).encode("utf-8")
    b64 = base64.urlsafe_b64encode(pt).decode().rstrip("=")
    sig = hmac.new(_signing_key(), b64.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{b64}.{sig}"


def verify_token(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    b64, _, sig = token.partition(".")
    if not sig:
        return False
    expect = hmac.new(_signing_key(), b64.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expect, sig):
        return False
    try:
        pad = "=" * (-len(b64) % 4)
        data = json.loads(base64.urlsafe_b64decode((b64 + pad).encode("ascii")).decode("utf-8"))
    except Exception:
        return False
    return int(data.get("exp", 0)) >= int(time.time())


def verify_password(candidate: str) -> bool:
    expected = _effective_web_ui_password()
    if not expected:
        return False
    a = hashlib.sha256(candidate.encode("utf-8")).digest()
    b = hashlib.sha256(expected.encode("utf-8")).digest()
    return hmac.compare_digest(a, b)
