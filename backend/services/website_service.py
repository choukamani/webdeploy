"""
Websites — the unit that owns GCP resources.

A *deployment* is one run of the pipeline; a *website* is what those runs
build. Redeploying ``demo:my-site`` five times yields five deployment records
but one set of resources, so resources belong to the website, and deleting
means deleting the website: every resource, every deployment record, every
log line and ZIP backup. (The old code deleted per deployment record, which
tore down a live site when someone cleaned an old failed run out of history.)

Website documents live in Firestore ``websites/{key}``::

    key, mode, website_name, domain, placement, status, url,
    owner_email, current_deployment_id, resources, created_at, updated_at,
    deletion: {attempts, lease_until, requested_by, last_error, failed_step, steps}

Deletion state machine
----------------------
``active`` ─begin─► ``deleting`` ─ok─► (document removed, audit row written)
                         │
                         └─fail─► ``delete_failed`` ─retry─► ``deleting`` …

``deleting`` holds a lease. If the worker dies mid-teardown (instance
recycled, request timeout) the lease expires and the delete may be retried;
until then a concurrent delete is refused rather than run twice. Because
every teardown step is idempotent, a retry resumes where the last attempt
stopped. Deployments are refused while a website is not ``active``, and a
delete is refused while one of its deployments is queued or running.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Callable, Iterable, Optional

from google.api_core import exceptions as gexc
from google.cloud import firestore
from google.cloud import storage as gcs

from config import Settings
from db import crud
from infra import teardown, url_map_ops
from infra.gcp_helpers import get_credentials
from infra.layout import (
    Placement,
    WebsiteRef,
    legacy_demo_names,
    legacy_prod_names,
    normalize_domain,
    public_url,
    website_key,
)
from models.enums import DeploymentStatus, UserRole

logger = logging.getLogger("webdeploy.websites")

WEBSITES = "websites"
DELETION_AUDIT = "website_deletions"

ACTIVE = "active"
DELETING = "deleting"
DELETE_FAILED = "delete_failed"

LEASE = timedelta(minutes=15)
IN_FLIGHT = {DeploymentStatus.QUEUED.value, DeploymentStatus.RUNNING.value}


class WebsiteConflict(Exception):
    """The website is busy (deploying or being deleted)."""


class WebsiteForbidden(Exception):
    """The user may not delete this website."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(ts) -> Optional[datetime]:
    if ts is None or not hasattr(ts, "timestamp"):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _email(value) -> str:
    return (value or "").strip().casefold()


# ═══════════════════════════════════════════════════════════════════════
#  Lookup
# ═══════════════════════════════════════════════════════════════════════

def key_of(record, settings: Settings) -> str:
    stored = getattr(record, "website_key", None)
    if stored:
        return stored
    return website_key(
        getattr(record, "mode", ""), getattr(record, "website_name", ""),
        getattr(record, "domain", None), settings,
    )


def split_key(key: str) -> tuple[str, str]:
    mode, _, rest = key.partition(":")
    if not mode or not rest:
        raise ValueError(f"Malformed website key {key!r}")
    return mode, rest


