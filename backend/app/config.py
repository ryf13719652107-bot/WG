from pathlib import Path
from typing import Optional
from datetime import datetime, timezone, timedelta

from pydantic_settings import BaseSettings, SettingsConfigDict

BEIJING_TZ = timezone(timedelta(hours=8))

# 固定读 backend/.env，避免 uvicorn 工作目录不在 backend 时读不到 WEB_UI_PASSWORD
_BACKEND_DIR = Path(__file__).resolve().parent.parent


def now_beijing() -> datetime:
    """Return current Beijing time as naive datetime (for SQLite compatibility)."""
    return datetime.now(BEIJING_TZ).replace(tzinfo=None)


class Settings(BaseSettings):
    database_url: str = "sqlite+aiosqlite:///data/trading_bot.db"
    encryption_key: Optional[str] = None
    binance_api_key: Optional[str] = None
    binance_secret: Optional[str] = None
    binance_testnet: bool = True
    okx_api_key: str | None = None
    okx_secret: str | None = None
    okx_passphrase: str | None = None
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173,http://47.238.227.85:5173"
    http_proxy: str = ""

    # 飞书：https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot
    # 无需 .env：与 BINANCE_* 相同，进程环境变量 FEISHU_WEBHOOK_URL / FEISHU_KEYWORD_PREFIX 即生效；
    # 网页「系统设置」写入 bot_config 后优先于环境变量。
    # 机器人若启用「自定义关键词」，关键字前缀应与 feishu_keyword_prefix 一致（默认 [WG]）。
    feishu_webhook_url: Optional[str] = None
    feishu_keyword_prefix: str = "[WG]"

    # 网页登录：非空则所有 /api/*（除白名单）与 /ws/* 需携带登录 Cookie
    web_ui_password: Optional[str] = None

    # .env 与进程环境变量合并；系统/服务里的 Environment= 仍覆盖 .env。
    model_config = SettingsConfigDict(
        env_file=_BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
    )


settings = Settings()
