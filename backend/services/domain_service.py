"""
Domain service — check ownership and purchase domains via Google Cloud Domains API.

Checks domain ownership in three ways (in order):
1. Cloud Domains registrations (domains bought via Google Cloud)
2. Cloud DNS managed zones (domains managed in the GCP project, even if bought elsewhere)
3. Cloud Domains searchDomains (availability + pricing for purchase)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from google.api_core import exceptions as gcp_exceptions
from google.cloud import domains_v1
from googleapiclient.discovery import build as api_build

from config import Settings
from infra.gcp_helpers import get_credentials

logger = logging.getLogger("webdeploy.domain_service")


@dataclass
class DomainCheckResult:
    """Result of a domain ownership/availability check."""
    status: str  # "owned" | "available" | "unavailable"
    price_amount: Optional[float] = None
    price_currency: Optional[str] = None
    message: str = ""


@dataclass
class DomainRegisterResult:
    """Result of a domain registration attempt."""
    success: bool
    message: str = ""


class DomainService:
    """Check and register domains via Google Cloud Domains + Cloud DNS."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._project = settings.PROJECT_ID
        self._location = settings.DOMAINS_LOCATION

    @property
    def _parent(self) -> str:
        return f"projects/{self._project}/locations/{self._location}"

    def _get_domains_client(self) -> domains_v1.DomainsClient:
        credentials = get_credentials(self._settings.GOOGLE_APPLICATION_CREDENTIALS)
        return domains_v1.DomainsClient(credentials=credentials)

    def _get_dns_service(self):
        credentials = get_credentials(self._settings.GOOGLE_APPLICATION_CREDENTIALS)
        return api_build("dns", "v1", credentials=credentials, cache_discovery=False)

    # ── Check domain ─────────────────────────────────────────────────

    def _check_cloud_domains(self, domain: str) -> Optional[DomainCheckResult]:
        """Check if the domain is registered via Cloud Domains."""
        try:
            client = self._get_domains_client()
            registration_name = f"{self._parent}/registrations/{domain}"
            client.get_registration(name=registration_name)
            logger.info("Domain %s found in Cloud Domains registrations", domain)
            return DomainCheckResult(
                status="owned",
                message=f"Domaine {domain} enregistré via Google Cloud Domains.",
            )
        except gcp_exceptions.NotFound:
            return None
        except Exception as exc:
            logger.warning("Error checking Cloud Domains for %s: %s", domain, exc)
            return None

    @staticmethod
    def _is_subdomain(domain: str) -> bool:
        """Return True if *domain* has 3+ dot-separated parts (e.g. blog.example.com)."""
        return len(domain.strip(".").split(".")) > 2

    @staticmethod
    def _get_parent_domain(domain: str) -> str:
        """Extract parent domain: ``blog.example.com`` -> ``example.com``."""
        parts = domain.strip(".").split(".")
        return ".".join(parts[-2:]) if len(parts) > 2 else domain

    def _check_cloud_dns(self, domain: str) -> Optional[DomainCheckResult]:
        """Check if a Cloud DNS managed zone exists for this domain or its parent.

        For subdomains (e.g. ``blog.example.com``), also checks if the parent
        domain (``example.com``) has a DNS zone — if so, the subdomain is
        considered "owned" because we can add records to it.
        """
        try:
            dns = self._get_dns_service()
            response = dns.managedZones().list(project=self._project).execute()
            zones = response.get("managedZones", [])

            # Cloud DNS stores dnsName with trailing dot: "example.com."
            target_dns_name = f"{domain}."

            # 1. Exact match — zone exists for this domain
            for zone in zones:
                if zone.get("dnsName") == target_dns_name:
                    logger.info(
                        "Domain %s found in Cloud DNS zone '%s'",
                        domain, zone.get("name"),
                    )
                    return DomainCheckResult(
                        status="owned",
                        message=f"Domaine {domain} configuré dans Cloud DNS (zone: {zone.get('name')}).",
                    )

            # 2. Subdomain check — look for parent zone
            if self._is_subdomain(domain):
                parent = self._get_parent_domain(domain)
                parent_dns_name = f"{parent}."
                for zone in zones:
                    if zone.get("dnsName") == parent_dns_name:
                        logger.info(
                            "Subdomain %s — parent zone '%s' found for %s",
                            domain, zone.get("name"), parent,
                        )
                        return DomainCheckResult(
                            status="owned",
                            message=(
                                f"Sous-domaine {domain} — la zone DNS du domaine parent "
                                f"{parent} existe (zone: {zone.get('name')}). "
                                f"Un enregistrement A sera ajouté automatiquement."
                            ),
                        )

            return None
        except Exception as exc:
            logger.warning("Error checking Cloud DNS for %s: %s", domain, exc)
            return None

    def check_domain(self, domain: str) -> DomainCheckResult:
        """
        Check whether a domain is already managed in this GCP project,
        available for purchase, or unavailable.

        For subdomains (e.g. ``blog.example.com``), checks whether the
        parent domain's DNS zone exists.  Cloud Domains availability
        search is only performed for root domains.

        Checks in order:
        1. Cloud Domains registrations (bought via Google)
        2. Cloud DNS zones (domain or parent domain managed here)
        3. Cloud Domains availability search (root domains only)
        """
        is_subdomain = self._is_subdomain(domain)
        parent_domain = self._get_parent_domain(domain) if is_subdomain else domain

        # 1. Cloud Domains registration (check parent for subdomains)
        result = self._check_cloud_domains(parent_domain)
        if result:
            if is_subdomain:
                return DomainCheckResult(
                    status="owned",
                    message=(
                        f"Sous-domaine {domain} — le domaine parent {parent_domain} "
                        f"est enregistré via Google Cloud Domains. "
                        f"Un enregistrement A sera ajouté automatiquement."
                    ),
                )
            return result

        # 2. Cloud DNS zone (also checks parent for subdomains)
        result = self._check_cloud_dns(domain)
        if result:
            return result

        # 3. For subdomains without a parent zone, explain what's needed
        if is_subdomain:
            logger.info(
                "Subdomain %s — no parent zone found for %s",
                domain, parent_domain,
            )
            return DomainCheckResult(
                status="external",
                message=(
                    f"Sous-domaine {domain} — aucune zone DNS trouvée pour {parent_domain}. "
                    f"Une nouvelle zone DNS sera créée automatiquement. "
                    f"Vous devrez configurer les nameservers chez votre registrar."
                ),
            )

        logger.info("Domain %s not found in project — checking availability", domain)

        # 4. Check availability via searchDomains (root domains only)
        try:
            client = self._get_domains_client()
            response = client.search_domains(
                query=domain,
                location=self._parent,
            )

            for reg_params in response.register_parameters:
                if reg_params.domain_name == domain:
                    if reg_params.availability == domains_v1.RegisterParameters.Availability.AVAILABLE:
                        price = reg_params.yearly_price
                        amount = price.units + price.nanos / 1e9 if price else 0
                        currency = price.currency_code if price else "USD"
                        logger.info(
                            "Domain %s is available: %.2f %s/year",
                            domain, amount, currency,
                        )
                        return DomainCheckResult(
                            status="available",
                            price_amount=amount,
                            price_currency=currency,
                            message=f"Domaine {domain} disponible — {amount:.2f} {currency}/an",
                        )
                    else:
                        logger.info("Domain %s registered externally (not in GCP)", domain)
                        return DomainCheckResult(
                            status="external",
                            message=(
                                f"Domaine {domain} enregistré chez un registrar externe. "
                                f"Après le déploiement, configurez les nameservers pour pointer vers Google Cloud DNS."
                            ),
                        )

            return DomainCheckResult(
                status="external",
                message=(
                    f"Domaine {domain} non trouvé dans Google Cloud Domains. "
                    f"S'il est enregistré ailleurs, vous pouvez déployer et configurer les nameservers après."
                ),
            )

        except Exception as exc:
            logger.exception("Error searching domain availability for %s", domain)
            return DomainCheckResult(
                status="external",
                message=f"Impossible de vérifier le domaine (erreur API). Vous pouvez quand même déployer.",
            )

    # ── Register domain ──────────────────────────────────────────────

    def register_domain(self, domain: str) -> DomainRegisterResult:
        """
        Purchase/register a domain via Cloud Domains API.
        Uses WHOIS contact info from settings.
        """
        client = self._get_domains_client()
        settings = self._settings

        # Build contact info from settings
        contact = domains_v1.ContactSettings.Contact(
            postal_address=domains_v1.types.PostalAddress(
                region_code=settings.DOMAINS_CONTACT_COUNTRY,
                administrative_area=settings.DOMAINS_CONTACT_STATE,
                locality=settings.DOMAINS_CONTACT_CITY,
                address_lines=[settings.DOMAINS_CONTACT_ADDRESS] if settings.DOMAINS_CONTACT_ADDRESS else [],
                postal_code=settings.DOMAINS_CONTACT_POSTAL_CODE,
                organization=settings.DOMAINS_CONTACT_COMPANY,
                recipients=[settings.DOMAINS_CONTACT_EMAIL],
            ),
            email=settings.DOMAINS_CONTACT_EMAIL,
            phone_number=settings.DOMAINS_CONTACT_PHONE,
        )

        contact_settings = domains_v1.ContactSettings(
            privacy=domains_v1.ContactPrivacy.REDACTED_CONTACT_DATA,
            registrant_contact=contact,
            admin_contact=contact,
            technical_contact=contact,
        )

        # Get yearly price from availability check
        check_result = self.check_domain(domain)
        if check_result.status == "owned":
            return DomainRegisterResult(
                success=True,
                message=f"Domaine {domain} déjà enregistré — aucun achat nécessaire.",
            )
        if check_result.status != "available":
            return DomainRegisterResult(
                success=False,
                message=f"Impossible d'acheter {domain}: {check_result.message}",
            )

        try:
            # Retrieve register parameters for the yearly price
            response = client.search_domains(
                query=domain,
                location=self._parent,
            )

            yearly_price = None
            for result in response.register_parameters:
                if result.domain_name == domain:
                    yearly_price = result.yearly_price
                    break

            if yearly_price is None:
                return DomainRegisterResult(
                    success=False,
                    message=f"Impossible de déterminer le prix pour {domain}.",
                )

            registration = domains_v1.Registration(
                name=f"{self._parent}/registrations/{domain}",
                domain_name=domain,
                dns_settings=domains_v1.DnsSettings(
                    custom_dns=domains_v1.DnsSettings.CustomDns(
                        name_servers=["ns-cloud-a1.googledomains.com",
                                      "ns-cloud-a2.googledomains.com",
                                      "ns-cloud-a3.googledomains.com",
                                      "ns-cloud-a4.googledomains.com"],
                    ),
                ),
                contact_settings=contact_settings,
            )

            operation = client.register_domain(
                parent=self._parent,
                registration=registration,
                yearly_price=yearly_price,
            )

            logger.info("Domain registration started for %s — waiting for completion...", domain)
            operation.result(timeout=300)  # Wait up to 5 minutes

            logger.info("Domain %s registered successfully!", domain)
            return DomainRegisterResult(
                success=True,
                message=f"Domaine {domain} acheté avec succès!",
            )

        except Exception as exc:
            logger.exception("Failed to register domain %s", domain)
            return DomainRegisterResult(
                success=False,
                message=f"Erreur lors de l'achat du domaine {domain}: {exc}",
            )
