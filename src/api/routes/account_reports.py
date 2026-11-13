"""Non-secret account reports. Agents may submit only for a profile they hold."""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ...hub import army_events
from ...hub.store import KEY_PREFIX
from ...services.profile.accounts import AccountsLocked
from ...services.profile.reports import ReportConflict, ReportInput, submit_report
from ..auth import _presented_token, can_see, hub_store
from ..dependencies import get_profile_manager
from ..helpers import require_profile
from .accounts import _locked, _no_personal_key_but_army

router = APIRouter(tags=["account-reports"])


class ReportReceipt(BaseModel):
    id: str


class ReportResponse(BaseModel):
    id: str
    platform: str
    account_id: str | None
    username: str
    status: str
    note: str
    reported_by: str
    reported_at: float
    reviewed_by: str
    reviewed_at: float | None


class ListedReport(ReportResponse):
    profile: str


def _actor(request: Request) -> str:
    return getattr(request.state, "user", {}).get("name", "")


@router.get(
    "/account-reports",
    response_model=list[ListedReport],
    # Army's panel shows and reviews them in its users' name; other personal keys cannot.
    dependencies=[Depends(_no_personal_key_but_army)],
)
def list_reports(
    request: Request, pending: bool = False, pm=Depends(get_profile_manager)
) -> list[dict]:
    reports = [
        {**report, "profile": p.name}
        for p in pm.list_profiles()
        if can_see(request, p.name)
        for report in pm.reports.list(p.uid)
        if not pending or report["reviewed_at"] is None
    ]
    return sorted(reports, key=lambda r: r["reported_at"], reverse=True)


@router.post("/profiles/{name}/account-reports", response_model=ReportReceipt)
def create_report(
    name: str, body: ReportInput, request: Request, pm=Depends(get_profile_manager)
) -> dict:
    require_profile(name, pm)
    store, token = hub_store(request), _presented_token(request)
    if token.startswith(KEY_PREFIX):
        if not store or store.key_kind(token) != "agent_key":
            raise HTTPException(403, "This key cannot submit account reports")
        if request.app.state.container.browser_launcher.holder(name) != _actor(request):
            raise HTTPException(403, "The agent does not hold this profile")
    try:
        receipt = submit_report(pm, name, body, _actor(request))
    except AccountsLocked as e:
        raise _locked() from e
    except ReportConflict as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except OSError as e:
        raise HTTPException(503, "Could not save the account report") from e
    army_events.notify("account_reports")
    return receipt


@router.post(
    "/profiles/{name}/account-reports/{report_id}/review",
    response_model=ReportResponse,
    dependencies=[Depends(_no_personal_key_but_army)],
)
def review_report(
    name: str, report_id: str, request: Request, pm=Depends(get_profile_manager)
) -> dict:
    profile = require_profile(name, pm)
    try:
        report = pm.reports.review(profile.uid, report_id, _actor(request))
    except ReportConflict as e:
        raise HTTPException(409, str(e)) from e
    except OSError as e:
        raise HTTPException(503, "Could not save the review") from e
    army_events.notify("account_reports")
    return report
