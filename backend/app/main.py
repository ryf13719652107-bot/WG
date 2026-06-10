import logging
import os
from logging.handlers import RotatingFileHandler
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from .config import settings
from .database import init_db, get_db, async_session
from .models.bot_config import BotConfig
from .models.strategy import Strategy
from .routers import account, strategies, positions, trades, dashboard, websocket, auth
from .services.scheduler import strategy_scheduler
from .services.exchange_factory import get_public_exchange
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

# ---- Logging Setup ----
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

# File handler with rotation (10MB x 5 files)
file_handler = RotatingFileHandler(
    os.path.join(LOG_DIR, "bot.log"),
    maxBytes=10 * 1024 * 1024,
    backupCount=5,
    encoding="utf-8",
)
file_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))

# Console handler
console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
))

# Root logger
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.addHandler(file_handler)
root_logger.addHandler(console_handler)

# Quiet noisy libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=" * 50)
    logger.info("Smart Hedge Martin starting...")
    logger.info("Step 1/5: init_db...")
    await init_db()
    from .services import ui_auth as _ui_hydrate

    async with async_session() as session:
        cfg_row = await session.execute(
            select(BotConfig).where(BotConfig.key == _ui_hydrate.BOT_CFG_WEB_UI_PASSWORD_KEY)
        )
        cfg = cfg_row.scalar_one_or_none()
        _ui_hydrate.set_db_password_overlay(cfg.value if cfg else None)
    logger.info("Step 2/5: scheduler.start...")
    strategy_scheduler.start()
    logger.info("Step 3/5: resume_running_strategies...")
    await strategy_scheduler.resume_running_strategies()
    logger.info("Step 4/5: start log persistence...")
    from .services.log_service import strategy_log_service
    await strategy_log_service.start_persistence()

    logger.info("Step 5/5: Backend ready")
    from .services import ui_auth as _ui_auth_startup

    env_on = _ui_auth_startup.env_or_envfile_password_configured()
    db_on = _ui_auth_startup.database_password_configured()
    if _ui_auth_startup.auth_enabled():
        logger.info(
            "Web UI 登录门禁: 已启用（进程或 .env 的 WEB_UI_PASSWORD=%s，数据库 web_ui_password=%s）",
            "有" if env_on else "无",
            "有" if db_on else "无",
        )
    else:
        logger.info(
            "Web UI 登录门禁: 未启用 — 请设置进程环境变量或 backend/.env 中的 WEB_UI_PASSWORD，或在「系统设置」保存 Web 控制台密码（写入数据库），并重启或刷新页面"
        )
    yield
    logger.info("Shutting down...")
    strategy_scheduler.stop()
    await strategy_log_service.stop_persistence()
    try:
        from .services.price_stream import price_stream
        await price_stream.shutdown()
    except Exception as e:
        logger.warning("price_stream shutdown error: %s", e)
    try:
        from .services.exchange_factory import clear_all_cache
        await clear_all_cache()
    except Exception as e:
        logger.warning("exchange cache clear error: %s", e)
    logger.info("Backend stopped")


app = FastAPI(
    title="马丁网格交易",
    description="Grid Martingale Trading System",
    version="0.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def web_ui_auth_middleware(request: Request, call_next):
    """WEB_UI_PASSWORD 非空时，保护 /api/*（白名单除外）及部分文档路由。"""
    from .services import ui_auth as _ua

    if not _ua.auth_enabled():
        return await call_next(request)
    if request.method == "OPTIONS":
        return await call_next(request)
    path = request.url.path
    if path.startswith("/api/"):
        if path.startswith("/api/auth/") or path == "/api/health":
            return await call_next(request)
        tok = request.cookies.get(_ua.COOKIE_NAME)
        if not _ua.verify_token(tok):
            return JSONResponse({"detail": "需要登录"}, status_code=401)
        return await call_next(request)
    if path.startswith("/docs") or path.startswith("/redoc") or path == "/openapi.json":
        tok = request.cookies.get(_ua.COOKIE_NAME)
        if not _ua.verify_token(tok):
            return JSONResponse({"detail": "需要登录"}, status_code=401)
    return await call_next(request)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    import logging
    logger = logging.getLogger(__name__)
    logger.error("Unhandled exception on %s %s: %s", request.method, request.url.path, exc, exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "服务器内部错误"},
    )


