import pytest

from config import Settings
from infra import layout, teardown
from infra.layout import Placement, WebsiteRef


@pytest.fixture
def s():
    return Settings(PROJECT_ID="proj", _env_file=None)


def ref(mode, name, domain="", placement=Placement.SITES):
    return WebsiteRef(layout.website_key(mode, name, domain, Settings(_env_file=None)), mode, name, domain, placement)


# ── keys & names ─────────────────────────────────────────────────────────

def test_website_key_groups_versions_and_prod_by_domain(s):
    assert layout.website_key("demo", "shop", None, s) == "demo:shop"
    # two prod deployments of one domain under different names are one website
    assert layout.website_key("prod", "clientshop", "ClientShop.com", s) == \
        layout.website_key("prod", "clientshp", "https://clientshop.com/", s) == "prod:clientshop.com"


def test_demo_prefix_rejects_anything_but_a_slug():
    assert layout.demo_prefix("my-site") == "my-site/"
    for bad in ["", "-x", "a/b", "..", "A", "x" * 64, "a b"]:
        with pytest.raises(ValueError):
            layout.demo_prefix(bad)


def test_names_fit_gcp_limits(s):
    p = layout.prod_names("a-very-long-subdomain-name-for-testing.some-long-domain-name.co.uk", s)
    for name in (p.bucket, p.backend_bucket, p.matcher, p.cert, *p.cert_map_entries.values()):
        assert len(name) <= 63
    assert layout.app_names("x" * 63, s).service.__len__() <= 49


def test_apex_detection_handles_two_label_suffixes(s):
    assert layout.prod_names("shop.co.uk", s).hosts == ["shop.co.uk", "www.shop.co.uk"]
    assert layout.prod_names("blog.shop.com", s).hosts == ["blog.shop.com"]


def test_public_urls(s):
    assert layout.public_url(ref("demo", "x", placement=Placement.LEGACY_DEMO), s) == f"https://{s.DEMO_DOMAIN}/x/"
    assert layout.public_url(ref("demo", "x"), s) == f"https://{s.SITES_DEMO_HOST}/x/"
    assert layout.public_url(ref("cloudrun", "api"), s) == f"https://api.{s.SITES_APPS_DOMAIN}/"


# ── plans ────────────────────────────────────────────────────────────────

def actions(plan):
    return [st.action for st in plan]


def test_sites_demo_plan_only_touches_its_prefix(s):
    plan = teardown.plan_teardown(ref("demo", "shop"), s)
    assert actions(plan) == ["gcs.delete_prefix", "cdn.invalidate"]
    assert plan[0].params == {"bucket": s.sites_demo_bucket_name, "prefix": "shop/"}
    assert not plan[1].fatal


def test_legacy_demo_plan_orders_routing_before_backends_before_storage(s):
    plan = teardown.plan_teardown(ref("demo", "shop", placement=Placement.LEGACY_DEMO), s)
    assert actions(plan)[:3] == ["url_map.remove_path_rules", "backend_bucket.delete", "gcs.delete_bucket"]
    assert plan[0].params["url_map"] == s.DEMO_URL_MAP_NAME
    assert plan[0].params["owned"] == ["backendBuckets/demo-shop-backend-demo"]


def test_prod_plans_cover_every_resource_created(s):
    sites = actions(teardown.plan_teardown(ref("prod", "x", "shop.com"), s))
    assert sites == [
        "url_map.remove_host_route", "certmap.delete_entry", "certmap.delete_entry",
        "certmanager.delete_cert", "backend_bucket.delete", "gcs.delete_bucket", "dns.cleanup_host",
    ]
    legacy = actions(teardown.plan_teardown(ref("prod", "x", "shop.com", Placement.LEGACY_PROD), s))
    assert legacy == [
        "url_map.remove_host_route", "classic_cert.detach_and_delete",
        "backend_bucket.delete", "gcs.delete_bucket", "dns.cleanup_host",
    ]


def test_cloudrun_plan(s):
    plan = teardown.plan_teardown(ref("cloudrun", "api"), s)
    assert actions(plan) == [
        "url_map.remove_host_route", "backend_service.delete", "neg.delete",
        "dns.cleanup_host", "run.delete_service", "ar.delete_package",
    ]


# ── executor ─────────────────────────────────────────────────────────────

class FakeOps:
    """Records calls; fails a step a given number of times."""

    def __init__(self, fail=None, outcomes=None):
        self.fail = dict(fail or {})
        self.outcomes = outcomes or {}
        self.calls = []
        self.deleted = set()

    def run(self, step):
        self.calls.append(step.id)
        if self.fail.get(step.id, 0) > 0:
            self.fail[step.id] -= 1
            raise RuntimeError(f"boom in {step.id}")
        if step.id in self.outcomes:
            return self.outcomes[step.id]
        if step.id in self.deleted:
            return teardown.OpOutcome("absent")
        self.deleted.add(step.id)
        return teardown.OpOutcome("deleted")


def test_partial_failure_stops_then_retry_resumes(s):
    plan = teardown.plan_teardown(ref("prod", "x", "shop.com"), s)
    ops = FakeOps(fail={"bucket": 1})

    first = teardown.TeardownExecutor(ops).run(plan)
    assert not first.ok and first.failed_step == "bucket"
    assert "dns" not in ops.calls  # nothing after the failure ran

    second = teardown.TeardownExecutor(ops).run(plan)
    assert second.ok
    by_id = {r.id: r.outcome for r in second.results}
    assert by_id["routing"] == "absent"   # finished last time — falls through
    assert by_id["bucket"] == "deleted"
    assert by_id["dns"] == "deleted"


def test_non_fatal_failure_does_not_stop_teardown(s):
    plan = teardown.plan_teardown(ref("demo", "shop"), s)
    report = teardown.TeardownExecutor(FakeOps(fail={"cdn": 1})).run(plan)
    assert report.ok
    assert report.results[-1].outcome.startswith("skipped")


def test_retained_routing_protects_host_scoped_steps(s):
    plan = teardown.plan_teardown(ref("prod", "x", "shop.com"), s)
    ops = FakeOps(outcomes={
        "routing": teardown.OpOutcome("retained: someone else's", {"shop.com", "www.shop.com"}),
    })
    report = teardown.TeardownExecutor(ops).run(plan)
    assert report.ok
    by_id = {r.id: r.outcome for r in report.results}
    assert by_id["cert"].startswith("retained") and by_id["dns"].startswith("retained")
    assert "cert" not in ops.calls and "dns" not in ops.calls
    assert by_id["bucket"] == "deleted"  # unshared resources still go


def test_precomputed_routing_outcomes_are_not_rerun(s):
    plan = teardown.plan_teardown(ref("demo", "shop", placement=Placement.LEGACY_DEMO), s)
    ops = FakeOps()
    report = teardown.TeardownExecutor(ops).run(plan, done={"routing": teardown.OpOutcome("deleted")})
    assert report.ok and "routing" not in ops.calls
