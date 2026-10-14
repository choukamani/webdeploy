"""Statistics queries — date-range Firestore queries and in-memory aggregation.

All statistics computation is done server-side in Python because Firestore
has no GROUP BY / COUNT / SUM aggregation.  For a deployment platform the
volume is manageable (hundreds to low thousands of documents per month).
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from google.cloud.firestore_v1 import Client as FirestoreClient

_DEPLOYMENTS = "deployments"

# Day-name mapping for French labels
_DAY_NAMES_FR = {
    0: "Lundi",
    1: "Mardi",
    2: "Mercredi",
    3: "Jeudi",
    4: "Vendredi",
    5: "Samedi",
    6: "Dimanche",
}


def query_deployments_in_range(
    db: FirestoreClient,
    start_utc: datetime,
    end_utc: datetime,
) -> list[dict]:
    """
    Fetch all deployment documents where ``created_at`` falls within
    [start_utc, end_utc).  Returns raw dicts with ``id`` injected.

    Uses the same Firestore query pattern as the daily report service,
    so the composite index on ``created_at`` should already exist.
    """
    query = (
        db.collection(_DEPLOYMENTS)
        .where("created_at", ">=", start_utc)
        .where("created_at", "<", end_utc)
    )
    results = []
    for doc in query.stream():
        data = doc.to_dict()
        data["id"] = doc.id
        results.append(data)
    return results


def compute_statistics(deployments: list[dict]) -> dict:
    """
    Given a list of deployment dicts, compute comprehensive statistics:

    - total_count, with_ai_count, without_ai_count
    - per_deployer breakdown (grouped by deployer_email)
    - daily_breakdown (grouped by date, with day name)
    - average_per_day
    - per_status breakdown
    - ai_tokens totals and estimated cost

    All groupings split into with_ai and without_ai.
    """
    total = len(deployments)
    with_ai = [d for d in deployments if d.get("ai_enabled", False)]
    without_ai = [d for d in deployments if not d.get("ai_enabled", False)]

    # ── Per deployer ────────────────────────────────────────────────
    deployer_map: dict[str, dict] = defaultdict(lambda: {
        "first_name": "",
        "last_name": "",
        "email": "",
        "total": 0,
        "with_ai": 0,
        "without_ai": 0,
        "websites": set(),
        "modes": set(),
        "input_tokens": 0,
        "output_tokens": 0,
    })

    for dep in deployments:
        email = dep.get("deployer_email", "") or "unknown"
        info = deployer_map[email]
        info["first_name"] = dep.get("deployer_first_name", "") or info["first_name"]
        info["last_name"] = dep.get("deployer_last_name", "") or info["last_name"]
        info["email"] = email
        info["total"] += 1
        if dep.get("ai_enabled", False):
            info["with_ai"] += 1
        else:
            info["without_ai"] += 1
        info["websites"].add(dep.get("website_name", ""))
        info["modes"].add(dep.get("mode", ""))

        # Accumulate token usage per deployer
        raw_usage = dep.get("ai_token_usage")
        if raw_usage:
            try:
                usage = json.loads(raw_usage) if isinstance(raw_usage, str) else raw_usage
                info["input_tokens"] += usage.get("input_tokens", 0)
                info["output_tokens"] += usage.get("output_tokens", 0)
            except (json.JSONDecodeError, TypeError):
                pass

    deployers_list = []
    for email, info in deployer_map.items():
        ai_cost = (info["input_tokens"] * 3 / 1_000_000) + (info["output_tokens"] * 15 / 1_000_000)
        deployers_list.append({
            "first_name": info["first_name"],
            "last_name": info["last_name"],
            "name": f"{info['first_name']} {info['last_name']}".strip() or email,
            "email": info["email"],
            "total": info["total"],
            "with_ai": info["with_ai"],
            "without_ai": info["without_ai"],
            "websites": sorted(info["websites"]),
            "modes": sorted(info["modes"]),
            "ai_cost": f"${ai_cost:.4f}" if ai_cost > 0 else "$0.00",
        })

    # Sort deployers by total descending
    deployers_list.sort(key=lambda d: d["total"], reverse=True)

    # ── Per day ─────────────────────────────────────────────────────
    day_map: dict[str, dict] = defaultdict(lambda: {"total": 0, "with_ai": 0, "without_ai": 0})

    for dep in deployments:
        created = dep.get("created_at")
        if created is None:
            continue
        if hasattr(created, "date"):
            day_str = created.date().isoformat()
            weekday = created.weekday()
        elif hasattr(created, "strftime"):
            day_str = created.strftime("%Y-%m-%d")
            weekday = created.weekday()
        else:
            continue

        day_map[day_str]["total"] += 1
        day_map[day_str]["weekday"] = weekday
        if dep.get("ai_enabled", False):
            day_map[day_str]["with_ai"] += 1
        else:
            day_map[day_str]["without_ai"] += 1

    daily_breakdown = []
    for day_str in sorted(day_map.keys()):
        counts = day_map[day_str]
        weekday = counts.get("weekday", 0)
        daily_breakdown.append({
            "date": day_str,
            "day_name": _DAY_NAMES_FR.get(weekday, ""),
            "total": counts["total"],
            "with_ai": counts["with_ai"],
            "without_ai": counts["without_ai"],
        })

    # ── Average per day ─────────────────────────────────────────────
    num_days = len(day_map) if day_map else 1
    avg_per_day = round(total / num_days, 2)

    # ── Per status ──────────────────────────────────────────────────
    status_map: dict[str, int] = defaultdict(int)
    for dep in deployments:
        status_map[dep.get("status", "unknown")] += 1

    # ── AI token totals ─────────────────────────────────────────────
    total_input = 0
    total_output = 0
    for dep in with_ai:
        raw = dep.get("ai_token_usage")
        if raw:
            try:
                usage = json.loads(raw) if isinstance(raw, str) else raw
                total_input += usage.get("input_tokens", 0)
                total_output += usage.get("output_tokens", 0)
            except (json.JSONDecodeError, TypeError):
                pass
    ai_cost_total = (total_input * 3 / 1_000_000) + (total_output * 15 / 1_000_000)

    return {
        "total_count": total,
        "with_ai_count": len(with_ai),
        "without_ai_count": len(without_ai),
        "average_per_day": avg_per_day,
        "per_deployer": deployers_list,
        "daily_breakdown": daily_breakdown,
        "per_status": dict(status_map),
        "ai_tokens": {
            "input_tokens": total_input,
            "output_tokens": total_output,
            "estimated_cost_usd": round(ai_cost_total, 4),
        },
    }