# Register routers — must be before SPA catch-all
app.include_router(auth.router)
app.include_router(account.router)
app.include_router(strategies.router)
app.include_router(positions.router)
app.include_router(trades.router)
app.include_router(dashboard.router)
app.include_router(websocket.router)


@app.get("/api/health")
async def health_check():
    return {"status": "ok", "version": "0.2.0"}


_MARKETS_CACHE: dict[str, tuple[float, list[str]]] = {}
_MARKETS_CACHE_TTL = 300  # seconds


@app.get("/api/markets")
async def get_markets(exchange: str = "binance", account_id: int | None = None):
    """Return available USDT perpetual symbols for the given exchange."""
    import time
    from .services.exchange_factory import get_exchange_service, get_public_exchange
    from .services.exchange_base import BaseExchangeService

    log = logging.getLogger(__name__)
    cache_key = f"{exchange}:{account_id or 'public'}"
    now = time.time()
    cached = _MARKETS_CACHE.get(cache_key)
    if cached and now - cached[0] < _MARKETS_CACHE_TTL:
        return {"symbols": cached[1]}

    errors: list[str] = []
    sources: list[tuple[str, object]] = []
    if account_id:
        sources.append(("account", lambda aid=account_id: get_exchange_service(aid)))
    sources.append(("public", lambda ex=exchange: get_public_exchange(ex)))

    for label, fetcher in sources:
        try:
            ex = await fetcher()
            if not ex:
                continue
            raw = await ex.fetch_markets()
            symbols = BaseExchangeService.extract_usdt_perp_symbols(raw)
            if symbols:
                _MARKETS_CACHE[cache_key] = (now, symbols)
                return {"symbols": symbols}
            errors.append(f"{label}: empty market list")
        except Exception as e:
            errors.append(f"{label}: {e}")

    log.warning("Failed to fetch markets (%s): %s", cache_key, "; ".join(errors) or "unknown")
    popular = [
        "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
        "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT",
        "UNIUSDT", "ATOMUSDT", "LTCUSDT", "ETCUSDT",
        "OPUSDT", "ARBUSDT", "FILUSDT", "APTUSDT", "NEARUSDT",
        "SUIUSDT", "PEPEUSDT", "WIFUSDT", "SEIUSDT", "GPSUSDT",
        "1INCHUSDT", "AAVEUSDT", "ALGOUSDT", "APEUSDT", "ARUSDT",
    ]
    return {"symbols": popular}


@app.get("/api/markets/strategy-counts")
async def get_strategy_counts(account_id: int | None = None, db=None):
    """Return per-symbol strategy counts for the 2-per-symbol constraint."""
    from sqlalchemy import select, func
    from .database import async_session
    from .models.strategy import Strategy
    async with async_session() as session:
        stmt = select(Strategy.symbol, func.count(Strategy.id)).group_by(Strategy.symbol)
        if account_id:
            stmt = stmt.where(Strategy.account_id == account_id)
        result = await session.execute(stmt)
        counts = {row[0]: row[1] for row in result.all()}
        # Also include per-direction to show which direction already exists
        dir_stmt = select(Strategy.symbol, Strategy.direction).where(Strategy.symbol.is_not(None))
        if account_id:
            dir_stmt = dir_stmt.where(Strategy.account_id == account_id)
        dir_result = await session.execute(dir_stmt)
        directions: dict[str, list[str]] = {}
        for row in dir_result.all():
            sym = row[0]
            if sym not in directions:
                directions[sym] = []
            directions[sym].append(row[1])
        return {"counts": counts, "directions": directions}


class ToggleRequest(BaseModel):
    enabled: bool


@app.get("/api/logs")
async def view_logs(lines: int = Query(default=100, le=1000)):
    """Return the last N lines of the log file."""
    log_file = os.path.join(LOG_DIR, "bot.log")
    if not os.path.exists(log_file):
        return {"lines": [], "message": "日志文件不存在"}
    try:
        with open(log_file, "r", encoding="utf-8") as f:
            all_lines = f.readlines()
            recent = all_lines[-lines:] if len(all_lines) > lines else all_lines
            return {"lines": [l.rstrip() for l in recent], "total": len(all_lines)}
    except Exception as e:
        return {"lines": [], "message": str(e)}


