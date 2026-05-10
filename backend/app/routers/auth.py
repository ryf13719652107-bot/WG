from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from ..services import ui_auth

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    password: str


class AuthStatus(BaseModel):
    auth_required: bool
    authenticated: bool


@router.get("/status", response_model=AuthStatus)
async def auth_status(request: Request) -> AuthStatus:
    req = ui_auth.auth_enabled()
    tok = request.cookies.get(ui_auth.COOKIE_NAME)
    ok = not req or ui_auth.verify_token(tok)
    return AuthStatus(auth_required=req, authenticated=ok)


@router.post("/login")
async def auth_login(body: LoginBody, response: Response):
    if not ui_auth.auth_enabled():
        return {"ok": True, "auth_required": False}

    if not ui_auth.verify_password(body.password or ""):
        from fastapi import HTTPException

        raise HTTPException(status_code=401, detail="密码错误")

    token = ui_auth.issue_token()
    response.set_cookie(
        ui_auth.COOKIE_NAME,
        token,
        max_age=ui_auth.TTL_SEC,
        path="/",
        httponly=True,
        samesite="lax",
        secure=False,
    )
    return {"ok": True}


@router.post("/logout")
async def auth_logout(response: Response):
    response.delete_cookie(
        ui_auth.COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="lax",
        secure=False,
    )
    return {"ok": True}
