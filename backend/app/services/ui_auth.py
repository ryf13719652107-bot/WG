"""前端 Web UI 登录 Cookie（HMAC 签名，无第三方 JWT 依赖）。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from ..config import settings

COOKIE_NAME = "wg_ui_token"
TTL_SEC = 7 * 24 * 3600


def auth_enabled() -> bool:
    return bool((getattr(settings, "web_ui_password", None) or "").strip())


def _signing_key() -> bytes:
    base = (
        (settings.encryption_key or "")
        + "|"
        + (settings.web_ui_password or "").strip()
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
    expected = (settings.web_ui_password or "").strip()
    if not expected:
        return False
    a = hashlib.sha256(candidate.encode("utf-8")).digest()
    b = hashlib.sha256(expected.encode("utf-8")).digest()
    return hmac.compare_digest(a, b)
