"""
Cloud DNS helpers shared by the deployers and the teardown engine.

Everything here is synchronous and idempotent. Deletes are *guarded*: a
record is removed only while it still holds the value WebDeploy wrote (an A
record pointing at one of our load balancers, a ``www`` CNAME pointing at the
apex). If someone has since re-pointed it, it now belongs to them and stays.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

from googleapiclient.discovery import Resource

from infra.gcp_helpers import call_with_retries, is_not_found

logger = logging.getLogger(__name__)

# Zones WebDeploy creates carry this marker in their description; only those
# are ever candidates for deletion.
ZONE_MARKER = "(WebDeploy)"

# Public suffixes with two labels, so "example.co.uk" counts as an apex
# domain (gets www + its own zone) rather than a subdomain of "co.uk".
_TWO_LABEL_SUFFIXES = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "ltd.uk", "plc.uk",
    "com.au", "net.au", "org.au", "co.nz", "org.nz", "co.za", "com.br",
    "co.jp", "com.mx", "com.tr", "co.in", "com.sg", "com.hk",
}


def fqdn(name: str) -> str:
    return name if name.endswith(".") else f"{name}."


def is_apex_domain(domain: str) -> bool:
    """True for ``example.com`` / ``example.co.uk``; False for subdomains."""
    labels = domain.strip(".").lower().split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in _TWO_LABEL_SUFFIXES:
        return len(labels) == 3
    return len(labels) == 2


def apex_of(domain: str) -> str:
    labels = domain.strip(".").lower().split(".")
    keep = 3 if len(labels) >= 3 and ".".join(labels[-2:]) in _TWO_LABEL_SUFFIXES else 2
    return ".".join(labels[-keep:])


def _list_zones(dns: Resource, project: str) -> list[dict]:
    zones, token = [], None
    while True:
        resp = call_with_retries(
            lambda: dns.managedZones().list(project=project, pageToken=token).execute(),
            description="list DNS zones",
        )
        zones.extend(resp.get("managedZones", []))
        token = resp.get("nextPageToken")
        if not token:
            return zones


def find_zone_for(dns: Resource, project: str, name: str) -> dict | None:
    """Most specific public zone in the project that contains ``name``."""
    target = fqdn(name.lower())
    best = None
    for zone in _list_zones(dns, project):
        if zone.get("visibility", "public") != "public":
            continue
        zone_name = zone.get("dnsName", "")
        if target == zone_name or target.endswith("." + zone_name):
            if best is None or len(zone_name) > len(best["dnsName"]):
                best = zone
    return best


def get_record(dns: Resource, project: str, zone: str, name: str, rtype: str) -> dict | None:
    resp = call_with_retries(
        lambda: dns.resourceRecordSets()
        .list(project=project, managedZone=zone, name=fqdn(name), type=rtype)
        .execute(),
        description=f"get DNS {rtype} {name}",
    )
    rrsets = resp.get("rrsets", [])
    return rrsets[0] if rrsets else None


def ensure_record(
    dns: Resource, project: str, zone: str, name: str, rtype: str,
    rrdatas: list[str], ttl: int = 300,
) -> bool:
    """Create or overwrite a record set. Returns True when it changed."""
    current = get_record(dns, project, zone, name, rtype)
    if current and current.get("rrdatas") == rrdatas and current.get("ttl") == ttl:
        return False
    change: dict[str, Any] = {
        "additions": [{"name": fqdn(name), "type": rtype, "ttl": ttl, "rrdatas": rrdatas}],
    }
    if current:
        change["deletions"] = [current]
    call_with_retries(
        lambda: dns.changes().create(project=project, managedZone=zone, body=change).execute(),
        description=f"write DNS {rtype} {name}",
    )
    logger.info("DNS %s %s -> %s (zone %s)", rtype, name, rrdatas, zone)
    return True


def delete_record_if(
    dns: Resource, project: str, zone: str, name: str, rtype: str,
    expected: Iterable[str],
) -> str:
    """Delete a record only if all its values are in ``expected``.

    Returns ``"deleted"``, ``"absent"``, or ``"retained: <reason>"``.
    """
    current = get_record(dns, project, zone, name, rtype)
    if current is None:
        return "absent"
    expected = {fqdn(v) if rtype == "CNAME" else v for v in expected}
    values = set(current.get("rrdatas", []))
    if not values <= expected:
        return f"retained: {rtype} {name} now points to {', '.join(sorted(values))}"
    try:
        call_with_retries(
            lambda: dns.changes()
            .create(project=project, managedZone=zone, body={"deletions": [current]})
            .execute(),
            description=f"delete DNS {rtype} {name}",
        )
    except Exception as exc:
        # A concurrent delete wins the race with a 404 — same end state.
        if is_not_found(exc):
            return "absent"
        raise
    return "deleted"


def create_zone(dns: Resource, project: str, zone_name: str, domain: str) -> dict:
    body = {
        "name": zone_name,
        "dnsName": fqdn(domain),
        "description": f"Managed zone for {domain} {ZONE_MARKER}",
    }
    return call_with_retries(
        lambda: dns.managedZones().create(project=project, body=body).execute(),
        description=f"create DNS zone {zone_name}",
    )


def delete_zone_if_unused(dns: Resource, project: str, zone: dict) -> str:
    """Delete a WebDeploy-created zone once it holds nothing but NS/SOA.

    Zones are user-visible: people add MX/TXT records to them for email and
    verification. A zone with anything beyond the apex NS/SOA is kept.
    """
    if ZONE_MARKER not in zone.get("description", ""):
        return "retained: zone was not created by WebDeploy"
    name = zone["name"]
    records, token = [], None
    while True:
        resp = call_with_retries(
            lambda: dns.resourceRecordSets()
            .list(project=project, managedZone=name, pageToken=token)
            .execute(),
            description=f"list records in {name}",
        )
        records.extend(resp.get("rrsets", []))
        token = resp.get("nextPageToken")
        if not token:
            break
    extra = [
        f"{r['type']} {r['name']}" for r in records
        if not (r["type"] in ("NS", "SOA") and r["name"] == zone["dnsName"])
    ]
    if extra:
        return f"retained: zone still holds {', '.join(extra[:5])}"
    try:
        call_with_retries(
            lambda: dns.managedZones().delete(project=project, managedZone=name).execute(),
            description=f"delete DNS zone {name}",
        )
    except Exception as exc:
        if is_not_found(exc):
            return "absent"
        raise
    return "deleted"


def ensure_host_records(
    dns: Resource, project: str, domain: str, ip: str, *,
    zone_name_for_new: str, allow_create_zone: bool,
    log=lambda msg: None,
) -> list[str]:
    """Point ``domain`` (and ``www.`` for apex domains) at ``ip``.

    Uses the most specific existing zone that contains the domain; for an
    apex domain without one, creates a dedicated zone when allowed. Returns
    the zone's nameservers when a new zone was created (the user must set
    them at their registrar), otherwise an empty list.
    """
    apex = is_apex_domain(domain)
    zone = find_zone_for(dns, project, domain)
    created = False
    if zone is None:
        if not allow_create_zone:
            log(f"No Cloud DNS zone contains {domain} — point its DNS at {ip} manually")
            return []
        zone = create_zone(dns, project, zone_name_for_new, domain)
        created = True
        log(f"Created DNS zone {zone['name']} for {domain}")

    if not created and ZONE_MARKER not in zone.get("description", ""):
        # Someone else's zone: never silently re-point a name that already
        # serves something (another site, a legacy LB, a mail host...).
        # Deleting the website later would then remove that record too.
        # (name, type) -> the only value we accept to find there already;
        # None means any record of that type is in the way.
        allowed: dict[tuple[str, str], Optional[list[str]]] = {
            (domain, "A"): [ip],
            (domain, "CNAME"): None,
        }
        if apex:
            allowed[(f"www.{domain}", "CNAME")] = [fqdn(domain)]
            allowed[(f"www.{domain}", "A")] = None
        for (name, rtype), ok_value in allowed.items():
            current = get_record(dns, project, zone["name"], name, rtype)
            if current and current.get("rrdatas") != ok_value:
                raise RuntimeError(
                    f"{name} already has a {rtype} record ({', '.join(current.get('rrdatas', []))}) "
                    f"in zone {zone['name']}. Remove or re-point it to {ip} first, then redeploy."
                )

    ensure_record(dns, project, zone["name"], domain, "A", [ip])
    if apex:
        ensure_record(dns, project, zone["name"], f"www.{domain}", "CNAME", [fqdn(domain)])
    log(f"DNS {domain}{' (+www)' if apex else ''} -> {ip} in zone {zone['name']}")

    if created:
        fresh = call_with_retries(
            lambda: dns.managedZones().get(project=project, managedZone=zone["name"]).execute(),
            description="read zone nameservers",
        )
        return fresh.get("nameServers", [])
    return []


def cleanup_host_records(
    dns: Resource, project: str, domain: str, expected_ips: list[str],
) -> list[str]:
    """Undo :func:`ensure_host_records` for ``domain``; returns outcome lines."""
    zone = find_zone_for(dns, project, domain)
    if zone is None:
        return [f"DNS {domain}: absent (no zone in project)"]
    outcomes = [
        f"DNS A {domain}: "
        + delete_record_if(dns, project, zone["name"], domain, "A", expected_ips)
    ]
    own_zone = zone.get("dnsName") == fqdn(domain)
    # Apex domains get a www CNAME; so did subdomains for which the legacy
    # deployer created a standalone zone.
    if is_apex_domain(domain) or own_zone:
        outcomes.append(
            f"DNS CNAME www.{domain}: "
            + delete_record_if(dns, project, zone["name"], f"www.{domain}", "CNAME", [domain])
        )
    if own_zone:
        outcomes.append(f"DNS zone {zone['name']}: " + delete_zone_if_unused(dns, project, zone))
    return outcomes