@app.post("/api/bot/toggle")
async def toggle_bot(body: ToggleRequest, db: AsyncSession = Depends(get_db)):
    from sqlalchemy import select
    enabled = body.enabled
    result = await db.execute(select(BotConfig).where(BotConfig.key == "master_switch"))
    config = result.scalar()
    if config:
        config.value = "true" if enabled else "false"
    else:
        config = BotConfig(key="master_switch", value="true" if enabled else "false")
        db.add(config)
    await db.commit()
    return {"master_switch": enabled}


class WebUiPasswordStatus(BaseModel):
    """供系统设置页判断门禁来源（不返回明文）。"""
    auth_required_effective: bool = Field(description="与 /api/auth/status 的 auth_required 一致")
    environment_has_password: bool
    database_has_password: bool


class WebUiPasswordUpdate(BaseModel):
    password: str = Field(default="", description="新密码；留空仅清除数据库中的 web_ui_password")


@app.get("/api/bot/web-ui-password", response_model=WebUiPasswordStatus)
async def get_web_ui_password_status(db: AsyncSession = Depends(get_db)):
    from .services import ui_auth as ua

    row = await db.execute(select(BotConfig).where(BotConfig.key == ua.BOT_CFG_WEB_UI_PASSWORD_KEY))
    cfg = row.scalar_one_or_none()
    db_has = bool(cfg and cfg.value.strip())
    env_has = ua.env_or_envfile_password_configured()
    return WebUiPasswordStatus(
        auth_required_effective=ua.auth_enabled(),
        environment_has_password=env_has,
        database_has_password=db_has,
    )


@app.put("/api/bot/web-ui-password", response_model=WebUiPasswordStatus)
async def put_web_ui_password(body: WebUiPasswordUpdate, db: AsyncSession = Depends(get_db)):
    from .services import ui_auth as ua

    raw = body.password.strip() if body.password else ""

    if not raw:
        del_row = await db.execute(select(BotConfig).where(BotConfig.key == ua.BOT_CFG_WEB_UI_PASSWORD_KEY))
        existing = del_row.scalar_one_or_none()
        if existing:
            await db.delete(existing)
        ua.set_db_password_overlay(None)
    else:
        r = await db.execute(select(BotConfig).where(BotConfig.key == ua.BOT_CFG_WEB_UI_PASSWORD_KEY))
        row = r.scalar_one_or_none()
        if row:
            row.value = raw
        else:
            db.add(BotConfig(key=ua.BOT_CFG_WEB_UI_PASSWORD_KEY, value=raw))
        ua.set_db_password_overlay(raw)

    await db.commit()

    row2 = await db.execute(select(BotConfig).where(BotConfig.key == ua.BOT_CFG_WEB_UI_PASSWORD_KEY))
    cfg = row2.scalar_one_or_none()
    db_has = bool(cfg and cfg.value.strip())
    env_has = ua.env_or_envfile_password_configured()
    return WebUiPasswordStatus(
        auth_required_effective=ua.auth_enabled(),
        environment_has_password=env_has,
        database_has_password=db_has,
    )


# ---- SPA fallback: must be LAST after all API routes ----
frontend_dist = os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "dist")
frontend_dist = os.path.abspath(frontend_dist)
_no_cache_html = {
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "Pragma": "no-cache",
}
if os.path.isdir(frontend_dist):
    logger.info("Frontend dist path: %s", frontend_dist)
    app.mount("/assets", StaticFiles(directory=os.path.join(frontend_dist, "assets")), name="static")

    @app.get("/{full_path:path}")
    async def serve_frontend(full_path: str):
        """SPA fallback — serve index.html for non-API routes."""
        file_path = os.path.join(frontend_dist, full_path) if full_path else ""
        if full_path and os.path.isfile(file_path):
            return FileResponse(file_path)
        index_html = os.path.join(frontend_dist, "index.html")
        return FileResponse(index_html, headers=_no_cache_html)
else:
    logger.warning("Frontend dist missing at %s — run: cd frontend && npm run build", frontend_dist)
