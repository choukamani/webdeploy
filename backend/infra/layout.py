"""
Resource layout — the single source of truth for which GCP resources make up
a website, and what they are called.

Both directions read from here: the deployers create exactly these names, and
the teardown planner deletes exactly these names. Keeping one definition is
what stops deletion from drifting out of sync with creation (the old code had
a separate, hand-maintained list per deployer, and prod had none at all).

Placements
----------
``sites``             The dedicated platform LB (``SITES_LB_PREFIX``). Every
                      website created from now on lands here.
``legacy_demo``       ``legacy-demo-lb``: per-site bucket + backend bucket + path rule
                      under ``DEMO_DOMAIN``. Existing sites stay put so their
                      URLs keep working; nothing new is added (it is full).
``legacy_prod``       ``websites-urlmap-prod``: per-domain bucket + backend
                      bucket + host rule + classic SSL cert.
``legacy_subdomain``  The retired subdomain mode (two sites remain).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from config import Settings
import hashlib

from infra.dns_ops import is_apex_domain
from infra.gcp_helpers import bounded_name, safe_name

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")


class Placement(str, Enum):
    SITES = "sites"
    LEGACY_DEMO = "legacy_demo"
    LEGACY_PROD = "legacy_prod"
    LEGACY_SUBDOMAIN = "legacy_subdomain"


def normalize_domain(domain: Optional[str]) -> str:
    """Canonical form of a user-typed domain: no scheme, path, port or dot."""
    d = (domain or "").strip().lower()
    d = re.sub(r"^[a-z]+://", "", d)
    d = d.split("/", 1)[0].split(":", 1)[0]
    return d.strip(".")


def website_key(mode: str, website_name: str, domain: Optional[str], settings: Settings) -> str:
    """Stable identity of a website across all of its deployments.

    Resources are named after the website, not the deployment, so every
    redeploy of the same key reuses (and a delete removes) the same set.
    Prod websites are identified by domain — that is what their resources
    are named after, and two prod deployments of one domain share them even
    when they were given different website names.
    """
    mode = str(getattr(mode, "value", mode))
    if mode == "prod":
        # A domain-less prod record (rejected by validation, so it owns
        # nothing) still needs a well-formed key to be deletable.
        return f"prod:{normalize_domain(domain) or website_name}"
    if mode == "subdomain":
        parent = normalize_domain(domain) or settings.DEMO_DOMAIN
        return f"subdomain:{website_name}.{parent}"
    return f"{mode}:{website_name}"


@dataclass(frozen=True)
class WebsiteRef:
    """Everything needed to locate a website's resources."""
    key: str
    mode: str
    website_name: str
    domain: str
    placement: Placement


# ═══════════════════════════════════════════════════════════════════════
#  Dedicated sites LB
# ═══════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class SitesNames:
    """Shared, provisioned-once components of the sites LB."""
    ip: str
    url_map: str
    http_url_map: str
    https_proxy: str
    http_proxy: str
    https_rule: str
    http_rule: str
    cert_map: str
    wildcard_cert: str
    wildcard_dns_auth: str
    demo_bucket: str
    demo_backend_bucket: str
    demo_matcher: str
    demo_host: str
    apps_domain: str
    dns_zone: str

    @classmethod
    def from_settings(cls, s: Settings) -> "SitesNames":
        p = s.SITES_LB_PREFIX
        return cls(
            ip=f"{p}-ip",
            url_map=f"{p}-urlmap",
            http_url_map=f"{p}-http-redirect",
            https_proxy=f"{p}-https-proxy",
            http_proxy=f"{p}-http-proxy",
            https_rule=f"{p}-https-rule",
            http_rule=f"{p}-http-rule",
            cert_map=f"{p}-certmap",
            wildcard_cert=bounded_name(f"{p}-wildcard-{safe_name(s.SITES_APPS_DOMAIN)}"),
            wildcard_dns_auth=bounded_name(f"{p}-dnsauth-{safe_name(s.SITES_APPS_DOMAIN)}"),
            demo_bucket=s.sites_demo_bucket_name,
            demo_backend_bucket=f"{p}-demo",
            demo_matcher="demo-sites",
            demo_host=s.SITES_DEMO_HOST,
            apps_domain=s.SITES_APPS_DOMAIN,
            dns_zone=s.SITES_DNS_ZONE,
        )


def demo_prefix(website_name: str) -> str:
    """Object prefix of a demo site inside the shared demo bucket.

    Validated hard: this string scopes a recursive delete inside a bucket
    that holds every other demo site, so it must never be empty or contain
    anything but a slug.
    """
    if not _SLUG_RE.match(website_name or "") or len(website_name) > 63:
        raise ValueError(f"Refusing unsafe demo prefix for website {website_name!r}")
    return f"{website_name}/"


@dataclass(frozen=True)
class ProdNames:
    hosts: list[str]
    bucket: str
    backend_bucket: str
    matcher: str
    cert: str
    cert_map_entries: dict[str, str] = field(default_factory=dict)  # hostname -> entry id
    zone_for_new: str = ""


def _tag(value: str) -> str:
    """Short digest of the *exact* name. ``safe_name`` is lossy — it maps
    ``my-shop.com`` and ``my.shop.com`` (or ``my--app`` and ``my-app``) to the
    same string — so names derived from it alone could let one website reuse,
    re-route or delete another's resources. The digest keeps them distinct."""
    return hashlib.sha1(value.encode()).hexdigest()[:6]


