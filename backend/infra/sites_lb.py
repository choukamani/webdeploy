"""
The dedicated platform load balancer ("sites LB").

    Global IP ──┬── :443  HTTPS proxy ── certificate map (Certificate Manager)
                │              │            ├─ PRIMARY / *.{apps}: wildcard cert (DNS-authorized)
                │              │            └─ {prod domain}: per-domain cert (LB-authorized)
                │              ▼
                │           URL map ── host {demo host} ─► shared demo backend bucket ─► gs://…-demo-sites/{site}/…
                │                    ├─ host {app}.{apps}  ─► backend service ─► serverless NEG ─► Cloud Run
                │                    └─ host {prod domain} ─► per-domain backend bucket ─► per-domain bucket
                └── :80   HTTP proxy ── redirect URL map (301 → https)

Why demo sites share one bucket: legacy-demo-lb filled up because every demo site
added a path rule (GCP caps a URL map at 64 kB ≈ 530 rules) *and* a backend
bucket (project quota 600 — 569 were in use). Serving every demo site from
one backend bucket, under its own object prefix, means a new demo site
touches neither: no URL map change, no new backend resource. The URL map only
grows for prod domains and Cloud Run apps, which are rare.

``SitesLB.provision()`` builds the shared part once (idempotent; run via
``scripts/provision_sites_lb.py``). The ``deploy_*`` / ``expose_*`` methods
add one website at a time and are idempotent, so a redeploy or a retry after
a partial failure converges instead of duplicating.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from google.cloud import storage as gcs
from googleapiclient import discovery

from config import Settings
from infra import dns_ops, resources, url_map_ops
from infra.gcp_helpers import (
    call_with_retries,
    get_credentials,
    mutate_url_map,
    wait_for_global_operation,
)
from infra.layout import (
    Placement,
    SitesNames,
    app_names,
    demo_prefix,
    normalize_domain,
    prod_names,
)
from models.deployment import DeploymentResult

logger = logging.getLogger(__name__)

NOT_FOUND_PAGE = "404.html"
_NOT_FOUND_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Not found</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{font-family:system-ui,sans-serif;display:grid;place-items:center;min-height:90vh;color:#374151}</style>
</head><body><main><h1>404</h1><p>There is no website at this address.</p></main></body></html>
"""


