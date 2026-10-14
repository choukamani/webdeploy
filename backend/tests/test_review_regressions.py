"""Regression tests for ownership/safety issues found in review."""
import pytest

from config import Settings
from infra import dns_ops, layout, teardown, url_map_ops as ops
from services.upload_service import UploadService
from tests.test_dns_ops import FakeDns, rr

LINK = "https://www.googleapis.com/compute/v1/projects/p/global/backendBuckets/{}"


@pytest.fixture
def s():
    return Settings(PROJECT_ID="proj", _env_file=None)


def test_lossy_safe_name_cannot_make_two_websites_share_resources(s):
    a, b = layout.prod_names("my-shop.com", s), layout.prod_names("my.shop.com", s)
    for field in ("bucket", "backend_bucket", "matcher", "cert"):
        assert getattr(a, field) != getattr(b, field)
    assert set(a.cert_map_entries.values()).isdisjoint(b.cert_map_entries.values())
    x, y = layout.app_names("my--app", s), layout.app_names("my-app", s)
    assert (x.neg, x.backend_service, x.matcher) != (y.neg, y.backend_service, y.matcher)


def test_add_host_route_refuses_to_repoint_existing_matcher():
    m = {"hostRules": [{"hosts": ["a.com"], "pathMatcher": "wd-x"}],
         "pathMatchers": [{"name": "wd-x", "defaultService": LINK.format("bb-a")}]}
    with pytest.raises(ops.RouteConflictError):
        ops.add_host_route(m, hosts=["b.com"], matcher_name="wd-x", service_link=LINK.format("bb-a"))
    with pytest.raises(ops.RouteConflictError):
        ops.add_host_route(m, hosts=["a.com"], matcher_name="wd-x", service_link=LINK.format("bb-b"))


@pytest.mark.parametrize("matcher", [
    {"name": "pm", "defaultUrlRedirect": {"hostRedirect": "shop.com"}},
    {"name": "pm", "defaultRouteAction": {"weightedBackendServices": [
        {"backendService": "https://x/global/backendServices/someone-else"}]}},
    {"name": "pm"},
])
def test_hand_made_matchers_are_never_treated_as_owned(matcher):
    m = {"hostRules": [{"hosts": ["www.shop.com"], "pathMatcher": "pm"}], "pathMatchers": [matcher]}
    change = ops.remove_host_route(m, hosts=["www.shop.com"], owned_services={ops.bucket_key("wd-shop")})
    assert not change.changed and change.retained_hosts == {"www.shop.com"}
    assert m["hostRules"] and m["pathMatchers"]


def test_prefix_delete_rejects_prefix_without_trailing_slash(s):
    ops_ = teardown.GcpOps(s, credentials=object())
    with pytest.raises(ValueError):
        ops_._gcs_delete_prefix(bucket="shared", prefix="my-site")  # would also match my-site-2/
    with pytest.raises(ValueError):
        ops_._gcs_delete_prefix(bucket="shared", prefix="")


def test_shared_bucket_upload_requires_prefix(tmp_path):
    (tmp_path / "index.html").write_text("x")
    with pytest.raises(ValueError):
        UploadService(settings=Settings(_env_file=None)).upload(
            str(tmp_path), "shared", prefix="", dedicated_bucket=False)


def test_prod_deploy_refuses_to_repoint_someone_elses_record():
    zone = {"name": "example-com", "dnsName": "example.com.", "description": "hand made"}
    dns = FakeDns([zone], {("example-com", "shop.example.com.", "A"): rr("shop.example.com.", "A", ["9.9.9.9"])})
    with pytest.raises(RuntimeError, match="already has"):
        dns_ops.ensure_host_records(dns, "p", "shop.example.com", "1.2.3.4",
                                    zone_name_for_new="x", allow_create_zone=True)
    assert dns.records[("example-com", "shop.example.com.", "A")]["rrdatas"] == ["9.9.9.9"]
    # Already pointing at us (a redeploy) is fine.
    dns.records[("example-com", "shop.example.com.", "A")]["rrdatas"] = ["1.2.3.4"]
    dns_ops.ensure_host_records(dns, "p", "shop.example.com", "1.2.3.4",
                                zone_name_for_new="x", allow_create_zone=True)


def test_cleanup_of_legacy_standalone_subdomain_zone_removes_www_and_zone():
    zone = {"name": "blog-x-com-zone", "dnsName": "blog.x.com.", "description": "Managed zone for blog.x.com (WebDeploy)"}
    dns = FakeDns([zone], {
        ("blog-x-com-zone", "blog.x.com.", "A"): rr("blog.x.com.", "A", ["1.2.3.4"]),
        ("blog-x-com-zone", "www.blog.x.com.", "CNAME"): rr("www.blog.x.com.", "CNAME", ["blog.x.com."]),
    })
    dns_ops.cleanup_host_records(dns, "p", "blog.x.com", ["1.2.3.4"])
    assert dns.deleted_zones == ["blog-x-com-zone"]
