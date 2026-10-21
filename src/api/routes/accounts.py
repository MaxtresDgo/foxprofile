"""A profile's site accounts: username, password and 2FA secret, and filling a
site's login form with them in the running profile.

Listings never carry secrets; the password and the current 2FA code each have
their own route. Personal keys (fpk_, the MCP and agent keys) get 403 here:
neither MCP clients nor agents read accounts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ...core.logging import get_logger
from ...core.strings import get_string
from ...hub.store import KEY_PREFIX
from ...services.browser.login import login
from ...services.profile.accounts import PLATFORMS, AccountsLocked, totp, valid_totp_secret
from ..auth import _presented_token, can_see
from ..dependencies import get_browser_launcher, get_profile_manager
from ..helpers import require_profile
from ..schemas.common import SuccessResponse

if TYPE_CHECKING:
    from ...interfaces import IBrowserLauncher, IProfileManager

logger = get_logger("api.accounts")


def _no_personal_key(request: Request) -> None:
    if _presented_token(request).startswith(KEY_PREFIX):
        raise HTTPException(status_code=403, detail=get_string("err_personal_key_accounts"))


router = APIRouter(
    prefix="/profiles/{name}/accounts",
    tags=["accounts"],
    dependencies=[Depends(_no_personal_key)],
)


# The panel's profile list: every visible profile's accounts in one request.
summary_router = APIRouter(
    prefix="/accounts", tags=["accounts"], dependencies=[Depends(_no_personal_key)]
)


class AccountCreate(BaseModel):
    platform: str = Field(description=", ".join(PLATFORMS))
    username: str = Field(min_length=1, max_length=320)
    password: str = Field("", max_length=1000)
    totp_secret: str = Field("", max_length=200, description="Base32 2FA secret")


class AccountUpdate(BaseModel):
    """Only the given fields change; "" clears the password or the 2FA secret."""

    platform: str | None = None
    username: str | None = Field(None, min_length=1, max_length=320)
    password: str | None = Field(None, max_length=1000)
    totp_secret: str | None = Field(None, max_length=200)


class AccountResponse(BaseModel):
    id: str
    platform: str
    username: str
    has_password: bool
    has_totp: bool


class PasswordResponse(BaseModel):
    password: str


class TotpResponse(BaseModel):
    code: str
    remaining: int


def _check(fields: dict) -> dict:
    if "platform" in fields and fields["platform"] not in PLATFORMS:
        raise HTTPException(
            status_code=400, detail=get_string("err_platform", values=", ".join(PLATFORMS))
        )
    if "username" in fields:
        fields["username"] = fields["username"].strip()
        if not fields["username"]:
            raise HTTPException(status_code=400, detail=get_string("err_account_username"))
    if fields.get("totp_secret"):
        fields["totp_secret"] = "".join(fields["totp_secret"].split()).upper()
        if not valid_totp_secret(fields["totp_secret"]):
            raise HTTPException(status_code=400, detail=get_string("err_totp_secret"))
    return fields


def _locked() -> HTTPException:
    return HTTPException(status_code=503, detail=get_string("err_accounts_locked"))


def _account(pm: IProfileManager, name: str, account_id: str) -> dict:
    require_profile(name, pm)
    try:
        account = pm.accounts.get(name, account_id)
    except AccountsLocked as e:
        raise _locked() from e
    if account is None:
        raise HTTPException(status_code=404, detail=get_string("err_account_not_found"))
    return account


@summary_router.get("", response_model=dict[str, list[AccountResponse]])
def accounts_summary(
    request: Request, pm: IProfileManager = Depends(get_profile_manager)
) -> dict[str, list[dict]]:
    """{profile: its accounts} for the profiles this caller sees that have any; no secrets."""
    try:
        listed = {
            p.name: pm.accounts.list(p.name) for p in pm.list_profiles() if can_see(request, p.name)
        }
    except AccountsLocked as e:
        raise _locked() from e
    return {name: accounts for name, accounts in listed.items() if accounts}


@router.get("", response_model=list[AccountResponse])
def list_accounts(name: str, pm: IProfileManager = Depends(get_profile_manager)) -> list[dict]:
    require_profile(name, pm)
    try:
        return pm.accounts.list(name)
    except AccountsLocked as e:
        raise _locked() from e


@router.post("", response_model=AccountResponse, status_code=201)
def add_account(
    name: str, body: AccountCreate, pm: IProfileManager = Depends(get_profile_manager)
) -> dict:
    require_profile(name, pm)
    try:
        account = pm.accounts.add(name, _check(body.model_dump()))
    except AccountsLocked as e:
        raise _locked() from e
    logger.info("Added a %s account to %s", account["platform"], name)
    return account


@router.patch("/{account_id}", response_model=AccountResponse)
def update_account(
    name: str,
    account_id: str,
    body: AccountUpdate,
    pm: IProfileManager = Depends(get_profile_manager),
) -> dict:
    _account(pm, name, account_id)
    try:
        account = pm.accounts.update(name, account_id, _check(body.model_dump(exclude_unset=True)))
    except AccountsLocked as e:
        raise _locked() from e
    if account is None:
        raise HTTPException(status_code=404, detail=get_string("err_account_not_found"))
    return account


@router.delete("/{account_id}", response_model=SuccessResponse)
def delete_account(
    name: str, account_id: str, pm: IProfileManager = Depends(get_profile_manager)
) -> SuccessResponse:
    require_profile(name, pm)
    try:
        deleted = pm.accounts.delete(name, account_id)
    except AccountsLocked as e:
        raise _locked() from e
    if not deleted:
        raise HTTPException(status_code=404, detail=get_string("err_account_not_found"))
    return SuccessResponse(message="Account deleted")


@router.get("/{account_id}/password", response_model=PasswordResponse)
def reveal_password(
    name: str, account_id: str, pm: IProfileManager = Depends(get_profile_manager)
) -> PasswordResponse:
    return PasswordResponse(password=_account(pm, name, account_id)["password"])


@router.get("/{account_id}/totp", response_model=TotpResponse)
def current_code(
    name: str, account_id: str, pm: IProfileManager = Depends(get_profile_manager)
) -> TotpResponse:
    secret = _account(pm, name, account_id)["totp_secret"]
    if not secret:
        raise HTTPException(status_code=404, detail=get_string("err_account_no_totp"))
    code, remaining = totp(secret)
    return TotpResponse(code=code, remaining=remaining)


@router.post("/{account_id}/login", response_model=SuccessResponse)
def fill_login(
    name: str,
    account_id: str,
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
) -> SuccessResponse:
    """Open the site's login page in the running profile and fill it in."""
    account = _account(pm, name, account_id)
    if not bl.is_running(name):
        raise HTTPException(status_code=409, detail=get_string("err_not_running", name=name))
    result = login(bl, name, account)
    if not result["ok"]:
        raise HTTPException(status_code=409, detail=result["error"])
    done = "account_already_signed_in" if result.get("signed_in") else "account_login_done"
    return SuccessResponse(message=get_string(done))
