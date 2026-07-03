from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException

from app import state
from app.core.errors import MegaAPIException
from app.core.mega_api import MegaAPI
from app.storage.account_store import LockedError

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


@router.get("")
async def list_accounts():
    encrypted = await state.account_store.is_encrypted()
    if await state.account_store.is_locked():
        return {"locked": True, "encrypted": encrypted, "accounts": []}

    accounts = await state.account_store.list_mega_accounts()
    return {
        "locked": False,
        "encrypted": encrypted,
        "accounts": [{"email": email, "active": email in state.active_sessions} for email in accounts],
    }


@router.post("/check-2fa")
async def check_2fa(email: str = Form(...)):
    api = MegaAPI(api_key=state.mega_api_key, proxy_manager=state.active_proxy_manager())
    try:
        requires = await api.check_2fa(email)
    finally:
        await api.aclose()
    return {"requires_2fa": requires}


@router.post("/login")
async def login(email: str = Form(...), password: str = Form(...), pincode: str | None = Form(None)):
    if await state.account_store.is_locked():
        raise HTTPException(423, "Account store is locked -- unlock with the master password first")

    api = MegaAPI(api_key=state.mega_api_key, proxy_manager=state.active_proxy_manager())
    try:
        await api.login(email, password, pincode)
    except MegaAPIException as exc:
        await api.aclose()
        if exc.is_two_factor_required:
            raise HTTPException(400, "Two-factor code required") from exc
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        await api.aclose()
        raise HTTPException(400, str(exc)) from exc

    await state.account_store.persist_mega_login(api.email, password, api.password_aes, api.user_hash)
    state.active_sessions[api.email] = api

    quota = await api.get_quota()
    return {
        "email": api.email,
        "root_id": api.root_id,
        "quota": {"used": quota.used_storage, "max": quota.max_storage} if quota else None,
    }


@router.delete("/{email}")
async def delete_account(email: str):
    session = state.active_sessions.pop(email, None)
    if session is not None:
        await session.aclose()
    await state.account_store.delete_mega_account(email)
    return {"ok": True}


@router.post("/master-password/unlock")
async def unlock_master_password(password: str = Form(...)):
    ok = await state.account_store.unlock(password)
    if not ok:
        raise HTTPException(400, "Incorrect master password")
    return {"ok": True}


@router.post("/master-password")
async def set_master_password(password: str | None = Form(None)):
    try:
        await state.account_store.set_master_password(password or None)
    except LockedError as exc:
        raise HTTPException(423, "Unlock the account store before changing the master password") from exc
    return {"ok": True, "encrypted": password is not None}
