"""
Domain API routes — check availability and register domains via Cloud Domains.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from config import Settings, get_settings
from services.domain_service import DomainService

logger = logging.getLogger("webdeploy.api.domains")

router = APIRouter(prefix="/api/domains", tags=["domains"])


# ── Response schemas ─────────────────────────────────────────────────

class DomainCheckResponse(BaseModel):
    status: str  # "owned" | "available" | "unavailable"
    price_amount: float | None = None
    price_currency: str | None = None
    message: str = ""


class DomainRegisterRequest(BaseModel):
    domain: str


class DomainRegisterResponse(BaseModel):
    success: bool
    message: str = ""


# ═══════════════════════════════════════════════════════════════════════
#  GET /api/domains/check?domain=example.com
# ═══════════════════════════════════════════════════════════════════════

@router.get("/check", response_model=DomainCheckResponse)
def check_domain(
    domain: str = Query(..., description="Domain name to check"),
    settings: Settings = Depends(get_settings),
) -> DomainCheckResponse:
    """
    Check if a domain is already owned in the GCP project, available
    for purchase, or unavailable.

    When PROD_AUTO_REGISTER_DOMAINS is disabled, always returns "owned"
    so deployment is not blocked.
    """
    if not settings.PROD_AUTO_REGISTER_DOMAINS:
        return DomainCheckResponse(
            status="owned",
            message="Vérification de domaine désactivée (PROD_AUTO_REGISTER_DOMAINS=false).",
        )

    if not domain or not domain.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Le nom de domaine est requis.",
        )

    service = DomainService(settings)
    result = service.check_domain(domain.strip().lower())

    return DomainCheckResponse(
        status=result.status,
        price_amount=result.price_amount,
        price_currency=result.price_currency,
        message=result.message,
    )


# ═══════════════════════════════════════════════════════════════════════
#  POST /api/domains/register
# ═══════════════════════════════════════════════════════════════════════

@router.post("/register", response_model=DomainRegisterResponse)
def register_domain(
    body: DomainRegisterRequest,
    settings: Settings = Depends(get_settings),
) -> DomainRegisterResponse:
    """
    Purchase a domain via Cloud Domains API.
    Requires PROD_AUTO_REGISTER_DOMAINS=true.
    """
    if not settings.PROD_AUTO_REGISTER_DOMAINS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="L'achat automatique de domaines est désactivé.",
        )

    domain = body.domain.strip().lower()
    if not domain:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Le nom de domaine est requis.",
        )

    service = DomainService(settings)
    result = service.register_domain(domain)

    if not result.success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=result.message,
        )

    return DomainRegisterResponse(
        success=result.success,
        message=result.message,
    )