def records_for(db, key: str, settings: Settings) -> list[SimpleNamespace]:
    """Every deployment record of a website, newest first.

    Queries use one equality filter (no composite index) and the key is
    matched in Python, which also covers records written before
    ``website_key`` was stored on them.
    """
    mode, rest = split_key(key)
    coll = db.collection(crud.DEPLOYMENTS)
    if mode in ("prod", "subdomain"):
        query = coll.where(filter=firestore.FieldFilter("mode", "==", mode))
    else:
        query = coll.where(filter=firestore.FieldFilter("website_name", "==", rest))
    records = [
        r for r in (crud._doc_to_record(d) for d in query.stream())
        if key_of(r, settings) == key
    ]
    records.sort(key=lambda r: _aware(getattr(r, "created_at", None)) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return records


def get_website(db, key: str) -> Optional[dict]:
    snap = db.collection(WEBSITES).document(key).get()
    return snap.to_dict() if snap.exists else None


def _infer_placement(mode: str, records: list) -> Placement:
    """Placement of a website that predates website documents."""
    for r in records:
        stored = getattr(r, "placement", None) or (getattr(r, "infra_details", None) or {}).get("placement")
        if stored:
            return Placement(stored)
    return {
        "demo": Placement.LEGACY_DEMO,
        "prod": Placement.LEGACY_PROD,
        "subdomain": Placement.LEGACY_SUBDOMAIN,
    }.get(mode, Placement.SITES)


def _ref(key: str, doc: dict) -> WebsiteRef:
    mode, rest = split_key(key)
    return WebsiteRef(
        key=key,
        mode=doc.get("mode") or mode,
        website_name=doc.get("website_name") or (rest if mode not in ("prod",) else ""),
        domain=doc.get("domain") or (rest if mode == "prod" else ""),
        placement=Placement(doc.get("placement") or _infer_placement(mode, []).value),
    )


def _doc_from_records(key: str, records: list, settings: Settings) -> dict:
    mode, _ = split_key(key)
    latest = records[0] if records else None
    oldest = records[-1] if records else None
    live = next((r for r in records if getattr(r, "status", "") == DeploymentStatus.SUCCESS.value), None)
    doc = {
        "key": key,
        "mode": mode,
        "website_name": getattr(latest, "website_name", "") if latest else split_key(key)[1],
        "domain": normalize_domain(getattr(latest, "domain", None)) if latest else "",
        "placement": _infer_placement(mode, records).value,
        "status": ACTIVE,
        "owner_email": getattr(oldest, "deployer_email", "") if oldest else "",
        "current_deployment_id": live.id if live else None,
        "created_at": _aware(getattr(oldest, "created_at", None)) or _now(),
        "updated_at": _now(),
        "backfilled": True,
    }
    doc["url"] = public_url(_ref(key, doc), settings)
    return doc


_INDEX_FIELDS = [
    "website_name", "mode", "domain", "website_key", "status", "deployer_email",
    "created_at", "infra_details",
]


def describe_many(db, keys: Iterable[str], settings: Settings) -> list[dict]:
    """:func:`describe` for many websites with two round trips in total —
    one scan of the deployment index and one batched read of website docs —
    instead of two queries per website."""
    keys = list(dict.fromkeys(keys))
    wanted = set(keys)
    grouped: dict[str, list] = {k: [] for k in keys}
    for snap in db.collection(crud.DEPLOYMENTS).select(_INDEX_FIELDS).stream():
        record = crud._doc_to_record(snap)
        k = key_of(record, settings)
        if k in wanted:
            grouped[k].append(record)
    for records in grouped.values():
        records.sort(key=lambda r: _aware(getattr(r, "created_at", None)) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    refs = [db.collection(WEBSITES).document(k) for k in keys]
    docs = {snap.id: snap.to_dict() for snap in db.get_all(refs) if snap.exists}
    return [
        describe(db, k, settings, records=grouped[k], doc=docs.get(k), fetch_doc=False)
        for k in keys
    ]


def describe(
    db, key: str, settings: Settings, records: Optional[list] = None,
    doc: Optional[dict] = None, fetch_doc: bool = True,
) -> dict:
    """Website summary for the API: status, URL, versions, plan."""
    records = records if records is not None else records_for(db, key, settings)
    if doc is None and fetch_doc:
        doc = get_website(db, key)
    if doc is None and records:
        doc = _doc_from_records(key, records, settings)
    if doc is None:
        return {"key": key, "exists": False}
    plan = teardown.plan_teardown(_ref(key, doc), settings)
    deletion = doc.get("deletion") or {}
    return {
        "key": key,
        "exists": True,
        "website_name": _ref(key, doc).website_name,
        "mode": _ref(key, doc).mode,
        "domain": doc.get("domain") or None,
        "placement": _ref(key, doc).placement.value,
        "url": doc.get("url"),
        "status": doc.get("status", ACTIVE),
        "live": any(getattr(r, "status", "") == DeploymentStatus.SUCCESS.value for r in records),
        "current_deployment_id": doc.get("current_deployment_id"),
        "versions": len(records),
        "deployment_ids": [r.id for r in records],
        "deployers": sorted({getattr(r, "deployer_email", "") or "" for r in records} - {""}),
        "in_flight": [r.id for r in records if getattr(r, "status", "") in IN_FLIGHT],
        "deletion_running": deletion_running(doc),
        "last_delete_error": deletion.get("last_error"),
        "resources": [st.description for st in plan],
    }


# ═══════════════════════════════════════════════════════════════════════
#  Deploy side
# ═══════════════════════════════════════════════════════════════════════

def check_deployable(db, key: str, settings: Settings) -> None:
    """Fail fast (before upload work starts) if the website is busy."""
    doc = get_website(db, key)
    if doc and doc.get("status", ACTIVE) != ACTIVE:
        raise WebsiteConflict(
            "This website is being deleted, or its last deletion failed. "
            "Finish (retry) the deletion before deploying it again."
        )
    busy = [r.id for r in records_for(db, key, settings) if getattr(r, "status", "") in IN_FLIGHT]
    if busy:
        raise WebsiteConflict(
            f"A deployment of this website is already in progress ({busy[0][:8]}). "
            "Wait for it to finish before deploying again."
        )


def check_cloud_run_name_free(db, key: str, settings: Settings) -> None:
    """Refuse a Cloud Run website whose service name is already taken by a
    service WebDeploy did not create.

    The project hosts hundreds of hand-made Cloud Run services (and the
    platform's own). Deploying would silently replace that service, and
    deleting the website would then delete it and its images. A website the
    platform already knows (it has records or a document) is its own.
    """
    from googleapiclient import discovery
    from infra.gcp_helpers import is_not_found
    from infra.layout import app_names

    if get_website(db, key) is not None or records_for(db, key, settings):
        return
    service = app_names(split_key(key)[1], settings).service
    run = discovery.build("run", "v2", cache_discovery=False,
                          credentials=get_credentials(settings.GOOGLE_APPLICATION_CREDENTIALS))
    name = f"projects/{settings.PROJECT_ID}/locations/{settings.CLOUDRUN_REGION}/services/{service}"
    try:
        run.projects().locations().services().get(name=name).execute()
    except Exception as exc:
        if is_not_found(exc):
            return
        raise
    raise WebsiteConflict(
        f"A Cloud Run service named '{service}' already exists and was not created by "
        "WebDeploy — choose another website name."
    )


def resolve_new_placement(compute, settings: Settings, mode: str, website_name: str, domain: str) -> Placement:
    """Where a website without a document lives.

    Sites that already exist on a legacy LB stay there, so redeploying them
    never changes a URL someone has already shared. Everything else goes to
    the sites LB.
    """
    project = settings.PROJECT_ID
    if mode == "demo":
        d = legacy_demo_names(website_name, settings)
        body = compute.urlMaps().get(project=project, urlMap=d.url_map).execute()
        if url_map_ops.has_path_rule(body, host=d.host, path=d.paths[1],
                                     service=url_map_ops.bucket_key(d.backend_bucket)):
            return Placement.LEGACY_DEMO
    elif mode == "prod":
        p = legacy_prod_names(domain, settings)
        body = compute.urlMaps().get(project=project, urlMap=p.url_map).execute()
        matcher = url_map_ops.find_matcher_for_host(body, normalize_domain(domain))
        if matcher and url_map_ops.service_key(matcher.get("defaultService", "")) == url_map_ops.bucket_key(p.backend_bucket):
            return Placement.LEGACY_PROD
    return Placement.SITES


def claim_for_deploy(
    db, settings: Settings, *, key: str, mode: str, website_name: str, domain: Optional[str],
    deployment_id: str, deployer_email: str, resolve_placement: Callable[[], Placement],
) -> dict:
    """Get (or create) the website document for a deployment's INFRA step.

    Refuses if the website is being deleted — the API checks this up front
    too, but a delete may have started while this deployment was queued.
    """
    ref = db.collection(WEBSITES).document(key)
    snap = ref.get()
    if snap.exists:
        doc = snap.to_dict()
        if doc.get("status", ACTIVE) != ACTIVE:
            raise WebsiteConflict("This website is being deleted — deployment cancelled.")
        ref.update({"last_deployment_id": deployment_id, "updated_at": _now()})
        return doc

    placement = resolve_placement()  # GCP reads — keep them out of the transaction
    doc = {
        "key": key,
        "mode": mode,
        "website_name": website_name,
        "domain": normalize_domain(domain),
        "placement": placement.value,
        "status": ACTIVE,
        "owner_email": deployer_email,
        "current_deployment_id": None,
        "last_deployment_id": deployment_id,
        "created_at": _now(),
        "updated_at": _now(),
    }
    doc["url"] = public_url(_ref(key, doc), settings)

    @firestore.transactional
    def _create(txn):
        current = ref.get(transaction=txn)
        if current.exists:
            return current.to_dict()  # created concurrently — use that one
        txn.set(ref, doc)
        return doc

    result = _create(db.transaction())
    if result.get("status", ACTIVE) != ACTIVE:
        raise WebsiteConflict("This website is being deleted — deployment cancelled.")
    return result


def _update_existing(ref, fields: dict) -> bool:
    """``update()`` (never ``set(merge=True)``): writing state onto a website
    that a finished delete just removed must not resurrect a partial doc."""
    try:
        ref.update(fields)
        return True
    except gexc.NotFound:
        return False


def mark_deployed(db, key: str, *, deployment_id: str, url: Optional[str], resources: list[dict]) -> None:
    _update_existing(db.collection(WEBSITES).document(key), {
        "current_deployment_id": deployment_id,
        "url": url,
        "resources": resources,
        "last_deployed_at": _now(),
        "updated_at": _now(),
    })


# ═══════════════════════════════════════════════════════════════════════
#  Delete side
# ═══════════════════════════════════════════════════════════════════════

def can_delete(user, records: list, doc: Optional[dict]) -> bool:
    """Admins may delete anything; others only websites that are entirely
    theirs — a website a colleague also deployed to is not one person's to
    destroy. Emails compare case-insensitively (records hold both forms)."""
    if getattr(user, "role", None) == UserRole.ADMIN.value:
        return True
    me = _email(getattr(user, "email", ""))
    if not me:
        return False
    if records:
        return all(_email(getattr(r, "deployer_email", "")) == me for r in records)
    return bool(doc) and _email(doc.get("owner_email")) == me


def begin_delete(db, key: str, settings: Settings, requested_by: str) -> tuple[dict, list]:
    """Atomically move a website into ``deleting``; returns (doc, records)."""
    ref = db.collection(WEBSITES).document(key)
    mode, rest = split_key(key)
    coll = db.collection(crud.DEPLOYMENTS)
    query = (
        coll.where(filter=firestore.FieldFilter("mode", "==", mode))
        if mode in ("prod", "subdomain")
        else coll.where(filter=firestore.FieldFilter("website_name", "==", rest))
    )

    @firestore.transactional
    def _begin(txn):
        snap = ref.get(transaction=txn)
        records = [
            r for r in (crud._doc_to_record(d) for d in txn.get(query))
            if key_of(r, settings) == key
        ]
        records.sort(key=lambda r: _aware(getattr(r, "created_at", None)) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        doc = snap.to_dict() if snap.exists else _doc_from_records(key, records, settings)
        if not snap.exists and not records:
            raise LookupError(f"Website {key} not found")

        deletion = dict(doc.get("deletion") or {})
        if deletion_running(doc):
            raise WebsiteConflict("A deletion of this website is already running.")
        busy = [r.id for r in records if getattr(r, "status", "") in IN_FLIGHT]
        if busy:
            raise WebsiteConflict(
                f"Deployment {busy[0][:8]} of this website is still in progress — "
                "wait for it to finish, then delete."
            )

        deletion.update({
            "attempts": int(deletion.get("attempts", 0)) + 1,
            "started_at": _now(),
            "lease_until": _now() + LEASE,
            "requested_by": requested_by,
        })
        doc.update({"status": DELETING, "deletion": deletion, "updated_at": _now()})
        txn.set(ref, doc)
        return doc, records

    doc, records = _begin(db.transaction())
    _stamp_records(db, records, {"deletion_status": DELETING, "deletion_error": None})
    return doc, records


def _stamp_records(db, records: Iterable, fields: dict) -> None:
    """Mirror the website's deletion state onto its deployment rows, which is
    what the history table lists. Best effort, row by row: a row removed in
    the meantime must not abort the state change it is mirroring."""
    for r in records:
        try:
            db.collection(crud.DEPLOYMENTS).document(r.id).update(fields)
        except Exception:
            logger.debug("Could not flag deployment %s with %s", r.id, fields, exc_info=True)


def fail_delete(db, key: str, records: list, report: teardown.TeardownReport) -> None:
    exists = _update_existing(db.collection(WEBSITES).document(key), {
        "status": DELETE_FAILED,
        "deletion.last_error": report.error,
        "deletion.failed_step": report.failed_step,
        "deletion.steps": [r.as_dict() for r in report.results],
        "deletion.failed_at": _now(),
        "deletion.lease_until": None,
        "updated_at": _now(),
    })
    if exists:
        _stamp_records(db, records, {"deletion_status": DELETE_FAILED, "deletion_error": report.error})


def renew_lease(db, key: str) -> None:
    """Restart the lease when this website's teardown actually begins — in a
    bulk request it may wait behind others for a while after begin_delete."""
    _update_existing(db.collection(WEBSITES).document(key), {"deletion.lease_until": _now() + LEASE})


def purge_metadata(db, settings: Settings, key: str, doc: dict, records: list,
                   report: teardown.TeardownReport, requested_by: str) -> None:
    """Last step of a successful teardown: logs, records, ZIP backups, the
    website document itself — then an audit row of what was removed."""
    for r in records:
        crud.delete_deployment(db, r.id)
    try:
        bucket = gcs.Client(
            project=settings.PROJECT_ID,
            credentials=get_credentials(settings.GOOGLE_APPLICATION_CREDENTIALS),
        ).bucket(settings.deploy_uploads_bucket_name)
        for r in records:
            try:
                bucket.blob(f"uploads/{r.id}.zip").delete()
            except Exception:
                pass  # absent, or never backed up
    except Exception:
        logger.warning("Could not remove ZIP backups for %s", key, exc_info=True)

    # Audit row first: if it fails the website document survives (still
    # "deleting", retryable) instead of a later write resurrecting a stub.
    db.collection(DELETION_AUDIT).add({
        "key": key,
        "website_name": doc.get("website_name"),
        "mode": doc.get("mode"),
        "domain": doc.get("domain"),
        "placement": doc.get("placement"),
        "url": doc.get("url"),
        "deployments_removed": [r.id for r in records],
        "steps": [r.as_dict() for r in report.results],
        "warnings": report.warnings,
        "requested_by": requested_by,
        "attempts": (doc.get("deletion") or {}).get("attempts"),
        "deleted_at": _now(),
    })
    db.collection(WEBSITES).document(key).delete()


def deletion_running(doc: Optional[dict]) -> bool:
    """A deletion holds the website only while its lease is live."""
    if not doc or doc.get("status") != DELETING:
        return False
    lease = _aware((doc.get("deletion") or {}).get("lease_until"))
    return bool(lease and lease > _now())


INTERRUPTED = "Deletion was interrupted before it finished — retry it."


def expire_stale_deletions(db, settings: Settings) -> int:
    """Turn deletions whose worker vanished into retryable failures — on the
    website and on its history rows, which is where the Retry button lives."""
    count = 0
    query = db.collection(WEBSITES).where(filter=firestore.FieldFilter("status", "==", DELETING))
    for snap in query.stream():
        if deletion_running(snap.to_dict()):
            continue
        if not _update_existing(snap.reference, {
            "status": DELETE_FAILED,
            "deletion.last_error": INTERRUPTED,
            "deletion.lease_until": None,
            "updated_at": _now(),
        }):
            continue
        _stamp_records(db, records_for(db, snap.id, settings),
                       {"deletion_status": DELETE_FAILED, "deletion_error": INTERRUPTED})
        count += 1
    return count


# ═══════════════════════════════════════════════════════════════════════
#  Orchestration
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class DeleteOutcome:
    key: str
    website_name: str = ""
    mode: str = ""
    status: str = "blocked"  # deleted | delete_failed | blocked
    # Why it was blocked: not_found | forbidden | conflict | error
    code: Optional[str] = None
    error: Optional[str] = None
    failed_step: Optional[str] = None
    deployments_removed: int = 0
    steps: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return self.__dict__.copy()


async def delete_websites(
    db, settings: Settings, keys: list[str], user, *, concurrency: int = 5,
) -> list[DeleteOutcome]:
    """Delete websites: every resource, record, log and backup.

    Never raises per website — each gets an outcome, so one failure cannot
    strand the rest of a bulk request. URL-map edits for the whole batch are
    applied first, one patch per map, instead of one patch per website.
    """
    requested_by = getattr(user, "email", "") or "unknown"
    outcomes: dict[str, DeleteOutcome] = {k: DeleteOutcome(key=k) for k in dict.fromkeys(keys)}
    started: dict[str, tuple[dict, list, list[teardown.Step]]] = {}

    # 1 — permission + state transition, one website at a time.
    for key in outcomes:
        out = outcomes[key]
        try:
            records = await asyncio.to_thread(records_for, db, key, settings)
            doc = await asyncio.to_thread(get_website, db, key)
            if not records and doc is None:
                out.status, out.code, out.error = "blocked", "not_found", "Website not found (already deleted?)"
                continue
            if not can_delete(user, records, doc):
                raise WebsiteForbidden(
                    "This website has deployments by other users — only an admin can delete it."
                )
            # Plan first: a website with no valid plan must not be left "deleting".
            preview_doc = doc or _doc_from_records(key, records, settings)
            teardown.plan_teardown(_ref(key, preview_doc), settings)
            doc, records = await asyncio.to_thread(begin_delete, db, key, settings, requested_by)
            ref = _ref(key, doc)
            out.website_name, out.mode = ref.website_name, ref.mode
            started[key] = (doc, records, teardown.plan_teardown(ref, settings))
        except WebsiteConflict as exc:
            out.status, out.code, out.error = "blocked", "conflict", str(exc)
        except WebsiteForbidden as exc:
            out.status, out.code, out.error = "blocked", "forbidden", str(exc)
        except LookupError as exc:
            out.status, out.code, out.error = "blocked", "not_found", str(exc)
        except Exception as exc:
            logger.exception("Could not start deletion of %s", key)
            out.status, out.code, out.error = "blocked", "error", f"Could not start deletion: {exc}"

    if not started:
        return list(outcomes.values())

    # 2 — routing, batched per URL map.
    routing_done: dict[str, dict[str, teardown.OpOutcome]] = {k: {} for k in started}
    routing_failed: dict[str, str] = {}
    by_map: dict[str, list[tuple[str, teardown.Step]]] = {}
    for key, (_, _, plan) in started.items():
        for st in plan:
            if st.action in teardown.ROUTING_ACTIONS:
                by_map.setdefault(st.params["url_map"], []).append((key, st))

    def _route_batch(url_map: str, items: list[tuple[str, teardown.Step]]) -> None:
        ops = teardown.GcpOps(settings)
        # Step ids are per-website; namespace them for the shared patch.
        steps = [teardown.Step(f"{k}|{st.id}", st.phase, st.action, st.params, st.description)
                 for k, st in items]
        try:
            results = ops.apply_route_removals(url_map, steps)
            for k, st in items:
                routing_done[k][st.id] = results[f"{k}|{st.id}"]
        except Exception as exc:
            logger.exception("Routing removal on %s failed", url_map)
            for k, st in items:
                routing_failed[k] = f"{st.description}: {exc}"

    for url_map, items in by_map.items():
        await asyncio.to_thread(_route_batch, url_map, items)

    # 3 — everything else, per website, a few at a time.
    semaphore = asyncio.Semaphore(concurrency)

    def _teardown_one(key: str) -> teardown.TeardownReport:
        doc, records, plan = started[key]
        if key in routing_failed:
            routing_step = next(st for st in plan if st.action in teardown.ROUTING_ACTIONS)
            report = teardown.TeardownReport()
            report.results.append(teardown.StepResult(
                routing_step.id, routing_step.description, f"failed: {routing_failed[key]}"))
            report.failed_step, report.error = routing_step.id, routing_failed[key]
            fail_delete(db, key, records, report)
            return report
        renew_lease(db, key)
        executor = teardown.TeardownExecutor(
            teardown.GcpOps(settings), log=lambda m: logger.info("[%s] %s", key, m),
        )
        report = executor.run(plan, done=routing_done[key])
        if report.ok:
            purge_metadata(db, settings, key, doc, records, report, requested_by)
        else:
            fail_delete(db, key, records, report)
        return report

    async def _run(key: str) -> None:
        out = outcomes[key]
        async with semaphore:
            try:
                report = await asyncio.to_thread(_teardown_one, key)
            except Exception as exc:  # bookkeeping itself failed
                logger.exception("Deletion of %s crashed", key)
                report = teardown.TeardownReport(failed_step="internal", error=str(exc))
                try:
                    await asyncio.to_thread(fail_delete, db, key, started[key][1], report)
                except Exception:
                    logger.exception("Could not record failed deletion of %s", key)
        out.steps = [r.as_dict() for r in report.results]
        out.warnings = report.warnings
        if report.ok:
            out.status, out.deployments_removed = "deleted", len(started[key][1])
        else:
            out.status, out.error, out.failed_step = DELETE_FAILED, report.error, report.failed_step

    await asyncio.gather(*(_run(k) for k in started))
    for out in outcomes.values():
        logger.warning("Website delete %s by %s: %s%s", out.key, requested_by, out.status,
                       f" ({out.error})" if out.error else "")
    return list(outcomes.values())


def delete_history_entry(db, settings: Settings, deployment_id: str, user) -> dict:
    """Remove one deployment record (and its logs/backup) — never resources.

    Allowed only for a version that is not the one serving the site: it is
    history cleanup, e.g. an old or failed run of a website that stays live.
    Deleting the last or live version means deleting the website.
    """
    record = crud.get_deployment(db, deployment_id)
    if record is None:
        raise LookupError(f"Deployment '{deployment_id}' not found.")
    if getattr(user, "role", None) != UserRole.ADMIN.value and \
            _email(getattr(record, "deployer_email", "")) != _email(getattr(user, "email", "")):
        raise WebsiteForbidden("You can only delete your own deployments.")
    if getattr(record, "status", "") in IN_FLIGHT:
        raise WebsiteConflict("This deployment is still running.")

    key = key_of(record, settings)
    records = records_for(db, key, settings)
    doc = get_website(db, key) or {}
    live_id = doc.get("current_deployment_id") or next(
        (r.id for r in records if getattr(r, "status", "") == DeploymentStatus.SUCCESS.value), None)
    if len(records) <= 1:
        raise WebsiteConflict("This is the website's only deployment — delete the website instead.")
    if deployment_id == live_id:
        raise WebsiteConflict("This is the version currently live — delete the website instead.")

    crud.delete_deployment(db, deployment_id)
    try:
        gcs.Client(
            project=settings.PROJECT_ID,
            credentials=get_credentials(settings.GOOGLE_APPLICATION_CREDENTIALS),
        ).bucket(settings.deploy_uploads_bucket_name).blob(f"uploads/{deployment_id}.zip").delete()
    except Exception:
        pass
    return {"deleted": True, "deployment_id": deployment_id, "website_key": key, "scope": "record"}
