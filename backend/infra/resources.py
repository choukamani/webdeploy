"""
Idempotent create / delete primitives for the GCP resources a website uses.

All functions are synchronous (run them via ``asyncio.to_thread``) and safe
to call repeatedly:

- ``ensure_*`` returns the existing resource when it is already there.
- ``delete_*`` returns an *outcome* string instead of raising for the
  expected end states, so a retried teardown walks straight through work
  that an earlier attempt already finished:

    ``"deleted"``             removed now
    ``"absent"``              already gone (a previous attempt, or never made)
    ``"retained: <why>"``     deliberately kept — still used by something this
                              website does not own

  Anything else (permissions, quota, holds, timeouts) raises, which stops the
  teardown at that step so it can be retried once the cause is fixed.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterable, Optional

from google.api_core import exceptions as gexc
from google.cloud import storage as gcs
from googleapiclient.discovery import Resource

from config import Settings
from infra.gcp_helpers import (
    call_with_retries,
    is_in_use,
    is_not_found,
    wait_for_global_operation,
    wait_for_lro,
    wait_for_region_operation,
)

logger = logging.getLogger(__name__)

DELETED = "deleted"
ABSENT = "absent"


def retained(reason: str) -> str:
    return f"retained: {reason}"


def compute_link(project: str, collection: str, name: str, region: str | None = None) -> str:
    scope = f"regions/{region}" if region else "global"
    return f"https://www.googleapis.com/compute/v1/projects/{project}/{scope}/{collection}/{name}"


# ═══════════════════════════════════════════════════════════════════════
#  Cloud Storage
# ═══════════════════════════════════════════════════════════════════════

def _website_config(bucket: gcs.Bucket) -> dict:
    return dict(bucket._properties.get("website") or {})  # no public getter


def ensure_site_bucket(
    client: gcs.Client,
    name: str,
    *,
    settings: Settings,
    cors_origins: list[str],
    main_page: str = "index.html",
    not_found_page: str = "index.html",
) -> bool:
    """Create a publicly readable website bucket, or align an existing one's
    website config. Returns True when the bucket was created.

    Updating the config on every deploy matters: it used to be written only
    at creation, so a site first deployed without an index.html kept serving
    its old entry page forever, whatever later deployments contained.
    """
    bucket = client.lookup_bucket(name)
    if bucket is not None:
        set_website_config(bucket, main_page, not_found_page)
        return False

    bucket = client.bucket(name)
    bucket.iam_configuration.uniform_bucket_level_access_enabled = True
    bucket.versioning_enabled = False
    bucket.cors = [{
        "origin": cors_origins,
        "method": ["GET", "HEAD", "OPTIONS"],
        "responseHeader": ["Content-Type", "Access-Control-Allow-Origin", "x-goog-meta-*"],
        "maxAgeSeconds": settings.BUCKET_CORS_MAX_AGE,
    }]
    try:
        bucket.create(location=settings.BUCKET_LOCATION)
    except gexc.Conflict:
        # Created concurrently, or the name is taken by another project.
        bucket = client.lookup_bucket(name)
        if bucket is None:
            raise RuntimeError(f"Bucket name '{name}' is taken outside this project")
        set_website_config(bucket, main_page, not_found_page)
        return False

    bucket.configure_website(main_page_suffix=main_page, not_found_page=not_found_page)
    bucket.patch()
    policy = bucket.get_iam_policy(requested_policy_version=3)
    policy.bindings.append({"role": "roles/storage.objectViewer", "members": {"allUsers"}})
    bucket.set_iam_policy(policy)
    logger.info("Created public website bucket %s", name)
    return True


def set_website_config(bucket: gcs.Bucket, main_page: str, not_found_page: str) -> bool:
    wanted = {"mainPageSuffix": main_page, "notFoundPage": not_found_page}
    if _website_config(bucket) == wanted:
        return False
    bucket.configure_website(main_page_suffix=main_page, not_found_page=not_found_page)
    bucket.patch()
    logger.info("Updated website config of %s -> %s", bucket.name, wanted)
    return True


def _delete_blob(blob: gcs.Blob) -> None:
    try:
        # Generation-pinned, so a version uploaded concurrently is not removed by mistake.
        blob.bucket.delete_blob(blob.name, generation=blob.generation)
    except gexc.NotFound:
        pass


def delete_objects(client: gcs.Client, bucket: str, prefix: str = "") -> int:
    """Delete every object (all generations) under ``prefix``. Returns count.

    Raises on the first object that cannot be deleted (e.g. under a hold);
    objects already removed stay removed, so a retry finishes the rest.
    """
    blobs = list(client.list_blobs(bucket, prefix=prefix or None, versions=True))
    if not blobs:
        return 0
    with ThreadPoolExecutor(max_workers=16) as pool:
        # list() forces every future, re-raising the first failure.
        list(pool.map(_delete_blob, blobs))
    return len(blobs)


def backend_buckets_using(compute: Resource, project: str, bucket: str) -> list[str]:
    resp = call_with_retries(
        lambda: compute.backendBuckets()
        .list(project=project, filter=f'bucketName = "{bucket}"')
        .execute(),
        description=f"find backend buckets of {bucket}",
    )
    return [bb["name"] for bb in resp.get("items", [])]


def delete_bucket(client: gcs.Client, compute: Resource, project: str, name: str) -> str:
    """Delete a website bucket and its objects, unless a backend bucket still
    serves it (then it is shared with a route this website does not own)."""
    if client.lookup_bucket(name) is None:
        return ABSENT
    users = backend_buckets_using(compute, project, name)
    if users:
        return retained(f"still served by backend bucket(s) {', '.join(users)}")
    count = delete_objects(client, name)
    try:
        client.bucket(name).delete()
    except gexc.NotFound:
        return ABSENT
    logger.info("Deleted bucket %s (%d objects)", name, count)
    return DELETED


# ═══════════════════════════════════════════════════════════════════════
#  Compute — addresses, backend buckets, backend services, NEGs
# ═══════════════════════════════════════════════════════════════════════

def global_address(compute: Resource, project: str, name: str) -> str:
    return call_with_retries(
        lambda: compute.globalAddresses().get(project=project, address=name).execute(),
        description=f"read address {name}",
    )["address"]


def _get_or_none(request) -> Optional[dict]:
    try:
        return call_with_retries(request.execute, description="read resource")
    except Exception as exc:
        if is_not_found(exc):
            return None
        raise


def _insert_global(compute: Resource, project: str, collection, body: dict) -> None:
    try:
        op = call_with_retries(
            lambda: collection.insert(project=project, body=body).execute(),
            description=f"create {body.get('name')}",
        )
    except Exception as exc:
        if getattr(getattr(exc, "resp", None), "status", None) == 409:
            return  # created concurrently — same end state
        raise
    wait_for_global_operation(compute, project, op["name"])


def cdn_backend_bucket_body(name: str, bucket: str, settings: Settings) -> dict[str, Any]:
    return {
        "name": name,
        "bucketName": bucket,
        "enableCdn": True,
        "cdnPolicy": {
            "cacheMode": "CACHE_ALL_STATIC",
            "defaultTtl": settings.CDN_DEFAULT_TTL,
            "maxTtl": settings.CDN_MAX_TTL,
            "clientTtl": settings.CDN_CLIENT_TTL,
            "negativeCaching": settings.CDN_NEGATIVE_CACHING,
            "negativeCachingPolicy": [
                {"code": 404, "ttl": settings.CDN_NEGATIVE_CACHING_TTL},
                {"code": 410, "ttl": settings.CDN_NEGATIVE_CACHING_TTL},
            ],
        },
        "compressionMode": "AUTOMATIC",
        "customResponseHeaders": ["X-Content-Type-Options:nosniff"],
    }


def ensure_backend_bucket(
    compute: Resource, project: str, name: str, bucket: str, settings: Settings,
) -> str:
    existing = _get_or_none(compute.backendBuckets().get(project=project, backendBucket=name))
    if existing is None:
        _insert_global(
            compute, project, compute.backendBuckets(),
            cdn_backend_bucket_body(name, bucket, settings),
        )
        existing = compute.backendBuckets().get(project=project, backendBucket=name).execute()
    elif existing.get("bucketName") != bucket:
        raise RuntimeError(
            f"Backend bucket '{name}' already serves bucket '{existing.get('bucketName')}', "
            f"not '{bucket}' — refusing to reuse it."
        )
    return existing["selfLink"]


def _delete_global(compute: Resource, project: str, request, what: str) -> str:
    try:
        op = call_with_retries(request.execute, description=f"delete {what}")
    except Exception as exc:
        if is_not_found(exc):
            return ABSENT
        if is_in_use(exc):
            return retained(f"{what} is still referenced by another resource")
        raise
    wait_for_global_operation(compute, project, op["name"])
    return DELETED


def delete_backend_bucket(compute: Resource, project: str, name: str) -> str:
    return _delete_global(
        compute, project,
        compute.backendBuckets().delete(project=project, backendBucket=name),
        f"backend bucket {name}",
    )


def ensure_serverless_neg(
    compute: Resource, project: str, region: str, name: str, service: str,
) -> str:
    existing = _get_or_none(
        compute.regionNetworkEndpointGroups().get(
            project=project, region=region, networkEndpointGroup=name,
        )
    )
    if existing is None:
        body = {
            "name": name,
            "networkEndpointType": "SERVERLESS",
            "cloudRun": {"service": service},
        }
        try:
            op = call_with_retries(
                lambda: compute.regionNetworkEndpointGroups()
                .insert(project=project, region=region, body=body)
                .execute(),
                description=f"create NEG {name}",
            )
            wait_for_region_operation(compute, project, region, op["name"])
        except Exception as exc:
            if getattr(getattr(exc, "resp", None), "status", None) != 409:
                raise
        return compute_link(project, "networkEndpointGroups", name, region)
    if (existing.get("cloudRun") or {}).get("service") != service:
        raise RuntimeError(f"NEG '{name}' points at another Cloud Run service — refusing to reuse it.")
    return existing["selfLink"]


def delete_serverless_neg(compute: Resource, project: str, region: str, name: str) -> str:
    try:
        op = call_with_retries(
            lambda: compute.regionNetworkEndpointGroups()
            .delete(project=project, region=region, networkEndpointGroup=name)
            .execute(),
            description=f"delete NEG {name}",
        )
    except Exception as exc:
        if is_not_found(exc):
            return ABSENT
        if is_in_use(exc):
            return retained(f"NEG {name} is still used by a backend service")
        raise
    wait_for_region_operation(compute, project, region, op["name"])
    return DELETED


def ensure_backend_service(compute: Resource, project: str, name: str, neg_link: str) -> str:
    existing = _get_or_none(compute.backendServices().get(project=project, backendService=name))
    if existing is None:
        _insert_global(compute, project, compute.backendServices(), {
            "name": name,
            "loadBalancingScheme": "EXTERNAL_MANAGED",
            "protocol": "HTTPS",
            "backends": [{"group": neg_link}],
        })
        existing = compute.backendServices().get(project=project, backendService=name).execute()
    return existing["selfLink"]


def delete_backend_service(compute: Resource, project: str, name: str) -> str:
    return _delete_global(
        compute, project,
        compute.backendServices().delete(project=project, backendService=name),
        f"backend service {name}",
    )


_proxy_locks: dict[str, threading.Lock] = {}
_proxy_locks_guard = threading.Lock()


def _proxy_lock(proxy: str) -> threading.Lock:
    with _proxy_locks_guard:
        return _proxy_locks.setdefault(proxy, threading.Lock())


def detach_and_delete_classic_cert(
    compute: Resource, project: str, proxy: str, cert: str, allowed_domains: Iterable[str],
) -> str:
    """Legacy prod LB: unhook a classic managed cert from the shared proxy and
    delete it — only if it is a Google-managed cert covering nothing beyond
    this website's hostnames.

    The proxy's certificate list is shared by every legacy prod site and is
    set as a whole (no fingerprint), so concurrent teardowns are serialized
    per proxy and every attempt re-reads the list it edits.
    """
    existing = _get_or_none(compute.sslCertificates().get(project=project, sslCertificate=cert))
    if existing is None:
        return ABSENT
    if existing.get("type") != "MANAGED":
        return retained(f"certificate {cert} was uploaded by hand, not created by WebDeploy")
    domains = set((existing.get("managed") or {}).get("domains", []))
    extra = domains - set(allowed_domains)
    if extra:
        return retained(f"certificate {cert} also covers {', '.join(sorted(extra))}")

    def _detach() -> Optional[str]:
        proxy_body = _get_or_none(compute.targetHttpsProxies().get(project=project, targetHttpsProxy=proxy))
        if proxy_body is None:
            return None
        certs = proxy_body.get("sslCertificates", [])
        kept = [c for c in certs if not c.endswith(f"/{cert}")]
        if len(kept) == len(certs):
            return None
        if not kept:
            return retained(f"certificate {cert} is the last one on proxy {proxy}")
        op = compute.targetHttpsProxies().setSslCertificates(
            project=project, targetHttpsProxy=proxy, body={"sslCertificates": kept},
        ).execute()
        wait_for_global_operation(compute, project, op["name"])
        return None

    with _proxy_lock(proxy):
        blocked = call_with_retries(_detach, description=f"detach {cert} from {proxy}")
    if blocked:
        return blocked

    try:
        op = call_with_retries(
            lambda: compute.sslCertificates().delete(project=project, sslCertificate=cert).execute(),
            description=f"delete certificate {cert}",
        )
    except Exception as exc:
        if is_not_found(exc):
            return ABSENT
        if is_in_use(exc):
            # Ours (domains checked above) yet still attached somewhere:
            # fail so the delete is retried, rather than orphan it as "kept".
            raise RuntimeError(f"certificate {cert} is still attached to a proxy") from exc
        raise
    wait_for_global_operation(compute, project, op["name"])
    return DELETED


# ═══════════════════════════════════════════════════════════════════════
#  Certificate Manager
# ═══════════════════════════════════════════════════════════════════════

class CertManager:
    """Thin idempotent wrapper over the Certificate Manager v1 API (global)."""

    def __init__(self, client: Resource, project: str) -> None:
        self._c = client
        self._project = project
        self.parent = f"projects/{project}/locations/global"

    def _loc(self):
        return self._c.projects().locations()

    def _wait(self, op: dict) -> None:
        wait_for_lro(
            lambda: self._loc().operations().get(name=op["name"]).execute(),
            op["name"],
        )

    def _create(self, collection, *, parent: str, id_field: str, rid: str, body: dict) -> None:
        try:
            op = call_with_retries(
                lambda: collection.create(parent=parent, body=body, **{id_field: rid}).execute(),
                description=f"create {rid}",
            )
        except Exception as exc:
            if getattr(getattr(exc, "resp", None), "status", None) == 409:
                return
            raise
        self._wait(op)

    def _delete(self, collection, name: str) -> str:
        try:
            op = call_with_retries(lambda: collection.delete(name=name).execute(), description=f"delete {name}")
        except Exception as exc:
            if is_not_found(exc):
                return ABSENT
            if getattr(getattr(exc, "resp", None), "status", None) == 400 and "in use" in str(exc).lower():
                return retained(f"{name} is still referenced")
            raise
        self._wait(op)
        return DELETED

    # DNS authorizations ------------------------------------------------
    def dns_auth_name(self, rid: str) -> str:
        return f"{self.parent}/dnsAuthorizations/{rid}"

    def ensure_dns_authorization(self, rid: str, domain: str) -> dict:
        """Returns the CNAME record the authorization needs in DNS."""
        coll = self._loc().dnsAuthorizations()
        if _get_or_none(coll.get(name=self.dns_auth_name(rid))) is None:
            self._create(coll, parent=self.parent, id_field="dnsAuthorizationId", rid=rid,
                         body={"domain": domain})
        return coll.get(name=self.dns_auth_name(rid)).execute()["dnsResourceRecord"]

    # Certificates ------------------------------------------------------
    def cert_name(self, rid: str) -> str:
        return f"{self.parent}/certificates/{rid}"

    def get_cert(self, rid: str) -> Optional[dict]:
        return _get_or_none(self._loc().certificates().get(name=self.cert_name(rid)))

    def ensure_managed_cert(self, rid: str, domains: list[str], dns_authorizations: list[str] | None = None) -> dict:
        """Google-managed cert. Without DNS authorizations it uses load
        balancer authorization: it provisions once DNS points at the LB."""
        existing = self.get_cert(rid)
        if existing is not None:
            have = sorted((existing.get("managed") or {}).get("domains", []))
            if have != sorted(domains):
                raise RuntimeError(f"Certificate '{rid}' covers {have}, expected {sorted(domains)}")
            return existing
        managed: dict[str, Any] = {"domains": domains}
        if dns_authorizations:
            managed["dnsAuthorizations"] = [self.dns_auth_name(a) for a in dns_authorizations]
        self._create(self._loc().certificates(), parent=self.parent, id_field="certificateId",
                     rid=rid, body={"managed": managed, "description": "WebDeploy"})
        return self.get_cert(rid) or {}

    def delete_cert(self, rid: str) -> str:
        return self._delete(self._loc().certificates(), self.cert_name(rid))

    # Certificate maps --------------------------------------------------
    def map_name(self, rid: str) -> str:
        return f"{self.parent}/certificateMaps/{rid}"

    def ensure_map(self, rid: str) -> str:
        coll = self._loc().certificateMaps()
        if _get_or_none(coll.get(name=self.map_name(rid))) is None:
            self._create(coll, parent=self.parent, id_field="certificateMapId", rid=rid,
                         body={"description": "WebDeploy sites LB"})
        return self.map_name(rid)

    def entry_name(self, map_rid: str, entry_rid: str) -> str:
        return f"{self.map_name(map_rid)}/certificateMapEntries/{entry_rid}"

    def ensure_map_entry(
        self, map_rid: str, entry_rid: str, cert_rid: str, *, hostname: str | None = None,
    ) -> None:
        """Hostname entry, or the PRIMARY (no-SNI fallback) entry when
        ``hostname`` is None."""
        coll = self._loc().certificateMaps().certificateMapEntries()
        if _get_or_none(coll.get(name=self.entry_name(map_rid, entry_rid))) is not None:
            return
        body: dict[str, Any] = {"certificates": [self.cert_name(cert_rid)]}
        if hostname:
            body["hostname"] = hostname
        else:
            body["matcher"] = "PRIMARY"
        self._create(coll, parent=self.map_name(map_rid), id_field="certificateMapEntryId",
                     rid=entry_rid, body=body)

    def delete_map_entry(self, map_rid: str, entry_rid: str) -> str:
        return self._delete(
            self._loc().certificateMaps().certificateMapEntries(),
            self.entry_name(map_rid, entry_rid),
        )
