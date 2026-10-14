"""
Pure, side-effect-free edits of a URL map body (the dict GCP returns).

Kept free of I/O so the ownership rules — the part that decides whether a
delete may touch a routing rule at all — are unit-testable. Callers fetch the
map, apply one of these functions inside ``gcp_helpers.mutate_url_map``, and
patch the result back.

Ownership model: a website owns the backend(s) it created. A rule is removed
only when *every* backend it routes to is owned by the website being deleted;
anything pointing elsewhere was made by someone else (by hand, or by another
website) and is left alone and reported as retained.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Iterable


def service_key(link: str) -> str:
    """Normalise a backend link to ``"backendBuckets/name"`` form.

    URL maps store full self-links; callers usually know only the type and
    name. Comparing on the last two path segments makes both forms equal.
    """
    parts = [p for p in (link or "").split("/") if p]
    return "/".join(parts[-2:]) if len(parts) >= 2 else (link or "")


def bucket_key(name: str) -> str:
    return f"backendBuckets/{name}"


def backend_service_key(name: str) -> str:
    return f"backendServices/{name}"


# Stand-in for "this matcher redirects": a redirect is something someone
# configured on purpose, and no website owns it, so it is never removed.
REDIRECT = "<redirect>"


def _action_services(holder: dict, action_key: str) -> set[str]:
    out = set()
    for backend in (holder.get(action_key) or {}).get("weightedBackendServices", []):
        out.add(service_key(backend.get("backendService", "")))
    return out


def _matcher_services(matcher: dict) -> set[str]:
    """Every backend a path matcher can send traffic to (plus REDIRECT)."""
    services = set()
    if matcher.get("defaultService"):
        services.add(service_key(matcher["defaultService"]))
    services |= _action_services(matcher, "defaultRouteAction")
    if matcher.get("defaultUrlRedirect"):
        services.add(REDIRECT)
    for rule in matcher.get("pathRules", []) + matcher.get("routeRules", []):
        if rule.get("service"):
            services.add(service_key(rule["service"]))
        services |= _action_services(rule, "routeAction")
        if rule.get("urlRedirect"):
            services.add(REDIRECT)
    return services


def _owned(services: set[str], owned: set[str]) -> bool:
    """A matcher belongs to the website only if it provably routes to the
    website's backends and nowhere else — one with nothing we recognise is
    someone's hand-made config, not ours."""
    return bool(services) and services <= owned


@dataclass
class RouteChange:
    """What an edit did. ``retained`` holds human-readable reasons for rules
    that matched the website but were not removed because it does not own them."""
    changed: bool = False
    removed: list[str] = field(default_factory=list)
    retained: list[str] = field(default_factory=list)
    retained_hosts: set[str] = field(default_factory=set)


class RouteConflictError(RuntimeError):
    """A hostname is already routed to a backend owned by something else."""


# ─── Host routes (one hostRule + one pathMatcher per website) ───────────

def find_matcher_for_host(url_map: dict, host: str) -> dict | None:
    for rule in url_map.get("hostRules", []):
        if host in rule.get("hosts", []):
            name = rule.get("pathMatcher")
            for matcher in url_map.get("pathMatchers", []):
                if matcher.get("name") == name:
                    return matcher
    return None


def add_host_route(
    url_map: dict, *, hosts: list[str], matcher_name: str, service_link: str,
) -> bool:
    """Route ``hosts`` to ``service_link`` through a dedicated path matcher.

    Idempotent: returns False when the map already routes exactly this way.
    Raises :class:`RouteConflictError` if a host is already routed by a
    different matcher — another website (or a hand-made rule) owns it, and
    silently re-pointing it would take that site down.
    """
    host_rules = url_map.setdefault("hostRules", [])
    matchers = url_map.setdefault("pathMatchers", [])
    wanted = service_key(service_link)

    for rule in host_rules:
        clash = [h for h in rule.get("hosts", []) if h in hosts]
        if clash and rule.get("pathMatcher") != matcher_name:
            raise RouteConflictError(
                f"Host(s) {', '.join(clash)} already routed by path matcher "
                f"'{rule.get('pathMatcher')}' — refusing to take them over."
            )

    matcher = next((m for m in matchers if m.get("name") == matcher_name), None)
    rule = next((r for r in host_rules if r.get("pathMatcher") == matcher_name), None)
    if rule is not None and sorted(rule.get("hosts", [])) != sorted(hosts):
        raise RouteConflictError(
            f"Path matcher '{matcher_name}' already serves {', '.join(rule.get('hosts', []))} — "
            "refusing to re-point it."
        )
    if matcher is not None and not _owned(_matcher_services(matcher), {wanted}):
        raise RouteConflictError(
            f"Path matcher '{matcher_name}' routes to something else — refusing to re-point it."
        )

    if (
        matcher is not None and rule is not None
        and sorted(rule.get("hosts", [])) == sorted(hosts)
        and _matcher_services(matcher) == {wanted}
    ):
        return False

    if matcher is None:
        matchers.append({"name": matcher_name, "defaultService": service_link})
    else:
        matcher.clear()
        matcher.update({"name": matcher_name, "defaultService": service_link})

    if rule is None:
        host_rules.append({"hosts": list(hosts), "pathMatcher": matcher_name})
    else:
        rule["hosts"] = list(hosts)
    return True