class SitesLB:
    """Operations on the dedicated sites LB. Synchronous — call from a thread."""

    def __init__(
        self,
        settings: Settings,
        log: Optional[Callable[[str], None]] = None,
        credentials=None,
    ) -> None:
        self.s = settings
        self.n = SitesNames.from_settings(settings)
        self.project = settings.PROJECT_ID
        self._log = log or (lambda msg: logger.info(msg))
        creds = credentials or get_credentials(settings.GOOGLE_APPLICATION_CREDENTIALS)
        self.compute = discovery.build("compute", "v1", credentials=creds, cache_discovery=False)
        self.dns = discovery.build("dns", "v1", credentials=creds, cache_discovery=False)
        self.certs = resources.CertManager(
            discovery.build("certificatemanager", "v1", credentials=creds, cache_discovery=False),
            self.project,
        )
        self.storage = gcs.Client(project=self.project, credentials=creds)
        self._ip: Optional[str] = None

    @staticmethod
    def lb_name(settings: Settings) -> str:
        return SitesNames.from_settings(settings).url_map

    def log(self, msg: str) -> None:
        try:
            self._log(msg)
        except Exception:
            logger.info(msg)

    def ip(self) -> str:
        if self._ip is None:
            self._ip = resources.global_address(self.compute, self.project, self.n.ip)
        return self._ip

    def _link(self, collection: str, name: str) -> str:
        return resources.compute_link(self.project, collection, name)

    # ═══════════════════════════════════════════════════════════════════
    #  Per-website deploy operations
    # ═══════════════════════════════════════════════════════════════════

    def deploy_demo(self, website_name: str) -> DeploymentResult:
        """A demo site needs no per-site infrastructure: it is an object
        prefix in the shared demo bucket, already routed by the LB."""
        prefix = demo_prefix(website_name)
        if self.storage.lookup_bucket(self.n.demo_bucket) is None:
            raise RuntimeError(
                f"Shared demo bucket '{self.n.demo_bucket}' is missing — "
                "run scripts/provision_sites_lb.py"
            )
        url = f"https://{self.n.demo_host}/{website_name}/"
        self.log(f"[INFRA] Demo site → gs://{self.n.demo_bucket}/{prefix} (sites LB, no routing change)")
        return DeploymentResult(
            mode="demo",
            website_name=website_name,
            success=True,
            url=url,
            storage_bucket=self.n.demo_bucket,
            backend_bucket=self.n.demo_backend_bucket,
            upload_prefix=prefix,
            placement=Placement.SITES.value,
            lb_name=self.n.url_map,
            cdn_url_map=self.n.url_map,
            cdn_host=self.n.demo_host,
            cdn_path=f"/{prefix}*",
            resources=[
                {"kind": "gcs_prefix", "name": f"gs://{self.n.demo_bucket}/{prefix}", "shared_bucket": True},
            ],
        )

    def deploy_prod(self, website_name: str, domain: str) -> DeploymentResult:
        domain = normalize_domain(domain)
        names = prod_names(domain, self.s)
        ip = self.ip()
        self.log(f"[INFRA] Prod site {domain} on sites LB (IP {ip})")

        created = resources.ensure_site_bucket(
            self.storage, names.bucket, settings=self.s,
            cors_origins=[f"https://{h}" for h in names.hosts],
        )
        self.log(f"[INFRA] Bucket {'created' if created else 'ready'}: {names.bucket}")

        bb_link = resources.ensure_backend_bucket(
            self.compute, self.project, names.backend_bucket, names.bucket, self.s,
        )
        self.log(f"[INFRA] Backend bucket ready: {names.backend_bucket}")

        self._route_hosts(names.hosts, names.matcher, bb_link)

        self.certs.ensure_managed_cert(names.cert, names.hosts)
        for host, entry in names.cert_map_entries.items():
            self.certs.ensure_map_entry(self.n.cert_map, entry, names.cert, hostname=host)
        self.log(
            f"[INFRA] Certificate {names.cert} for {', '.join(names.hosts)} "
            "(Google provisions it once DNS points at the LB; can take up to an hour)"
        )

        nameservers: list[str] = []
        if self.s.PROD_AUTO_CREATE_DNS_ZONE:
            nameservers = dns_ops.ensure_host_records(
                self.dns, self.project, domain, ip,
                zone_name_for_new=names.zone_for_new, allow_create_zone=True, log=self.log,
            )
        else:
            self.log(f"[INFRA] ACTION REQUIRED: point {', '.join(names.hosts)} at {ip} (A record)")
        if nameservers:
            self.log(f"[INFRA] Set these nameservers at the registrar: {', '.join(nameservers)}")

        return DeploymentResult(
            mode="prod",
            website_name=website_name,
            success=True,
            url=f"https://{domain}/",
            storage_bucket=names.bucket,
            backend_bucket=names.backend_bucket,
            url_map_updated=True,
            dns_nameservers=nameservers,
            upload_prefix="",
            placement=Placement.SITES.value,
            lb_name=self.n.url_map,
            cdn_url_map=self.n.url_map,
            cdn_host=domain,
            cdn_path="/*",
            resources=[
                {"kind": "gcs_bucket", "name": names.bucket},
                {"kind": "backend_bucket", "name": names.backend_bucket},
                {"kind": "host_rule", "name": ", ".join(names.hosts), "url_map": self.n.url_map},
                {"kind": "certificate", "name": names.cert},
                {"kind": "dns", "name": domain, "ip": ip},
            ],
        )

    def expose_cloud_run(self, website_name: str) -> dict:
        """Put a deployed Cloud Run service behind the LB at {app}.{apps domain}."""
        names = app_names(website_name, self.s)
        ip = self.ip()

        current = dns_ops.get_record(self.dns, self.project, self.n.dns_zone, names.host, "A")
        if current and current.get("rrdatas") != [ip]:
            raise RuntimeError(
                f"{names.host} already has DNS records pointing at "
                f"{', '.join(current.get('rrdatas', []))} — pick another website name."
            )

        neg_link = resources.ensure_serverless_neg(
            self.compute, self.project, names.region, names.neg, names.service,
        )
        bes_link = resources.ensure_backend_service(
            self.compute, self.project, names.backend_service, neg_link,
        )
        self.log(f"[INFRA] Serverless NEG {names.neg} + backend service {names.backend_service} ready")

        self._route_hosts([names.host], names.matcher, bes_link)
        dns_ops.ensure_record(self.dns, self.project, self.n.dns_zone, names.host, "A", [ip])
        self.log(f"[INFRA] https://{names.host}/ → Cloud Run '{names.service}' (wildcard certificate)")
        return {
            "url": f"https://{names.host}/",
            "resources": [
                {"kind": "cloud_run_service", "name": names.service},
                {"kind": "serverless_neg", "name": names.neg},
                {"kind": "backend_service", "name": names.backend_service},
                {"kind": "host_rule", "name": names.host, "url_map": self.n.url_map},
                {"kind": "dns", "name": names.host, "ip": ip},
            ],
        }

    def _route_hosts(self, hosts: list[str], matcher: str, service_link: str) -> None:
        def _mutate(url_map: dict) -> bool:
            return url_map_ops.add_host_route(
                url_map, hosts=hosts, matcher_name=matcher, service_link=service_link,
            )

        mutate_url_map(self.compute, self.project, self.n.url_map, _mutate)
        self.log(f"[INFRA] URL map {self.n.url_map}: {', '.join(hosts)} → {service_link.rsplit('/', 1)[-1]}")

    # ═══════════════════════════════════════════════════════════════════
    #  Status
    # ═══════════════════════════════════════════════════════════════════

    def status(self) -> dict:
        url_map = self.compute.urlMaps().get(project=self.project, urlMap=self.n.url_map).execute()
        size = url_map_ops.estimate_size(url_map)
        return {
            "url_map": self.n.url_map,
            "ip": self.ip(),
            "demo_host": self.n.demo_host,
            "host_rules": len(url_map.get("hostRules", [])),
            "estimated_size_bytes": size,
            "limit_bytes": url_map_ops.URL_MAP_LIMIT_BYTES,
            "used_percent": round(100 * size / url_map_ops.URL_MAP_LIMIT_BYTES, 1),
        }

    # ═══════════════════════════════════════════════════════════════════
    #  One-time provisioning (idempotent)
    # ═══════════════════════════════════════════════════════════════════

    def provision(self) -> dict:
        n, p, c = self.n, self.project, self.compute

        # 1 — Global IP
        if resources._get_or_none(c.globalAddresses().get(project=p, address=n.ip)) is None:
            resources._insert_global(c, p, c.globalAddresses(), {
                "name": n.ip, "ipVersion": "IPV4", "addressType": "EXTERNAL",
                "networkTier": "PREMIUM", "description": "WebDeploy sites LB",
            })
        ip = self.ip()
        self.log(f"IP {n.ip} = {ip}")

        # 2 — Shared demo bucket + its 404 page
        resources.ensure_site_bucket(
            self.storage, n.demo_bucket, settings=self.s,
            cors_origins=[f"https://{n.demo_host}"],
            main_page="index.html", not_found_page=NOT_FOUND_PAGE,
        )
        blob = self.storage.bucket(n.demo_bucket).blob(NOT_FOUND_PAGE)
        blob.cache_control = "public, max-age=300"
        blob.upload_from_string(_NOT_FOUND_HTML, content_type="text/html")
        self.log(f"Demo bucket gs://{n.demo_bucket} ready")

        # 3 — Shared demo backend bucket (CDN)
        demo_bb = resources.ensure_backend_bucket(c, p, n.demo_backend_bucket, n.demo_bucket, self.s)
        self.log(f"Demo backend bucket {n.demo_backend_bucket} ready")

        # 4 — HTTPS URL map: unknown hosts and the demo host → demo bucket
        if resources._get_or_none(c.urlMaps().get(project=p, urlMap=n.url_map)) is None:
            resources._insert_global(c, p, c.urlMaps(), {
                "name": n.url_map,
                "description": "WebDeploy sites LB — managed by the platform, do not edit by hand",
                "defaultService": demo_bb,
                "hostRules": [{"hosts": [n.demo_host], "pathMatcher": n.demo_matcher}],
                "pathMatchers": [{"name": n.demo_matcher, "defaultService": demo_bb}],
            })
        self.log(f"URL map {n.url_map} ready")

        # 5 — HTTP → HTTPS redirect URL map
        if resources._get_or_none(c.urlMaps().get(project=p, urlMap=n.http_url_map)) is None:
            resources._insert_global(c, p, c.urlMaps(), {
                "name": n.http_url_map,
                "defaultUrlRedirect": {
                    "httpsRedirect": True,
                    "redirectResponseCode": "MOVED_PERMANENTLY_DEFAULT",
                    "stripQuery": False,
                },
            })

        # 6 — Wildcard certificate for demo host + apps, DNS-authorized
        record = self.certs.ensure_dns_authorization(n.wildcard_dns_auth, n.apps_domain)
        dns_ops.ensure_record(
            self.dns, p, n.dns_zone, record["name"], record["type"], [record["data"]],
        )
        cert_domains = [f"*.{n.apps_domain}"]
        if not _covered_by_wildcard(n.demo_host, n.apps_domain):
            cert_domains.append(n.demo_host)
        self.certs.ensure_managed_cert(n.wildcard_cert, cert_domains, [n.wildcard_dns_auth])
        cert_map = self.certs.ensure_map(n.cert_map)
        self.certs.ensure_map_entry(n.cert_map, "primary", n.wildcard_cert)
        self.certs.ensure_map_entry(n.cert_map, "wildcard", n.wildcard_cert, hostname=f"*.{n.apps_domain}")
        if not _covered_by_wildcard(n.demo_host, n.apps_domain):
            self.certs.ensure_map_entry(n.cert_map, "demo-host", n.wildcard_cert, hostname=n.demo_host)
        self.log(f"Certificate map {n.cert_map} with {n.wildcard_cert} ({', '.join(cert_domains)})")

        # 7 — Proxies
        if resources._get_or_none(c.targetHttpsProxies().get(project=p, targetHttpsProxy=n.https_proxy)) is None:
            resources._insert_global(c, p, c.targetHttpsProxies(), {
                "name": n.https_proxy,
                "urlMap": self._link("urlMaps", n.url_map),
                "certificateMap": f"//certificatemanager.googleapis.com/{cert_map}",
            })
        if resources._get_or_none(c.targetHttpProxies().get(project=p, targetHttpProxy=n.http_proxy)) is None:
            resources._insert_global(c, p, c.targetHttpProxies(), {
                "name": n.http_proxy,
                "urlMap": self._link("urlMaps", n.http_url_map),
            })

        # 8 — Forwarding rules
        for rule, port, target in (
            (n.https_rule, "443", self._link("targetHttpsProxies", n.https_proxy)),
            (n.http_rule, "80", self._link("targetHttpProxies", n.http_proxy)),
        ):
            if resources._get_or_none(c.globalForwardingRules().get(project=p, forwardingRule=rule)) is None:
                op = call_with_retries(lambda: c.globalForwardingRules().insert(project=p, body={
                    "name": rule,
                    "IPAddress": self._link("addresses", n.ip),
                    "IPProtocol": "TCP",
                    "portRange": port,
                    "target": target,
                    "loadBalancingScheme": "EXTERNAL_MANAGED",
                    "networkTier": "PREMIUM",
                }).execute(), description=f"create {rule}")
                wait_for_global_operation(c, p, op["name"])
        self.log("Proxies and forwarding rules ready (:443, :80)")

        # 9 — Demo hostname → LB
        dns_ops.ensure_record(self.dns, p, n.dns_zone, n.demo_host, "A", [ip])
        self.log(f"DNS {n.demo_host} → {ip}")

        cert = self.certs.get_cert(n.wildcard_cert) or {}
        return {
            "ip": ip,
            "url_map": n.url_map,
            "demo_url": f"https://{n.demo_host}/<site>/",
            "apps_url": f"https://<app>.{n.apps_domain}/",
            "certificate_state": (cert.get("managed") or {}).get("state"),
        }


def _covered_by_wildcard(host: str, apex: str) -> bool:
    return host.endswith(f".{apex}") and "." not in host[: -len(apex) - 1]
