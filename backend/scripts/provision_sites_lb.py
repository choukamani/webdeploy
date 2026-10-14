"""
Provision the dedicated sites load balancer (idempotent — safe to re-run).

Creates, if missing: global IP, shared demo bucket + backend bucket, HTTPS
URL map, HTTP→HTTPS redirect, DNS-authorized wildcard certificate in a
certificate map, proxies, forwarding rules, and the demo hostname's DNS
record. Names and hostnames come from the same settings the backend uses
(``SITES_*`` in config.py), so the script and the app cannot disagree.

Usage (from backend/)::

    python -m scripts.provision_sites_lb              # Application Default Credentials
    python -m scripts.provision_sites_lb --gcloud     # your `gcloud auth` user token
    python -m scripts.provision_sites_lb --status     # print routing capacity only
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import get_settings  # noqa: E402
from infra.sites_lb import SitesLB  # noqa: E402


def _gcloud_credentials():
    from google.oauth2.credentials import Credentials

    token = subprocess.check_output(["gcloud", "auth", "print-access-token"], text=True).strip()
    return Credentials(token=token)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gcloud", action="store_true", help="authenticate with the gcloud CLI user token")
    parser.add_argument("--status", action="store_true", help="only report URL map capacity")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    lb = SitesLB(
        get_settings(),
        log=lambda m: print(f"  · {m}", flush=True),
        credentials=_gcloud_credentials() if args.gcloud else None,
    )
    result = lb.status() if args.status else lb.provision()
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
