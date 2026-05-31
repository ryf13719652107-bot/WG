from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ..database import get_db
from ..models.account import Account
from ..schemas.account import AccountCreate, AccountEquityGuardUpdate, AccountResponse
from ..services.encryption import encrypt, decrypt, mask_key

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


def _account_to_response(account: Account, masked_key: str) -> AccountResponse:
    return AccountResponse(
        id=account.id,
        name=account.name,
        exchange=account.exchange or "binance",
        masked_key=masked_key,
        testnet=account.testnet,
        hedge_mode=account.hedge_mode,
        equity_stop_floor_u=float(getattr(account, "equity_stop_floor_u", 0) or 0),
        equity_baseline_u=float(account.equity_baseline_u) if account.equity_baseline_u else None,
        equity_baseline_at=account.equity_baseline_at,
        equity_stop_triggered=bool(getattr(account, "equity_stop_triggered", False)),
        created_at=account.created_at,
        updated_at=account.updated_at,
    )


@router.post("", response_model=AccountResponse)
async def create_account(data: AccountCreate, db: AsyncSession = Depends(get_db)):
    try:
        encrypted_key = encrypt(data.api_key)
        encrypted_secret = encrypt(data.api_secret)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"加密失败: {str(e)}")

    account = Account(
        name=data.name,
        exchange=data.exchange,
        api_key_encrypted=encrypted_key,
        api_secret_encrypted=encrypted_secret,
        testnet=data.testnet,
        hedge_mode=data.hedge_mode,
    )
    if data.okx_passphrase:
        account.okx_passphrase_encrypted = encrypt(data.okx_passphrase)
    db.add(account)
    await db.commit()
    await db.refresh(account)

    return _account_to_response(account, mask_key(data.api_key))


@router.get("", response_model=list[AccountResponse])
async def list_accounts(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Account))
    accounts = result.scalars().all()
    resp = []
    for a in accounts:
        try:
            key = decrypt(a.api_key_encrypted)
            mk = mask_key(key)
        except Exception:
            mk = "****"
        resp.append(_account_to_response(a, mk))
    return resp


@router.patch("/{account_id}/equity-guard", response_model=AccountResponse)
async def update_account_equity_guard(
    account_id: int,
    data: AccountEquityGuardUpdate,
    db: AsyncSession = Depends(get_db),
):
    account = await db.get(Account, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="账户不存在")
    account.equity_stop_floor_u = float(data.equity_stop_floor_u)
    if data.equity_stop_floor_u <= 0:
        account.equity_stop_triggered = False
    await db.commit()
    await db.refresh(account)
    try:
        mk = mask_key(decrypt(account.api_key_encrypted))
    except Exception:
        mk = "****"
    return _account_to_response(account, mk)


@router.post("/{account_id}/equity-guard/reset", response_model=AccountResponse)
async def reset_account_equity_guard(account_id: int, db: AsyncSession = Depends(get_db)):
    """清除触发标记与基准权益；下次启动策略时将重新记入初始总资金。"""
    account = await db.get(Account, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="账户不存在")
    account.equity_stop_triggered = False
    account.equity_baseline_u = None
    account.equity_baseline_at = None
    await db.commit()
    await db.refresh(account)
    try:
        mk = mask_key(decrypt(account.api_key_encrypted))
    except Exception:
        mk = "****"
    return _account_to_response(account, mk)


@router.delete("/{account_id}", status_code=204)
async def delete_account(account_id: int, db: AsyncSession = Depends(get_db)):
    account = await db.get(Account, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="账户不存在")

    # Block deletion if strategies exist for this account
    from ..models.strategy import Strategy
    result = await db.execute(select(Strategy).where(Strategy.account_id == account_id))
    existing = result.scalars().all()
    if existing:
        labels = ", ".join(f"{s.symbol}({s.direction})" for s in existing)
        raise HTTPException(status_code=400, detail=f"该账户下还有策略，请先删除策略：{labels}")

    await db.delete(account)
    await db.commit()