def remove_host_route(
    url_map: dict, *, hosts: Iterable[str], owned_services: set[str],
) -> RouteChange:
    """Stop routing ``hosts``, removing only rules the website owns.

    A host is released when the matcher it points to sends traffic solely to
    ``owned_services``. Released hosts are dropped from their host rule; a
    rule left with no hosts is deleted, and a matcher no longer referenced by
    any rule is deleted too (its backends are all owned, so nothing else
    depends on it).
    """
    hosts = set(hosts)
    owned = {service_key(s) for s in owned_services}
    change = RouteChange()
    matchers = {m.get("name"): m for m in url_map.get("pathMatchers", [])}

    kept_rules = []
    for rule in url_map.get("hostRules", []):
        ours = [h for h in rule.get("hosts", []) if h in hosts]
        if not ours:
            kept_rules.append(rule)
            continue
        matcher = matchers.get(rule.get("pathMatcher"))
        services = _matcher_services(matcher) if matcher else set()
        if matcher is not None and not _owned(services, owned):
            change.retained.append(
                f"{', '.join(ours)} routes to "
                f"{', '.join(sorted(services - owned)) or 'a rule with no backend'}, "
                "which this website did not create"
            )
            change.retained_hosts.update(ours)
            kept_rules.append(rule)
            continue
        remaining = [h for h in rule.get("hosts", []) if h not in hosts]
        change.changed = True
        change.removed.append(f"host rule {', '.join(ours)}")
        if remaining:
            kept_rules.append({**rule, "hosts": remaining})
    url_map["hostRules"] = kept_rules

    referenced = {r.get("pathMatcher") for r in kept_rules}
    kept_matchers = []
    for name, matcher in matchers.items():
        if name not in referenced and _owned(_matcher_services(matcher), owned):
            change.changed = True
            change.removed.append(f"path matcher {name}")
            continue
        kept_matchers.append(matcher)
    url_map["pathMatchers"] = kept_matchers
    return change


# ─── Path routes (legacy demo LB: /{site} and /{site}/* under one host) ──

def remove_path_rules(
    url_map: dict, *, host: str, paths: Iterable[str], owned_services: set[str],
) -> RouteChange:
    """Drop ``paths`` from the matcher serving ``host`` where they route to an
    owned backend. Paths routed elsewhere are kept and reported."""
    paths = set(paths)
    owned = {service_key(s) for s in owned_services}
    change = RouteChange()
    matcher = find_matcher_for_host(url_map, host)
    if matcher is None:
        return change

    kept = []
    for rule in matcher.get("pathRules", []):
        ours = [p for p in rule.get("paths", []) if p in paths]
        if not ours:
            kept.append(rule)
            continue
        if service_key(rule.get("service", "")) not in owned:
            change.retained.append(
                f"{', '.join(ours)} routes to {service_key(rule.get('service', ''))}, "
                "which this website did not create"
            )
            kept.append(rule)
            continue
        remaining = [p for p in rule.get("paths", []) if p not in paths]
        change.changed = True
        change.removed.append(f"path rule {', '.join(ours)}")
        if remaining:
            kept.append({**rule, "paths": remaining})
    matcher["pathRules"] = kept
    return change


def has_path_rule(url_map: dict, *, host: str, path: str, service: str | None = None) -> bool:
    matcher = find_matcher_for_host(url_map, host)
    if matcher is None:
        return False
    for rule in matcher.get("pathRules", []):
        if path in rule.get("paths", []):
            return service is None or service_key(rule.get("service", "")) == service_key(service)
    return False


# ─── Capacity ───────────────────────────────────────────────────────────

# GCP rejects a URL map whose routing config exceeds 64 kB. Its measured size
# is not the JSON we receive, so this is an approximation for monitoring,
# calibrated against legacy-demo-lb: 107,329 bytes of compact routing JSON were
# measured by GCP at 64.09 kB.
URL_MAP_LIMIT_BYTES = 64 * 1024
_MEASURED_TO_JSON_RATIO = (64.09 * 1024) / 107_329


def estimate_size(url_map: dict) -> int:
    routing = {
        k: url_map.get(k)
        for k in ("defaultService", "defaultUrlRedirect", "hostRules", "pathMatchers", "tests")
        if url_map.get(k)
    }
    return int(len(json.dumps(routing, separators=(",", ":"))) * _MEASURED_TO_JSON_RATIO)
