"""
Website routes — look up, inspect, and delete websites.

A website is everything one ``(mode, name)`` — or, for prod, one domain —
has accumulated over all of its deployments. Deleting one removes its GCP
resources and all of its deployment records; see
``services.website_service`` for the state machine that makes that
retryable after a partial failure.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from api.dependencies import require_admin, require_approved
from config import Settings, get_settings
from db.database import get_db
from infra.layout import Placement, WebsiteRef, normalize_domain, public_url, website_key
from services import website_service as ws

logger = logging.getLogger("webdeploy.api.websites")

router = APIRouter(prefix="/api/websites", tags=["websites"])

# Websites per delete request. Each teardown drives several GCP operations
# that wait on GCP (tens of seconds for a legacy site), and a request must
# finish well inside Cloud Run's timeout — the UI sends larger selections in
# chunks of this size and shows progress between them.
MAX_WEBSITES_PER_REQUEST = 10

# Teardowns that run at once inside a request.
DELETE_CONCURRENCY = 5


def _visible(user, summary: dict) -> bool:
    if getattr(user, "role", None) == "admin":
        return True
    me = (getattr(user, "email", "") or "").casefold()
    return any(d.casefold() == me for d in summary.get("deployers", []))


@router.get("/lookup")
def lookup_website(
    mode: str = Query(...),
    website_name: str = Query(""),
    domain: Optional[str] = Query(None),
    user=Depends(require_approved),
    db=Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """What deploying (mode, name, domain) would do: update an existing
    website (and where it lives), or create a new one at which URL."""
    if mode not in ("demo", "prod", "cloudrun"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Unknown mode '{mode}'")
    if mode == "prod" and not normalize_domain(domain):
        return {"exists": False}
    key = website_key(mode, website_name.strip().lower(), domain, settings)
    summary = ws.describe(db, key, settings)
    if summary.get("exists"):
        if not _visible(user, summary):
            # Someone else's website: say it exists (the name is taken) but not what's in it.
            return {"key": key, "exists": True, "owned_by_other": True, "url": summary["url"],
                    "status": summary["status"]}
        return summary
    prospective = WebsiteRef(key, mode, website_name.strip().lower(), normalize_domain(domain), Placement.SITES)
    return {"key": key, "exists": False, "url": public_url(prospective, settings)}


@router.get("/sites-lb/status")
def sites_lb_status(user=Depends(require_admin), settings: Settings = Depends(get_settings)):
    """Routing capacity of the dedicated LB (its URL map has a hard 64 kB cap)."""
    from infra.sites_lb import SitesLB
    try:
        return SitesLB(settings).status()
    except Exception as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Could not read sites LB: {exc}")


@router.get("/{key}")
def get_website(
    key: str,
    user=Depends(require_approved),
    db=Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    try:
        summary = ws.describe(db, key, settings)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))
    if not summary.get("exists") or not _visible(user, summary):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Website '{key}' not found.")
    return summary


class WebsiteDeleteRequest(BaseModel):
    keys: list[str] = Field(..., min_length=1, max_length=MAX_WEBSITES_PER_REQUEST)


async def run_deletes(db, settings: Settings, keys: list[str], user) -> dict:
    for key in keys:
        try:
            ws.split_key(key)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))
    outcomes = await ws.delete_websites(db, settings, keys, user, concurrency=DELETE_CONCURRENCY)
    items = [o.as_dict() for o in outcomes]
    return {
        "results": items,
        "deleted_count": sum(1 for o in outcomes if o.status == "deleted"),
        "failed_count": sum(1 for o in outcomes if o.status == ws.DELETE_FAILED),
        "blocked_count": sum(1 for o in outcomes if o.status == "blocked"),
    }


@router.post("/bulk-delete")
async def bulk_delete_websites(
    body: WebsiteDeleteRequest,
    user=Depends(require_approved),
    db=Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Delete up to ``MAX_WEBSITES_PER_REQUEST`` websites.

    Each website gets its own outcome — ``deleted``, ``delete_failed``
    (resources partly removed; call again to resume) or ``blocked`` (busy,
    not yours, not found). One failure never stops the others.
    """
    return await run_deletes(db, settings, list(dict.fromkeys(body.keys)), user)


@router.delete("/{key}")
async def delete_website(
    key: str,
    user=Depends(require_approved),
    db=Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Delete one website. 200 when gone; 409 when busy; 403 when not
    yours; 502 when GCP cleanup failed part-way (retry the same call)."""
    result = await run_deletes(db, settings, [key], user)
    return raise_for_outcome(result["results"][0])


def raise_for_outcome(outcome: dict) -> dict:
    if outcome["status"] == "deleted":
        return outcome
    if outcome["status"] == "blocked":
        code = {
            "not_found": status.HTTP_404_NOT_FOUND,
            "forbidden": status.HTTP_403_FORBIDDEN,
            "conflict": status.HTTP_409_CONFLICT,
        }.get(outcome.get("code"), status.HTTP_500_INTERNAL_SERVER_ERROR)
        raise HTTPException(code, detail=outcome)
    raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=outcome)
