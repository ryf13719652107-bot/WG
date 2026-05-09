"""Exchange factory: creates and caches exchange service instances per account."""
import time
import logging
from typing import Optional

from .exchange_base import BaseExchangeService
from .encryption import decrypt

logger = logging.getLogger(__name__)

_INSTANCE_TTL = 600  # 10 minutes before forcing recreation
_private_cache: dict[int, tuple[float, BaseExchangeService]] = {}
_public_cache: dict[str, tuple[float, BaseExchangeService]] = {}


async def create_exchange_service(account) -> BaseExchangeService:
    """Create an exchange service instance based on an Account model."""
    from .binance_service import BinanceService
    from .okx_service import OkxService

    api_key = decrypt(account.api_key_encrypted)
    api_secret = decrypt(account.api_secret_encrypted)
    exchange_type = getattr(account, 'exchange', 'binance') or 'binance'

    if exchange_type == "okx":
        svc = OkxService(api_key, api_secret, account.testnet, account.hedge_mode)
        passphrase = getattr(account, 'okx_passphrase_encrypted', None)
        if passphrase:
            svc.set_passphrase(decrypt(passphrase))
        return svc
    else:
        return BinanceService(api_key, api_secret, account.testnet, account.hedge_mode)


async def get_exchange_service(account_id: int) -> Optional[BaseExchangeService]:
    """Get cached exchange service for an account, creating if needed."""
    global _private_cache
    from ..database import async_session
    from ..models.account import Account
    from sqlalchemy import select

    now = time.time()
    if account_id in _private_cache:
        created, svc = _private_cache[account_id]
        if now - created < _INSTANCE_TTL:
            return svc
        try:
            await svc.close()
        except Exception:
            pass

    async with async_session() as session:
        account = await session.get(Account, account_id)
        if not account:
            return None
        svc = await create_exchange_service(account)
        _private_cache[account_id] = (now, svc)
        return svc


async def get_public_exchange(exchange_type: str = "binance") -> BaseExchangeService:
    """Get a public (no auth) exchange instance for market data."""
    global _public_cache
    from .binance_service import BinanceService

    now = time.time()
    if exchange_type in _public_cache:
        created, svc = _public_cache[exchange_type]
        if now - created < _INSTANCE_TTL:
            return svc
        try:
            await svc.close()
        except Exception:
            pass

    if exchange_type == "okx":
        from .okx_service import OkxService
        svc = OkxService(api_key="", secret="", testnet=False)
    else:
        svc = BinanceService(api_key="", secret="", testnet=False)

    _public_cache[exchange_type] = (now, svc)
    return svc


async def clear_all_cache():
    """Close and clear all cached exchange instances."""
    global _private_cache, _public_cache
    for _, svc in _private_cache.values():
        try:
            await svc.close()
        except Exception:
            pass
    for _, svc in _public_cache.values():
        try:
            await svc.close()
        except Exception:
            pass
    _private_cache.clear()
    _public_cache.clear()
