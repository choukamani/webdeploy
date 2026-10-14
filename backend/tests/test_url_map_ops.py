import copy

import pytest

from infra import url_map_ops as ops

LINK = "https://www.googleapis.com/compute/v1/projects/p/global/backendBuckets/{}"
SVC = "https://www.googleapis.com/compute/v1/projects/p/global/backendServices/{}"


def demo_map():
    return {
        "defaultService": LINK.format("default"),
        "hostRules": [{"hosts": ["demo.example.com"], "pathMatcher": "pm"}],
        "pathMatchers": [{
            "name": "pm",
            "defaultService": LINK.format("default"),
            "pathRules": [
                {"paths": ["/a", "/a/*"], "service": LINK.format("demo-a-backend-demo")},
                {"paths": ["/b", "/b/*"], "service": LINK.format("demo-b-backend-demo")},
                {"paths": ["/manual/*", "/c/*"], "service": LINK.format("hand-made")},
            ],
        }],
    }


def test_service_key_normalises_links_and_names():
    assert ops.service_key(LINK.format("x")) == "backendBuckets/x"
    assert ops.service_key("global/backendBuckets/x") == "backendBuckets/x"
    assert ops.bucket_key("x") == "backendBuckets/x"


def test_remove_path_rules_removes_only_owned_rule():
    m = demo_map()
    change = ops.remove_path_rules(m, host="demo.example.com", paths=["/a", "/a/*"],
                                   owned_services={ops.bucket_key("demo-a-backend-demo")})
    assert change.changed
    paths = [p for r in m["pathMatchers"][0]["pathRules"] for p in r["paths"]]
    assert "/a" not in paths and "/b/*" in paths and "/manual/*" in paths


def test_remove_path_rules_keeps_paths_routed_to_foreign_backend():
    m = demo_map()
    change = ops.remove_path_rules(m, host="demo.example.com", paths=["/c", "/c/*"],
                                   owned_services={ops.bucket_key("demo-c-backend-demo")})
    assert not change.changed
    assert change.retained and "hand-made" in change.retained[0]
    assert m == demo_map()


def test_remove_path_rules_is_idempotent():
    m = demo_map()
    owned = {ops.bucket_key("demo-a-backend-demo")}
    ops.remove_path_rules(m, host="demo.example.com", paths=["/a", "/a/*"], owned_services=owned)
    again = ops.remove_path_rules(m, host="demo.example.com", paths=["/a", "/a/*"], owned_services=owned)
    assert not again.changed and not again.retained


def test_add_host_route_creates_then_is_noop():
    m = demo_map()
    assert ops.add_host_route(m, hosts=["shop.com", "www.shop.com"], matcher_name="wd-shop-com",
                              service_link=LINK.format("wd-shop-com"))
    snapshot = copy.deepcopy(m)
    assert not ops.add_host_route(m, hosts=["shop.com", "www.shop.com"], matcher_name="wd-shop-com",
                                  service_link=LINK.format("wd-shop-com"))
    assert m == snapshot


def test_add_host_route_refuses_to_steal_a_host():
    m = demo_map()
    with pytest.raises(ops.RouteConflictError):
        ops.add_host_route(m, hosts=["demo.example.com"], matcher_name="wd-x",
                           service_link=LINK.format("wd-x"))


def test_remove_host_route_round_trip_restores_map():
    m = demo_map()
    original = copy.deepcopy(m)
    ops.add_host_route(m, hosts=["app.example.com"], matcher_name="wd-app",
                       service_link=SVC.format("wd-app"))
    change = ops.remove_host_route(m, hosts=["app.example.com"],
                                   owned_services={ops.backend_service_key("wd-app")})
    assert change.changed and not change.retained
    assert m == original


def test_remove_host_route_retains_host_pointing_elsewhere():
    m = demo_map()
    ops.add_host_route(m, hosts=["shop.com"], matcher_name="other",
                       service_link=LINK.format("someone-elses"))
    change = ops.remove_host_route(m, hosts=["shop.com"],
                                   owned_services={ops.bucket_key("wd-shop-com")})
    assert not change.changed
    assert change.retained_hosts == {"shop.com"}
    assert ops.find_matcher_for_host(m, "shop.com") is not None


def test_remove_host_route_keeps_other_hosts_of_a_shared_rule():
    m = demo_map()
    m["hostRules"].append({"hosts": ["shop.com", "www.shop.com", "promo.shop.com"], "pathMatcher": "pm-shop"})
    m["pathMatchers"].append({"name": "pm-shop", "defaultService": LINK.format("shop-backend-prod")})
    change = ops.remove_host_route(m, hosts=["shop.com", "www.shop.com"],
                                   owned_services={ops.bucket_key("shop-backend-prod")})
    assert change.changed
    rule = next(r for r in m["hostRules"] if r["pathMatcher"] == "pm-shop")
    assert rule["hosts"] == ["promo.shop.com"]
    assert any(pm["name"] == "pm-shop" for pm in m["pathMatchers"])  # still referenced


def test_has_path_rule_checks_service():
    m = demo_map()
    assert ops.has_path_rule(m, host="demo.example.com", path="/a/*",
                             service=ops.bucket_key("demo-a-backend-demo"))
    assert not ops.has_path_rule(m, host="demo.example.com", path="/c/*",
                                 service=ops.bucket_key("demo-c-backend-demo"))


def test_estimate_size_grows_with_rules():
    small = ops.estimate_size(demo_map())
    m = demo_map()
    for i in range(100):
        ops.add_host_route(m, hosts=[f"s{i}.com"], matcher_name=f"wd-s{i}", service_link=LINK.format(f"wd-s{i}"))
    # ~100 bytes per host route (host rule + matcher with a full self-link)
    assert ops.estimate_size(m) > small + 100 * 80
