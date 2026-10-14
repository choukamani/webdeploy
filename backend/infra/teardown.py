"""
Website teardown — plan, then execute, idempotently.

``plan_teardown`` turns a website into an ordered list of steps, derived from
``infra.layout`` (the same names the deployers create). It is pure, so what a
delete will touch can be previewed and unit-tested without GCP.

``TeardownExecutor`` runs a plan through ``GcpOps``. Every step is
idempotent ("already gone" counts as done), so the recovery story for a
partial failure is simply *run the same plan again*: finished steps fall
straight through, and the run resumes at the step that failed.

Order matters and is enforced by phase: routing first (nothing may be served
from a resource about to vanish, and GCP refuses to delete a backend that a
URL map still references), then certificates, backends, storage, DNS, and
finally compute. Execution stops at the first failing step; nothing after it
runs, so a later step can never act on a half-finished earlier one.

Ownership guards live in the operations, not the plan: a routing rule,
certificate, bucket or DNS record that is also used by something this
website did not create is *retained* (reported, not deleted). When a host's
routing is retained, every host-scoped step for it (cert, DNS) is retained
too — the host is still serving someone else's site.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from google.cloud import storage as gcs
from googleapiclient import discovery

from config import Settings
from infra import dns_ops, resources, url_map_ops
from infra.gcp_helpers import (
    call_with_retries,
    get_credentials,
    is_not_found,
    mutate_url_map,
    wait_for_lro,
)
from infra.layout import (
    Placement,
    SitesNames,
    WebsiteRef,
    app_names,
    demo_prefix,
    legacy_demo_names,
    legacy_prod_names,
    legacy_subdomain_names,
    prod_names,
)

logger = logging.getLogger(__name__)

# Phases — lower runs first.
ROUTING, CERTS, BACKENDS, STORAGE, DNS, COMPUTE, CDN = 10, 20, 30, 40, 50, 60, 70

ROUTING_ACTIONS = {"url_map.remove_host_route", "url_map.remove_path_rules"}


@dataclass(frozen=True)
class Step:
    id: str
    phase: int
    action: str
    params: dict
    description: str
    hosts: tuple[str, ...] = ()
    fatal: bool = True


@dataclass
class StepResult:
    id: str
    description: str
    outcome: str  # deleted | absent | retained: … | skipped: … | failed: …

    def as_dict(self) -> dict:
        return {"step": self.id, "description": self.description, "outcome": self.outcome}


@dataclass
class TeardownReport:
    results: list[StepResult] = field(default_factory=list)
    failed_step: Optional[str] = None
    error: Optional[str] = None
    retained_hosts: set[str] = field(default_factory=set)

    @property
    def ok(self) -> bool:
        return self.failed_step is None

    @property
    def warnings(self) -> list[str]:
        return [
            f"{r.description}: {r.outcome}" for r in self.results
            if r.outcome.startswith(("retained", "skipped"))
        ]


# ═══════════════════════════════════════════════════════════════════════
#  Planner (pure)
# ═══════════════════════════════════════════════════════════════════════

def plan_teardown(ref: WebsiteRef, s: Settings) -> list[Step]:
    n = SitesNames.from_settings(s)
    steps: list[Step] = []
    add = steps.append

    if ref.mode in ("prod", "subdomain") and not ref.domain and ref.placement != Placement.LEGACY_SUBDOMAIN:
        # A prod record without a domain never got past validation, so it
        # cannot own resources (they are all named after the domain).
        return []

    if ref.mode == "cloudrun":
        # Same plan for Cloud Run websites made before the sites LB existed:
        # their LB steps simply find nothing.
        a = app_names(ref.website_name, s)
        add(Step("routing", ROUTING, "url_map.remove_host_route",
                 {"url_map": n.url_map, "hosts": [a.host],
                  "owned": [url_map_ops.backend_service_key(a.backend_service)]},
                 f"Host rule {a.host} on {n.url_map}"))
        add(Step("backend-service", BACKENDS, "backend_service.delete",
                 {"name": a.backend_service}, f"Backend service {a.backend_service}"))
        add(Step("neg", BACKENDS + 1, "neg.delete",
                 {"name": a.neg, "region": a.region}, f"Serverless NEG {a.neg}"))
        add(Step("dns", DNS, "dns.cleanup_host",
                 {"domain": a.host, "address": n.ip}, f"DNS record {a.host}", hosts=(a.host,)))
        add(Step("cloud-run", COMPUTE, "run.delete_service",
                 {"name": a.service, "region": a.region}, f"Cloud Run service {a.service}"))
        add(Step("images", COMPUTE + 1, "ar.delete_package",
                 {"package": a.ar_package, "region": a.region, "repo": s.CLOUDRUN_ARTIFACT_REPO},
                 f"Artifact Registry images {a.ar_package}"))

    elif ref.placement == Placement.SITES and ref.mode == "demo":
        prefix = demo_prefix(ref.website_name)
        add(Step("files", STORAGE, "gcs.delete_prefix",
                 {"bucket": n.demo_bucket, "prefix": prefix},
                 f"Site files gs://{n.demo_bucket}/{prefix}"))
        add(Step("cdn", CDN, "cdn.invalidate",
                 {"url_map": n.url_map, "host": n.demo_host, "path": f"/{prefix}*"},
                 f"CDN cache /{prefix}*", fatal=False))

    elif ref.placement == Placement.SITES and ref.mode == "prod":
        p = prod_names(ref.domain, s)
        hosts = tuple(p.hosts)
        add(Step("routing", ROUTING, "url_map.remove_host_route",
                 {"url_map": n.url_map, "hosts": list(hosts),
                  "owned": [url_map_ops.bucket_key(p.backend_bucket)]},
                 f"Host rule {', '.join(hosts)} on {n.url_map}"))
        for host, entry in p.cert_map_entries.items():
            add(Step(f"cert-entry:{host}", CERTS, "certmap.delete_entry",
                     {"map": n.cert_map, "entry": entry},
                     f"Certificate map entry {host}", hosts=(host,)))
        add(Step("cert", CERTS + 1, "certmanager.delete_cert", {"name": p.cert},
                 f"Certificate {p.cert}", hosts=hosts))
        add(Step("backend-bucket", BACKENDS, "backend_bucket.delete",
                 {"name": p.backend_bucket}, f"Backend bucket {p.backend_bucket}"))
        add(Step("bucket", STORAGE, "gcs.delete_bucket", {"name": p.bucket}, f"Bucket gs://{p.bucket}"))
        add(Step("dns", DNS, "dns.cleanup_host",
                 {"domain": ref.domain, "address": n.ip}, f"DNS records {ref.domain}", hosts=hosts))

    elif ref.placement == Placement.LEGACY_DEMO:
        d = legacy_demo_names(ref.website_name, s)
        add(Step("routing", ROUTING, "url_map.remove_path_rules",
                 {"url_map": d.url_map, "host": d.host, "paths": d.paths,
                  "owned": [url_map_ops.bucket_key(d.backend_bucket)]},
                 f"Path rules {', '.join(d.paths)} on {d.url_map}"))
        add(Step("backend-bucket", BACKENDS, "backend_bucket.delete",
                 {"name": d.backend_bucket}, f"Backend bucket {d.backend_bucket}"))
        add(Step("bucket", STORAGE, "gcs.delete_bucket", {"name": d.bucket}, f"Bucket gs://{d.bucket}"))
        add(Step("cdn", CDN, "cdn.invalidate",
                 {"url_map": d.url_map, "host": None, "path": f"/{ref.website_name}/*"},
                 f"CDN cache /{ref.website_name}/*", fatal=False))

    elif ref.placement == Placement.LEGACY_PROD:
        p = legacy_prod_names(ref.domain, s)
        hosts = tuple(p.hosts)
        add(Step("routing", ROUTING, "url_map.remove_host_route",
                 {"url_map": p.url_map, "hosts": list(hosts),
                  "owned": [url_map_ops.bucket_key(p.backend_bucket)]},
                 f"Host rule {', '.join(hosts)} on {p.url_map}"))
        add(Step("cert", CERTS, "classic_cert.detach_and_delete",
                 {"proxy": p.proxy, "name": p.classic_cert, "allowed": list(hosts)},
                 f"SSL certificate {p.classic_cert}", hosts=hosts))
        add(Step("backend-bucket", BACKENDS, "backend_bucket.delete",
                 {"name": p.backend_bucket}, f"Backend bucket {p.backend_bucket}"))
        add(Step("bucket", STORAGE, "gcs.delete_bucket", {"name": p.bucket}, f"Bucket gs://{p.bucket}"))
        add(Step("dns", DNS, "dns.cleanup_host",
                 {"domain": ref.domain, "address": p.ip_name}, f"DNS records {ref.domain}", hosts=hosts))

    elif ref.placement == Placement.LEGACY_SUBDOMAIN:
        sd = legacy_subdomain_names(ref.website_name, ref.domain, s)
        add(Step("routing", ROUTING, "url_map.remove_host_route",
                 {"url_map": sd.url_map, "hosts": [sd.host],
                  "owned": [url_map_ops.bucket_key(sd.backend_bucket)]},
                 f"Host rule {sd.host} on {sd.url_map}"))
        if sd.classic_cert:
            add(Step("cert", CERTS, "classic_cert.detach_and_delete",
                     {"proxy": sd.proxy, "name": sd.classic_cert, "allowed": [sd.host]},
                     f"SSL certificate {sd.classic_cert}", hosts=(sd.host,)))
        add(Step("backend-bucket", BACKENDS, "backend_bucket.delete",
                 {"name": sd.backend_bucket}, f"Backend bucket {sd.backend_bucket}"))
        add(Step("bucket", STORAGE, "gcs.delete_bucket", {"name": sd.bucket}, f"Bucket gs://{sd.bucket}"))
        # DNS for these sites lives at the client's registrar — nothing of ours to remove.

    else:
        raise ValueError(f"No teardown plan for mode={ref.mode} placement={ref.placement}")

    return sorted(steps, key=lambda st: st.phase)


# ═══════════════════════════════════════════════════════════════════════
#  GCP operations
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class OpOutcome:
    outcome: str
    retained_hosts: set[str] = field(default_factory=set)


def _route_outcome(change: url_map_ops.RouteChange) -> OpOutcome:
    if change.retained:
        return OpOutcome("retained: " + "; ".join(change.retained), set(change.retained_hosts))
    return OpOutcome(resources.DELETED if change.changed else resources.ABSENT)


class GcpOps:
    """Executes teardown steps against GCP. One instance per thread —
    googleapiclient service objects are not thread-safe."""

    def __init__(self, settings: Settings, credentials=None) -> None:
        self.s = settings
        self.project = settings.PROJECT_ID
        self._creds = credentials or get_credentials(settings.GOOGLE_APPLICATION_CREDENTIALS)
        self._clients: dict[str, object] = {}

    def _client(self, api: str, version: str):
        key = f"{api}/{version}"
        if key not in self._clients:
            self._clients[key] = discovery.build(api, version, credentials=self._creds, cache_discovery=False)
        return self._clients[key]

    @property
    def compute(self):
        return self._client("compute", "v1")

    @property
    def storage(self) -> gcs.Client:
        if "storage" not in self._clients:
            self._clients["storage"] = gcs.Client(project=self.project, credentials=self._creds)
        return self._clients["storage"]  # type: ignore[return-value]

    def run(self, step: Step) -> OpOutcome:
        handler = getattr(self, "_" + step.action.replace(".", "_"))
        result = handler(**step.params)
        return result if isinstance(result, OpOutcome) else OpOutcome(result)

    # ── routing ────────────────────────────────────────────────────────
    def apply_route_removals(self, url_map: str, steps: list[Step]) -> dict[str, OpOutcome]:
        """Apply several routing removals to one URL map in a single patch.

        Bulk deletes would otherwise patch a shared map once per website,
        each patch waiting on GCP and serialized by the map's lock. Returns
        an outcome per step id; raises if the patch itself fails.
        """
        outcomes: dict[str, OpOutcome] = {}

        def _mutate(body: dict) -> bool:
            outcomes.clear()  # re-run from scratch on every retry's fresh GET
            changed = False
            for st in steps:
                p = st.params
                if st.action == "url_map.remove_host_route":
                    change = url_map_ops.remove_host_route(
                        body, hosts=p["hosts"], owned_services=set(p["owned"]))
                else:
                    change = url_map_ops.remove_path_rules(
                        body, host=p["host"], paths=p["paths"], owned_services=set(p["owned"]))
                outcomes[st.id] = _route_outcome(change)
                changed = changed or change.changed
            return changed

        try:
            mutate_url_map(self.compute, self.project, url_map, _mutate)
        except Exception as exc:
            if is_not_found(exc):
                return {st.id: OpOutcome(resources.ABSENT) for st in steps}
            raise
        return dict(outcomes)

    def _url_map_remove_host_route(self, **params) -> OpOutcome:
        step = Step("x", ROUTING, "url_map.remove_host_route", params, "")
        return self.apply_route_removals(params["url_map"], [step])["x"]

    def _url_map_remove_path_rules(self, **params) -> OpOutcome:
        step = Step("x", ROUTING, "url_map.remove_path_rules", params, "")
        return self.apply_route_removals(params["url_map"], [step])["x"]

    def _cdn_invalidate(self, url_map: str, host: Optional[str], path: str) -> str:
        body = {"path": path}
        if host:
            body["host"] = host
        try:
            # Fire and forget: invalidation is advisory (HTML is never cached,
            # assets expire within the hour) and waiting adds minutes.
            self.compute.urlMaps().invalidateCache(
                project=self.project, urlMap=url_map, body=body).execute()
        except Exception as exc:
            if is_not_found(exc):
                return resources.ABSENT
            raise
        return "invalidated"

    # ── certificates ───────────────────────────────────────────────────
    def _certs(self) -> resources.CertManager:
        return resources.CertManager(self._client("certificatemanager", "v1"), self.project)

    def _certmap_delete_entry(self, map: str, entry: str) -> str:
        return self._certs().delete_map_entry(map, entry)

    def _certmanager_delete_cert(self, name: str) -> str:
        return self._certs().delete_cert(name)

    def _classic_cert_detach_and_delete(self, proxy: str, name: str, allowed: list[str]) -> str:
        return resources.detach_and_delete_classic_cert(self.compute, self.project, proxy, name, allowed)

    # ── backends ───────────────────────────────────────────────────────
    def _backend_bucket_delete(self, name: str) -> str:
        return resources.delete_backend_bucket(self.compute, self.project, name)

    def _backend_service_delete(self, name: str) -> str:
        return resources.delete_backend_service(self.compute, self.project, name)

    def _neg_delete(self, name: str, region: str) -> str:
        return resources.delete_serverless_neg(self.compute, self.project, region, name)

    # ── storage ────────────────────────────────────────────────────────
    def _gcs_delete_prefix(self, bucket: str, prefix: str) -> str:
        # Re-validate at the last moment: exactly "{slug}/" — never bare, never
        # without the slash (which would also match "{slug}-2/…").
        if not prefix.endswith("/") or demo_prefix(prefix[:-1]) != prefix:
            raise ValueError(f"Refusing to delete unsafe prefix {prefix!r}")
        count = resources.delete_objects(self.storage, bucket, prefix)
        return f"deleted ({count} objects)" if count else resources.ABSENT

    def _gcs_delete_bucket(self, name: str) -> str:
        return resources.delete_bucket(self.storage, self.compute, self.project, name)

    # ── DNS ────────────────────────────────────────────────────────────
    def _dns_cleanup_host(self, domain: str, address: str) -> str:
        dns = self._client("dns", "v1")
        try:
            ip = resources.global_address(self.compute, self.project, address)
        except Exception as exc:
            if not is_not_found(exc):
                raise
            return "skipped: load balancer address no longer exists"
        lines = dns_ops.cleanup_host_records(dns, self.project, domain, [ip])
        retained_lines = [line for line in lines if "retained" in line]
        if retained_lines:
            return "retained: " + "; ".join(retained_lines)
        return resources.DELETED if any("deleted" in line for line in lines) else resources.ABSENT

    # ── compute ────────────────────────────────────────────────────────
    def _run_delete_service(self, name: str, region: str) -> str:
        run = self._client("run", "v2")
        full = f"projects/{self.project}/locations/{region}/services/{name}"
        try:
            op = call_with_retries(
                lambda: run.projects().locations().services().delete(name=full).execute(),
                description=f"delete Cloud Run {name}",
            )
        except Exception as exc:
            if is_not_found(exc):
                return resources.ABSENT
            raise
        wait_for_lro(
            lambda: run.projects().locations().operations().get(name=op["name"]).execute(),
            op["name"],
        )
        return resources.DELETED

    def _ar_delete_package(self, package: str, region: str, repo: str) -> str:
        ar = self._client("artifactregistry", "v1")
        full = f"projects/{self.project}/locations/{region}/repositories/{repo}/packages/{package}"
        try:
            op = call_with_retries(
                lambda: ar.projects().locations().repositories().packages().delete(name=full).execute(),
                description=f"delete images {package}",
            )
        except Exception as exc:
            if is_not_found(exc):
                return resources.ABSENT
            raise
        if not op.get("done"):
            wait_for_lro(
                lambda: ar.projects().locations().operations().get(name=op["name"]).execute(),
                op["name"],
            )
        return resources.DELETED


# ═══════════════════════════════════════════════════════════════════════
#  Executor
# ═══════════════════════════════════════════════════════════════════════

class TeardownExecutor:
    def __init__(self, ops: GcpOps, log: Callable[[str], None] = lambda m: None) -> None:
        self.ops = ops
        self.log = log

    def run(
        self,
        steps: Iterable[Step],
        *,
        done: Optional[dict[str, OpOutcome]] = None,
    ) -> TeardownReport:
        """Run ``steps`` in phase order, stopping at the first fatal failure.

        ``done`` carries outcomes of steps already executed elsewhere (the
        batched routing phase of a bulk delete) so they are recorded, not rerun.
        """
        report = TeardownReport()
        done = done or {}
        for step in sorted(steps, key=lambda st: st.phase):
            if step.id in done:
                outcome = done[step.id]
            elif step.hosts and set(step.hosts) & report.retained_hosts:
                outcome = OpOutcome("retained: host is still routed to a site this website does not own")
            else:
                try:
                    outcome = self.ops.run(step)
                except Exception as exc:
                    if not step.fatal:
                        outcome = OpOutcome(f"skipped: {exc}")
                    else:
                        logger.exception("Teardown step %s failed", step.id)
                        report.results.append(StepResult(step.id, step.description, f"failed: {exc}"))
                        report.failed_step = step.id
                        report.error = f"{step.description}: {exc}"
                        self.log(f"[DELETE] FAILED {step.description}: {exc}")
                        return report
            report.retained_hosts |= outcome.retained_hosts
            report.results.append(StepResult(step.id, step.description, outcome.outcome))
            self.log(f"[DELETE] {step.description}: {outcome.outcome}")
        return report
