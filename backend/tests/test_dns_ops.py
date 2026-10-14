from infra import dns_ops


class _Req:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class FakeDns:
    """Just enough of the Cloud DNS client for dns_ops."""

    def __init__(self, zones, records):
        self.zones = zones            # list of zone dicts
        self.records = records        # {(zone, name, type): rrset}
        self.deleted_zones = []

    def managedZones(self):
        dns = self

        class Z:
            def list(self, project, pageToken=None):
                return _Req(lambda: {"managedZones": dns.zones})

            def delete(self, project, managedZone):
                def _d():
                    dns.deleted_zones.append(managedZone)
                    return {}
                return _Req(_d)
        return Z()

    def resourceRecordSets(self):
        dns = self

        class R:
            def list(self, project, managedZone, name=None, type=None, pageToken=None):
                return _Req(lambda: {"rrsets": [
                    rr for (z, n, t), rr in dns.records.items()
                    if z == managedZone and (name is None or n == name) and (type is None or t == type)
                ]})
        return R()

    def changes(self):
        dns = self

        class C:
            def create(self, project, managedZone, body):
                def _c():
                    for rr in body.get("deletions", []):
                        dns.records.pop((managedZone, rr["name"], rr["type"]), None)
                    for rr in body.get("additions", []):
                        dns.records[(managedZone, rr["name"], rr["type"])] = rr
                    return {}
                return _Req(_c)
        return C()


def rr(name, rtype, data):
    return {"name": name, "type": rtype, "ttl": 300, "rrdatas": data}


def test_cleanup_deletes_our_records_and_our_empty_zone():
    zone = {"name": "shop-com-zone", "dnsName": "shop.com.", "description": "Managed zone for shop.com (WebDeploy)"}
    dns = FakeDns([zone], {
        ("shop-com-zone", "shop.com.", "A"): rr("shop.com.", "A", ["1.2.3.4"]),
        ("shop-com-zone", "www.shop.com.", "CNAME"): rr("www.shop.com.", "CNAME", ["shop.com."]),
        ("shop-com-zone", "shop.com.", "NS"): rr("shop.com.", "NS", ["ns1."]),
        ("shop-com-zone", "shop.com.", "SOA"): rr("shop.com.", "SOA", ["soa"]),
    })
    lines = dns_ops.cleanup_host_records(dns, "p", "shop.com", ["1.2.3.4"])
    assert all("deleted" in line for line in lines), lines
    assert dns.deleted_zones == ["shop-com-zone"]


def test_cleanup_keeps_repointed_record_and_zone_with_user_records():
    zone = {"name": "shop-com-zone", "dnsName": "shop.com.", "description": "Managed zone for shop.com (WebDeploy)"}
    dns = FakeDns([zone], {
        ("shop-com-zone", "shop.com.", "A"): rr("shop.com.", "A", ["9.9.9.9"]),   # user moved it
        ("shop-com-zone", "shop.com.", "MX"): rr("shop.com.", "MX", ["10 mail."]),
    })
    lines = dns_ops.cleanup_host_records(dns, "p", "shop.com", ["1.2.3.4"])
    assert "retained" in lines[0]
    assert ("shop-com-zone", "shop.com.", "A") in dns.records
    assert dns.deleted_zones == []


def test_cleanup_subdomain_only_touches_its_record_in_parent_zone():
    zone = {"name": "example-com", "dnsName": "example.com.", "description": "hand made"}
    dns = FakeDns([zone], {
        ("example-com", "app.example.com.", "A"): rr("app.example.com.", "A", ["1.2.3.4"]),
        ("example-com", "example.com.", "A"): rr("example.com.", "A", ["1.2.3.4"]),
    })
    dns_ops.cleanup_host_records(dns, "p", "app.example.com", ["1.2.3.4"])
    assert ("example-com", "app.example.com.", "A") not in dns.records
    assert ("example-com", "example.com.", "A") in dns.records
    assert dns.deleted_zones == []


def test_find_zone_prefers_most_specific():
    dns = FakeDns([
        {"name": "a", "dnsName": "example.com."},
        {"name": "b", "dnsName": "eu.example.com."},
    ], {})
    assert dns_ops.find_zone_for(dns, "p", "shop.eu.example.com")["name"] == "b"
    assert dns_ops.find_zone_for(dns, "p", "notexample.com") is None