def _named(prefix: str, value: str, max_len: int = 63) -> str:
    """``{prefix}{safe value}-{digest}``, the digest kept even when truncating."""
    tag = _tag(value)
    return f"{prefix}{safe_name(value)[: max_len - len(prefix) - 7].rstrip('-')}-{tag}"


def prod_names(domain: str, s: Settings) -> ProdNames:
    """Per-domain resources of a prod website on the sites LB.

    The ``wd-`` / project prefixes keep them visibly apart from the legacy
    ``{domain}-bucket-prod`` family, and GCS bucket names are global, so the
    project id avoids collisions with buckets in other projects.
    """
    domain = normalize_domain(domain)
    hosts = [domain, f"www.{domain}"] if is_apex_domain(domain) else [domain]
    return ProdNames(
        hosts=hosts,
        bucket=_named(f"{s.PROJECT_ID}-wd-", domain),
        backend_bucket=_named("wd-", domain),
        matcher=_named("wd-", domain),
        cert=_named("wd-", domain),
        cert_map_entries={h: _named("wd-", h) for h in hosts},
        zone_for_new=bounded_name(f"{safe_name(domain)}-zone"),
    )


@dataclass(frozen=True)
class AppNames:
    service: str
    neg: str
    backend_service: str
    matcher: str
    host: str
    region: str
    ar_package: str


def app_names(website_name: str, s: Settings) -> AppNames:
    """Cloud Run website: the service plus its LB front door.

    Cloud Run service ids are capped at 49 characters; names that fit keep
    the ``safe_name`` form used before, so existing services are reused.
    """
    service = bounded_name(safe_name(website_name), 49)
    return AppNames(
        service=service,
        neg=_named("wd-neg-", website_name),
        backend_service=_named("wd-app-", website_name),
        matcher=_named("wd-app-", website_name),
        host=f"{website_name}.{s.SITES_APPS_DOMAIN}",
        region=s.CLOUDRUN_REGION,
        ar_package=website_name,
    )


# ═══════════════════════════════════════════════════════════════════════
#  Legacy load balancers
# ═══════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class LegacyDemoNames:
    url_map: str
    host: str
    paths: list[str]
    bucket: str
    backend_bucket: str


def legacy_demo_names(website_name: str, s: Settings) -> LegacyDemoNames:
    sn = safe_name(website_name)
    return LegacyDemoNames(
        url_map=s.DEMO_URL_MAP_NAME,
        host=s.DEMO_DOMAIN,
        paths=[f"/{website_name}", f"/{website_name}/*"],
        bucket=f"demo-{sn}-bucket-demo",
        backend_bucket=f"demo-{sn}-backend-demo",
    )


@dataclass(frozen=True)
class LegacyProdNames:
    url_map: str
    proxy: str
    ip_name: str
    hosts: list[str]
    bucket: str
    backend_bucket: str
    classic_cert: str


def legacy_prod_names(domain: str, s: Settings) -> LegacyProdNames:
    domain = normalize_domain(domain)
    sd = safe_name(domain)
    hosts = [domain, f"www.{domain}"] if is_apex_domain(domain) else [domain]
    return LegacyProdNames(
        url_map=s.PROD_URL_MAP_NAME,
        proxy=s.PROD_HTTPS_PROXY_NAME,
        ip_name=s.PROD_GLOBAL_IP_NAME,
        hosts=hosts,
        bucket=f"{sd}-bucket-prod",
        backend_bucket=f"{sd}-backend-prod",
        classic_cert=f"{sd}-ssl-cert",
    )


@dataclass(frozen=True)
class LegacySubdomainNames:
    url_map: str
    proxy: Optional[str]
    host: str
    bucket: str
    backend_bucket: str
    classic_cert: Optional[str]


def legacy_subdomain_names(website_name: str, parent: str, s: Settings) -> LegacySubdomainNames:
    sn = safe_name(website_name)
    parent = normalize_domain(parent) or s.DEMO_DOMAIN
    host = f"{website_name}.{parent}"
    internal = parent == s.DEMO_DOMAIN
    return LegacySubdomainNames(
        url_map=s.DEMO_URL_MAP_NAME if internal else s.PROD_URL_MAP_NAME,
        proxy=None if internal else s.PROD_HTTPS_PROXY_NAME,
        host=host,
        bucket=f"sub-{sn}-bucket-demo",
        backend_bucket=f"sub-{sn}-backend-demo",
        classic_cert=None if internal else f"{safe_name(host)}-ssl-cert",
    )


# ═══════════════════════════════════════════════════════════════════════
#  URLs
# ═══════════════════════════════════════════════════════════════════════

def public_url(ref: WebsiteRef, s: Settings) -> str:
    if ref.placement == Placement.LEGACY_DEMO:
        return f"https://{s.DEMO_DOMAIN}/{ref.website_name}/"
    if ref.placement == Placement.LEGACY_SUBDOMAIN:
        return f"https://{legacy_subdomain_names(ref.website_name, ref.domain, s).host}/"
    if ref.mode == "demo":
        return f"https://{s.SITES_DEMO_HOST}/{ref.website_name}/"
    if ref.mode == "cloudrun":
        return f"https://{app_names(ref.website_name, s).host}/"
    return f"https://{normalize_domain(ref.domain)}/"
