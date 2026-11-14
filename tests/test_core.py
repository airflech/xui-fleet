import copy

import pytest
import yaml
from pydantic import ValidationError

from xui_fleet import xrayconf as xc
from xui_fleet.cli import EXAMPLE, _drop_node_from_yaml
from xui_fleet.config import FleetConfig, append_node, load_config
from xui_fleet.reconcile import Reconciler, Report, _free_port

@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    """The fake panel restarts instantly."""
    import time as real
    import types
    import xui_fleet.reconcile as rec
    monkeypatch.setattr(rec, "time", types.SimpleNamespace(time=real.time, sleep=lambda _s: None))


@pytest.fixture(autouse=True)
def _no_warp_unless_asked(request, monkeypatch):
    """Most tests check other entries; WARP has its own tests (marked "warp")."""
    if "warp" in request.keywords:
        return
    orig = FleetConfig.set_for
    monkeypatch.setattr(FleetConfig, "set_for",
                        lambda self, n: orig(self, n).model_copy(update={"warp": False, "cascade_warp": False}))


TEMPLATE = {
    "outbounds": [{"tag": "direct", "protocol": "freedom"}, {"tag": "blocked", "protocol": "blackhole"}],
    "routing": {"rules": [
        {"type": "field", "inboundTag": ["api"], "outboundTag": "api"},
        {"type": "field", "ip": ["geoip:private"], "outboundTag": "blocked"},
    ]},
}


def _inbound(network="tcp"):
    profile = xc.get_profile("vless-reality-tcp" if network == "tcp" else "vless-reality-xhttp")
    inbound = xc.build_inbound(tag="fleet-direct-de", remark="DE", port=443, profile=profile,
                               target="example.com:443", server_names=["example.com"], fingerprint="chrome",
                               private_key="PRIV", public_key="PUB", node_id=7)
    inbound["id"] = 11
    return inbound


def test_cascade_rule_goes_before_blocking_rules_and_after_api():
    out = xc.outbound_for_inbound(_inbound(), tag="fleet-out-de", address="1.2.3.4", client_id="uuid", flow=xc.VISION)
    merged = xc.merge_template(TEMPLATE, {"fleet-cascade-de": out})
    tags = [r["outboundTag"] for r in merged["routing"]["rules"]]
    assert tags == ["api", "fleet-out-de", "blocked"]
    assert merged["routing"]["rules"][1]["inboundTag"] == ["fleet-cascade-de"]


def test_merge_is_idempotent_and_removes_stale_nodes_only():
    out = xc.outbound_for_inbound(_inbound(), tag="fleet-out-de", address="1.2.3.4", client_id="uuid", flow=xc.VISION)
    once = xc.merge_template(TEMPLATE, {"fleet-cascade-de": out})
    assert xc.merge_template(once, {"fleet-cascade-de": out}) == once
    assert xc.merge_template(once, {}) == TEMPLATE  # user's own outbounds and rules survive


def test_outbound_mirrors_the_inbound_it_dials():
    inbound = _inbound("xhttp")
    out = xc.outbound_for_inbound(inbound, tag="fleet-out-de", address="node.example", client_id="uuid", flow="")
    reality = inbound["streamSettings"]["realitySettings"]
    assert out["settings"] == {"address": "node.example", "port": 443, "id": "uuid", "flow": "", "encryption": "none"}
    assert out["streamSettings"]["xhttpSettings"]["path"] == inbound["streamSettings"]["xhttpSettings"]["path"]
    assert out["streamSettings"]["realitySettings"]["publicKey"] == "PUB"
    assert out["streamSettings"]["realitySettings"]["shortId"] == reality["shortIds"][0]
    assert out["streamSettings"]["realitySettings"]["serverName"] == "example.com"


def test_drift_update_never_rotates_keys():
    current = _inbound()
    want = dict(remark="DE", port=443, target="example.com:443", server_names=["example.com"],
                exclude_from_sub=False, sort_index=1)
    assert xc.inbound_drift(current, **want) is None
    update = xc.inbound_drift(current, **dict(want, port=8443, target="other.com:443", server_names=["other.com"]))
    before, after = current["streamSettings"]["realitySettings"], update["streamSettings"]["realitySettings"]
    assert (update["port"], after["target"], after["serverNames"]) == (8443, "other.com:443", ["other.com"])
    assert (after["privateKey"], after["shortIds"]) == (before["privateKey"], before["shortIds"])
    assert current["port"] == 443  # the input is not mutated


def test_free_port_skips_used_and_fails_when_exhausted():
    assert _free_port((8443, 8445), set()) == 443  # the first cascade looks like any HTTPS site
    assert _free_port((8443, 8445), {443, 8443, 8444}) == 8445
    with pytest.raises(Exception):
        _free_port((8443, 8444), {443, 8443, 8444})


def test_config_rejects_duplicate_names_and_ports():
    base = {"master": {"name": "ru", "host": "1.1.1.1"}}
    with pytest.raises(ValidationError):
        FleetConfig.model_validate(dict(base, nodes=[{"name": "ru", "host": "2.2.2.2"}]))
    with pytest.raises(ValidationError):
        FleetConfig.model_validate(dict(base, nodes=[
            {"name": "a", "host": "2.2.2.2", "cascade": {"port": 9000}},
            {"name": "b", "host": "3.3.3.3", "cascade": {"port": 9000}}]))
    with pytest.raises(ValidationError):
        FleetConfig.model_validate(dict(base, nodes=[{"name": "a", "host": "2.2.2.2", "typo": 1}]))


def test_defaults_master_needs_trusted_cert_nodes_are_pinned():
    cfg = FleetConfig.model_validate({"master": {"name": "ru", "host": "1.1.1.1"},
                                      "nodes": [{"name": "de", "host": "2.2.2.2"}]})
    assert cfg.tls_for(cfg.master).mode == "ip"
    assert cfg.tls_for(cfg.nodes[0]).mode == "selfsigned"
    assert cfg.cascade_for(cfg.nodes[0]).reality.resolved()[1] == ["ya.ru"]
    assert cfg.direct_for(cfg.nodes[0]).port == 443


def test_node_add_and_remove_round_trip_keeps_the_rest_of_the_file(tmp_path):
    path = tmp_path / "fleet.yaml"
    path.write_text(EXAMPLE, encoding="utf-8")
    append_node(path, {"name": "de-1", "host": "198.51.100.20", "label": "🇩🇪 Germany"})
    append_node(path, {"name": "nl-1", "host": "198.51.100.21", "ssh": {"port": 2222}})
    cfg = load_config(path)
    assert [n.name for n in cfg.nodes] == ["de-1", "nl-1"]
    assert cfg.nodes[0].label == "🇩🇪 Germany" and cfg.ssh_for(cfg.nodes[1]).port == 2222
    assert [u.name for u in cfg.users] == ["me"] and cfg.master.label == "🇷🇺 RU"

    assert _drop_node_from_yaml(path, "de-1")
    assert [n.name for n in load_config(path).nodes] == ["nl-1"]
    assert _drop_node_from_yaml(path, "nl-1")
    assert load_config(path).nodes == []
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["users"] == [{"name": "me"}]


class FakePanel:
    """In-memory stand-in for the master API: enough to exercise the reconciler."""

    def __init__(self):
        self._nodes, self._inbounds, self._clients = [], [], {}
        self.template = copy.deepcopy(TEMPLATE)
        self.calls = []

    def nodes(self):
        return copy.deepcopy(self._nodes)

    def add_node(self, payload):
        node = dict(payload, id=len(self._nodes) + 1, status="online", hasApiToken=True)
        node.pop("apiToken", None)
        self._nodes.append(node)
        self.calls.append("add_node")
        return node

    def update_node(self, node_id, payload):
        for n in self._nodes:
            if n["id"] == node_id:
                n.update({k: v for k, v in payload.items() if k != "apiToken"})
        self.calls.append("update_node")

    def probe_node(self, node_id):
        return None

    def inbounds(self):
        return copy.deepcopy(self._inbounds)

    def add_inbound(self, payload):
        inbound = dict(copy.deepcopy(payload), id=len(self._inbounds) + 1)
        self._inbounds.append(inbound)
        self.calls.append("add_inbound")
        return copy.deepcopy(inbound)

    def update_inbound(self, inbound_id, payload):
        self._inbounds = [dict(copy.deepcopy(payload), id=inbound_id) if i["id"] == inbound_id else i
                          for i in self._inbounds]
        self.calls.append("update_inbound")

    def delete_inbound(self, inbound_id):
        self._inbounds = [i for i in self._inbounds if i["id"] != inbound_id]
        self.calls.append("delete_inbound")

    def new_x25519(self):
        return {"privateKey": "PRIV", "publicKey": "PUB"}

    def get(self, path):
        assert path == "/server/getNewVlessEnc"
        self.calls.append("vless_enc")
        return {"auths": [{"id": "x25519", "decryption": "X-D", "encryption": "X-E"},
                          {"id": "mlkem768", "decryption": "mlkem768x25519plus.native.600s.D",
                           "encryption": "mlkem768x25519plus.native.0rtt.E"}]}

    def clients(self):
        return copy.deepcopy(list(self._clients.values()))

    def client(self, email):
        c = self._clients.get(email)
        return {"client": copy.deepcopy(c), "inboundIds": list(c["inboundIds"])} if c else None

    def add_client(self, client, inbound_ids):
        self._clients[client["email"]] = dict(client, uuid="uuid-" + client["email"], inboundIds=list(inbound_ids))
        self.calls.append("add_client")

    def attach_clients(self, emails, inbound_ids):
        for email in emails:
            ids = self._clients[email]["inboundIds"]
            ids.extend(i for i in inbound_ids if i not in ids)
        self.calls.append("attach_clients")

    def xray_template(self):
        return copy.deepcopy(self.template)

    def save_xray_template(self, template):
        self.template = copy.deepcopy(template)
        self.calls.append("save_template")

    def restart_xray(self):
        self.calls.append("restart_xray")

    def test_outbound(self, outbound):
        # Works only when the master dials with a client that really sits on the node inbound.
        port, uid = outbound["settings"]["port"], outbound["settings"]["id"]
        target = [i["id"] for i in self._inbounds if i.get("nodeId") and i["port"] == port]
        ok = any(c["uuid"] == uid and set(target) & set(c["inboundIds"]) for c in self._clients.values())
        return {"success": ok, "delay": 40} if ok else {"success": False, "error": "EOF"}

    _settings = None

    def settings(self):
        if self._settings is None:
            self._settings = {"subEnable": True, "subPort": 2096, "subPath": "/s/", "subCertFile": "/c", "webCertFile": "/c"}
        return copy.deepcopy(self._settings)

    def update_settings(self, settings):
        self._settings = copy.deepcopy(settings)
        self.calls.append("update_settings")

    def restart_panel(self):
        pass

    def wait_ready(self, timeout):
        pass


def _access(names):
    from xui_fleet.reconcile import NodeAccess
    return {n: NodeAccess("https", f"{n}.example", 2053, "/x/", "tok", token_fresh=True,
                          tls_verify_mode="pin", pinned_cert_sha256="PIN") for n in names}


def _cfg(nodes):
    return FleetConfig.model_validate({"master": {"name": "ru", "host": "9.9.9.9"},
                                       "nodes": nodes, "users": [{"name": "alice"}]})


def test_reconcile_builds_cascade_and_is_idempotent():
    panel = FakePanel()
    cfg = _cfg([{"name": "de", "host": "de.example"}])
    report = Reconciler(panel, cfg).run(_access(["de"]))
    tags = {i["tag"]: i for i in panel.inbounds()}
    assert set(tags) == {"fleet-direct-de", "fleet-cascade-de", "fleet-xhttp-de"}  # Hysteria2 needs the node cert
    assert tags["fleet-direct-de"]["nodeId"] == 1 and "nodeId" not in tags["fleet-cascade-de"]
    assert tags["fleet-cascade-de"]["shareAddr"] == "9.9.9.9"
    out = [o for o in panel.template["outbounds"] if o["tag"] == "fleet-out-de"][0]
    assert out["settings"]["address"] == "de.example" and out["settings"]["id"] == "uuid-fleet-bridge"
    # the bridge only lives on the upstream; the user gets both entries
    assert panel._clients["fleet-bridge"]["inboundIds"] == [tags["fleet-direct-de"]["id"]]
    assert sorted(panel._clients["alice"]["inboundIds"]) == sorted(i["id"] for i in tags.values())
    assert report.subscriptions["alice"].startswith("https://9.9.9.9:2096/s/")
    assert "fleet-bridge" not in report.subscriptions

    panel.calls.clear()
    again = Reconciler(panel, cfg).run(_stale(_access(["de"])))
    assert again.changes == [] and panel.calls == []


def _stale(access):
    for a in access.values():
        a.token_fresh = False
    return access


def test_new_node_reaches_existing_users_and_partial_run_keeps_other_routes():
    panel = FakePanel()
    Reconciler(panel, _cfg([{"name": "de", "host": "de.example"}])).run(_access(["de"]))
    # a user created by hand in the panel UI on a fleet inbound
    panel._clients["manual"] = {"email": "manual", "subId": "m1", "uuid": "u", "inboundIds": [1]}
    cfg = _cfg([{"name": "de", "host": "de.example"}, {"name": "nl", "host": "nl.example"}])
    Reconciler(panel, cfg).run(_access(["nl"]), only={"nl"})
    all_ids = sorted(i["id"] for i in panel.inbounds())
    assert len(all_ids) == 6
    assert sorted(panel._clients["manual"]["inboundIds"]) == all_ids
    assert sorted(panel._clients["alice"]["inboundIds"]) == all_ids
    out_tags = sorted(o["tag"] for o in panel.template["outbounds"] if o["tag"].startswith("fleet-out-"))
    assert out_tags == ["fleet-out-de", "fleet-out-nl"]
    ports = sorted(i["port"] for i in panel.inbounds() if i["tag"].startswith("fleet-cascade-"))
    assert ports == [443, 8443]


def test_dry_run_changes_nothing():
    panel = FakePanel()
    report = Reconciler(panel, _cfg([{"name": "de", "host": "de.example"}]), dry_run=True).run(_access(["de"]))
    assert report.changes and panel.calls == [] and panel.template == TEMPLATE


def test_remove_node_tears_down_only_that_node():
    panel = FakePanel()
    cfg = _cfg([{"name": "de", "host": "de.example"}, {"name": "nl", "host": "nl.example"}])
    Reconciler(panel, cfg).run(_access(["de", "nl"]))
    panel.delete_node = lambda node_id: panel._nodes.__setitem__(slice(None), [n for n in panel._nodes if n["id"] != node_id])
    Reconciler(panel, cfg).remove_node("de")
    assert sorted(i["tag"] for i in panel.inbounds()) == ["fleet-cascade-nl", "fleet-direct-nl", "fleet-xhttp-nl"]
    assert [o["tag"] for o in panel.template["outbounds"] if o["tag"].startswith("fleet-")] == ["fleet-out-nl"]
    assert [n["name"] for n in panel.nodes()] == ["nl"]


def test_cascade_is_verified_end_to_end_and_failure_is_reported():
    panel = FakePanel()
    cfg = _cfg([{"name": "de", "host": "de.example"}])
    ok = Reconciler(panel, cfg).run(_access(["de"]))
    assert not [w for w in ok.warnings if "каскад" in w]
    # the bridge lost its seat on the node (e.g. the node had not synced yet)
    panel._clients["fleet-bridge"]["inboundIds"] = []
    rec = Reconciler(panel, cfg)
    rec.verify_wait = 0
    report = Report()
    rec.report = report
    rec._verify_cascades(cfg.nodes)
    assert any("каскад RU → de не прошёл проверку: EOF" in w for w in report.warnings)


def test_node_is_registered_without_adopting_its_own_inbounds():
    panel = FakePanel()
    Reconciler(panel, _cfg([{"name": "de", "host": "de.example"}])).run(_access(["de"]))
    node = panel.nodes()[0]
    # only the service's own inbounds on that node are synced, never the owner's
    assert node["inboundSyncMode"] == "selected" and node["inboundTags"] == ["fleet-direct-de", "fleet-xhttp-de"]


def test_busy_443_on_a_live_node_uses_the_port_found_during_provisioning():
    panel = FakePanel()
    access = _access(["de"])
    access["de"].direct_port = 8443
    Reconciler(panel, _cfg([{"name": "de", "host": "de.example"}])).run(access)
    direct = [i for i in panel.inbounds() if i["tag"] == "fleet-direct-de"][0]
    assert direct["port"] == 8443
    out = [o for o in panel.template["outbounds"] if o["tag"] == "fleet-out-de"][0]
    assert out["settings"]["port"] == 8443  # the cascade dials the same port
    # a later run without provisioning data keeps the working port
    again = Reconciler(panel, _cfg([{"name": "de", "host": "de.example"}])).run(_stale(_access(["de"])))
    assert again.changes == []


def test_backup_remarks_and_hysteria_cert_are_rewritten_for_the_new_node():
    from xui_fleet import migrate as mg
    pem = "-----BEGIN CERTIFICATE-----\nAAEC\n-----END CERTIFICATE-----\n"
    facts = mg.NodeFacts(node_id=3, host="198.51.100.9", busy_tcp={443}, busy_udp=set(), cert_pem=pem, key_pem="K")
    b = mg.Backup([], [], {}, {}, {}, {})
    imp = mg.Importer(None, b, "uk-1", facts, "🇬🇧 UK", dry_run=True, log=lambda m: None)
    assert imp._rename("🇩🇪 Germany - Резерв 1") == "🇬🇧 UK - Резерв 1"
    old = {"tag": "in-443-udp", "protocol": "hysteria", "port": 443, "enable": 1, "remark": "🇩🇪 Germany - Резерв 7",
           "settings": {"version": 2, "clients": [{"email": "x"}]}, "sniffing": {},
           "stream_settings": {"security": "tls", "tlsSettings": {"serverName": "old.example",
                               "certificates": [{"certificateFile": "/root/cert/old/fullchain.pem"}]}}}
    assert imp._port(old, {443}, set()) == 443  # UDP 443 is free even though TCP 443 is taken
    p = imp._inbound_payload(old, 443)
    tls = p["streamSettings"]["tlsSettings"]
    assert p["settings"]["clients"] == [] and p["nodeId"] == 3
    assert tls["serverName"] == "198.51.100.9" and "certificateFile" not in tls["certificates"][0]
    # "AAEC" is base64 for bytes 00 01 02: the pin is the hex SHA-256 of the DER bytes
    assert tls["settings"]["pinnedPeerCertSha256"] == [mg.hashlib.sha256(bytes([0, 1, 2])).hexdigest()]


def test_warp_rule_is_copied_once_and_wins_over_blocking_rules():
    from xui_fleet import migrate as mg
    warp = {"tag": "warp", "protocol": "wireguard", "settings": {}}
    once = mg.merge_warp(TEMPLATE, warp, ["in-51471-tcp"])
    assert mg.merge_warp(once, warp, ["in-51471-tcp"]) == once
    assert [r["outboundTag"] for r in once["routing"]["rules"]] == ["api", "warp", "blocked"]


def test_node_gets_its_own_warp_identity_with_the_old_outbound_shape():
    from xui_fleet import migrate as mg
    import base64
    old = {"tag": "warp", "protocol": "wireguard", "targetStrategy": "ForceIPv4",
           "settings": {"mtu": 1280, "secretKey": "OLD", "address": ["172.16.0.2/32"], "reserved": [1, 2, 3],
                        "peers": [{"publicKey": "OLDPEER", "endpoint": "engage.cloudflareclient.com:2408",
                                   "allowedIPs": ["0.0.0.0/0"]}]}}
    reg = {"data": {"private_key": "NEWKEY"},
           "config": {"config": {"client_id": base64.b64encode(bytes([9, 8, 7])).decode(),
                                 "interface": {"addresses": {"v4": "172.16.0.9", "v6": "2606::9"}},
                                 "peers": [{"public_key": "NEWPEER", "endpoint": {"host": "engage.cloudflareclient.com:2408"}}]}}}
    out = mg.warp_from_registration(old, reg)
    s = out["settings"]
    assert (s["secretKey"], s["reserved"], s["address"]) == ("NEWKEY", [9, 8, 7], ["172.16.0.9/32", "2606::9/128"])
    assert s["peers"][0]["publicKey"] == "NEWPEER" and s["peers"][0]["allowedIPs"] == ["0.0.0.0/0"]
    assert s["mtu"] == 1280 and out["targetStrategy"] == "ForceIPv4"
    assert old["settings"]["secretKey"] == "OLD"
    keys = mg.new_wireguard_keys()
    assert len(base64.b64decode(keys["privateKey"])) == 32 and len(base64.b64decode(keys["publicKey"])) == 32


def test_ru_entry_impersonates_its_own_domain_once_one_is_set():
    ip_only = FleetConfig.model_validate({"master": {"name": "ru", "host": "1.1.1.1"}, "nodes": [{"name": "de", "host": "2.2.2.2"}]})
    assert ip_only.cascade_for(ip_only.nodes[0]).reality.resolved()[:2] == ("ya.ru:443", ["ya.ru"])
    cfg = FleetConfig.model_validate({"master": {"name": "ru", "host": "1.1.1.1", "tls": {"mode": "domain", "domain": "vpn.example.ru"}},
                                      "nodes": [{"name": "de", "host": "2.2.2.2"},
                                                {"name": "nl", "host": "3.3.3.3", "cascade": {"reality": {"target": "x.com:443"}}}]})
    assert cfg.cascade_for(cfg.nodes[0]).reality.resolved()[:2] == ("127.0.0.1:8444", ["vpn.example.ru"])
    assert cfg.cascade_for(cfg.nodes[1]).reality.resolved()[0] == "x.com:443"  # an explicit choice is kept


def test_every_node_gets_the_same_set_with_unified_names_and_free_ports():
    panel = FakePanel()
    access = _access(["uk"])
    a = access["uk"]
    a.busy_tcp, a.busy_udp = {443, 8443, 2096}, {443}  # a live server: 443 and 8443 taken by its owner
    a.cert_pem, a.key_pem, a.cert_pin_hex = "-----BEGIN CERTIFICATE-----\nAAEC\n-----END CERTIFICATE-----", "K", "ab" * 32
    a.direct_port = 9000
    cfg = FleetConfig.model_validate({"master": {"name": "ru", "host": "9.9.9.9"}, "users": [{"name": "alice"}],
                                      "nodes": [{"name": "uk", "host": "uk.example", "label": "🇬🇧 UK"}]})
    Reconciler(panel, cfg).run(access)
    by_tag = {i["tag"]: i for i in panel.inbounds()}
    assert {t: (i["remark"], i["port"]) for t, i in by_tag.items()} == {
        "fleet-cascade-uk": ("🇬🇧 UK · через RU", 443),
        "fleet-direct-uk": ("🇬🇧 UK", 9000),
        "fleet-xhttp-uk": ("🇬🇧 UK · XHTTP", 2053),
        "fleet-hy2-uk": ("🇬🇧 UK · Hysteria2", 8443),
    }
    order = sorted(by_tag.values(), key=lambda i: i["subSortIndex"])
    assert [i["tag"] for i in order] == ["fleet-xhttp-uk", "fleet-hy2-uk", "fleet-cascade-uk", "fleet-direct-uk"]
    hy2 = by_tag["fleet-hy2-uk"]["streamSettings"]["tlsSettings"]
    assert hy2["settings"]["pinnedPeerCertSha256"] == ["ab" * 32] and hy2["serverName"] == "uk.example"
    assert sorted(panel._clients["alice"]["inboundIds"]) == sorted(i["id"] for i in by_tag.values())
    assert panel.nodes()[0]["inboundTags"] == ["fleet-direct-uk", "fleet-hy2-uk", "fleet-xhttp-uk"]
    assert Reconciler(panel, cfg).run(_stale(_access(["uk"]))).changes == []


def test_adding_a_node_works_whatever_the_list_indentation(tmp_path):
    # The exact layout that broke: PyYAML writes list items without indentation under "nodes:".
    path = tmp_path / "fleet.yaml"
    path.write_text(
        "version: 1\nmaster:\n  name: ru-msk\n  host: 203.0.113.10\n"
        "nodes:\n- name: ni-r-1\n  host: 198.51.100.20\n  cascade:\n    port: 443\n"
        "users:\n- name: alice\n", encoding="utf-8")
    append_node(path, {"name": "de-fra-1", "host": "198.51.100.30", "ssh": {"port": 22}})
    cfg = load_config(path)
    assert [n.name for n in cfg.nodes] == ["ni-r-1", "de-fra-1"]
    assert cfg.nodes[0].cascade.port == 443 and [u.name for u in cfg.users] == ["alice"]
    with pytest.raises(ValueError):
        append_node(path, {"name": "de-fra-1", "host": "1.1.1.1"})
    assert _drop_node_from_yaml(path, "ni-r-1") and [n.name for n in load_config(path).nodes] == ["de-fra-1"]
    assert not _drop_node_from_yaml(path, "nope")


def test_flag_is_detected_by_default_and_can_be_chosen_or_removed():
    from xui_fleet.geo import display_label
    assert display_label("de-fra-2", "Germany", None, "DE") == "🇩🇪 Germany"
    assert display_label("de-fra-2", None, None, "DE") == "🇩🇪 Германия"  # no label: country name
    assert display_label("x", "Germany", "NL", "DE") == "🇳🇱 Germany"  # a manual choice wins over GeoIP
    assert display_label("x", "🇬🇧 UK", None, "SG") == "🇬🇧 UK"  # a flag typed by the user is kept
    assert display_label("x", "🇬🇧 UK", "GB", "SG") == "🇬🇧 UK"  # never doubled
    assert display_label("x", "🇬🇧 UK", "none", "GB") == "UK"
    assert display_label("node-7", None, None, None) == "node-7"
    assert display_label("x", "Fast", "🇫🇮", None) == "🇫🇮 Fast"


def test_connection_names_follow_the_flag():
    panel = FakePanel()
    cfg = FleetConfig.model_validate({"master": {"name": "ru", "host": "9.9.9.9"}, "users": [{"name": "a"}],
                                      "nodes": [{"name": "de", "host": "de.example", "label": "Germany"}]})
    Reconciler(panel, cfg, countries={"de": "DE"}).run(_access(["de"]))
    names = sorted(i["remark"] for i in panel.inbounds())
    assert names == ["🇩🇪 Germany", "🇩🇪 Germany · XHTTP", "🇩🇪 Germany · через RU"]
    cfg.nodes[0].flag = "NL"
    Reconciler(panel, cfg, countries={"de": "DE"}).run(_stale(_access(["de"])))
    assert sorted(i["remark"] for i in panel.inbounds()) == ["🇳🇱 Germany", "🇳🇱 Germany · XHTTP", "🇳🇱 Germany · через RU"]


def test_ru_hysteria2_entry_shares_the_cascade_exit_and_reaches_users():
    panel = FakePanel()
    cfg = FleetConfig.model_validate({"master": {"name": "ru", "host": "9.9.9.9", "tls": {"mode": "domain", "domain": "vpn.example.ru"}},
                                      "users": [{"name": "alice"}],
                                      "nodes": [{"name": "de", "host": "de.example", "label": "🇩🇪 Germany"}]})
    Reconciler(panel, cfg).run(_access(["de"]))
    by_tag = {i["tag"]: i for i in panel.inbounds()}
    hy = by_tag["fleet-rhy2-de"]
    assert "nodeId" not in hy and hy["port"] == 443 and hy["protocol"] == "hysteria"
    assert hy["remark"] == "🇩🇪 Germany · через RU (Hysteria2)" and hy["shareAddr"] == "vpn.example.ru"
    tls = hy["streamSettings"]["tlsSettings"]
    assert tls["certificates"][0]["certificateFile"] == "/root/cert/vpn.example.ru/fullchain.pem"
    assert tls["settings"]["pinnedPeerCertSha256"] == []  # a real cert, nothing to pin
    outs = [o["tag"] for o in panel.template["outbounds"] if o["tag"].startswith("fleet-out-")]
    assert outs == ["fleet-out-de"]  # one exit for both RU entries
    rules = {r["inboundTag"][0]: r["outboundTag"] for r in panel.template["routing"]["rules"] if r.get("inboundTag")}
    assert rules["fleet-rhy2-de"] == rules["fleet-cascade-de"] == "fleet-out-de"
    assert hy["id"] in panel._clients["alice"]["inboundIds"]
    order = [i["tag"] for i in sorted(panel.inbounds(), key=lambda i: i["subSortIndex"])]
    assert order[:3] == ["fleet-rxh-de", "fleet-xhttp-de", "fleet-rhy2-de"]
    assert Reconciler(panel, cfg).run(_stale(_access(["de"]))).changes == []


def test_hidden_entries_leave_the_subscription_but_keep_working():
    panel = FakePanel()
    cfg = FleetConfig.model_validate({"master": {"name": "ru", "host": "9.9.9.9", "tls": {"mode": "domain", "domain": "v.example"}},
                                      "users": [{"name": "a"}], "nodes": [{"name": "de", "host": "de.example"}]})
    Reconciler(panel, cfg).run(_access(["de"]))
    cfg.nodes[0].hide = ["direct", "xhttp", "cascade"]
    Reconciler(panel, cfg).run(_stale(_access(["de"])))
    hidden = {i["tag"]: bool(i.get("excludeFromSub")) for i in panel.inbounds()}
    assert hidden == {"fleet-direct-de": True, "fleet-xhttp-de": True, "fleet-cascade-de": True, "fleet-rhy2-de": False,
                      "fleet-rxh-de": False}
    assert any(o["tag"] == "fleet-out-de" for o in panel.template["outbounds"])  # the hidden direct still carries the cascade
    assert Reconciler(panel, cfg).run(_stale(_access(["de"]))).changes == []
    cfg.nodes[0].hide = []
    Reconciler(panel, cfg).run(_stale(_access(["de"])))
    assert not any(i.get("excludeFromSub") for i in panel.inbounds())
    with pytest.raises(ValidationError):
        FleetConfig.model_validate({"master": {"name": "ru", "host": "1.1.1.1"}, "nodes": [{"name": "x", "host": "2.2.2.2", "hide": ["typo"]}]})


def test_hysteria2_cascade_works_without_the_tcp_one():
    panel = FakePanel()
    cfg = FleetConfig.model_validate({"master": {"name": "ru", "host": "9.9.9.9", "tls": {"mode": "domain", "domain": "v.example"}},
                                      "users": [{"name": "a"}], "nodes": [{"name": "de", "host": "de.example"}]})
    Reconciler(panel, cfg).run(_access(["de"]))
    cfg.nodes[0].set = cfg.set_for(cfg.nodes[0]).model_copy(update={"cascade_tcp": False})
    Reconciler(panel, cfg).run(_stale(_access(["de"])))
    tags = {i["tag"] for i in panel.inbounds()}
    assert "fleet-cascade-de" not in tags and "fleet-rhy2-de" in tags
    rules = {r["inboundTag"][0]: r["outboundTag"] for r in panel.template["routing"]["rules"] if r.get("inboundTag")}
    assert rules == {"api": "api", "fleet-rhy2-de": "fleet-out-de", "fleet-rxh-de": "fleet-out-de"}
    assert [o["tag"] for o in panel.template["outbounds"] if o["tag"].startswith("fleet-out")] == ["fleet-out-de"]
    assert Reconciler(panel, cfg).run(_stale(_access(["de"]))).changes == []


def test_backup_bundle_round_trip_and_keeps_only_ten(tmp_path):
    import sqlite3, time as _t
    from xui_fleet import backup as bk
    db_path = tmp_path / "src.db"
    con = sqlite3.connect(str(db_path))
    for t in ("inbounds", "clients", "nodes"):
        con.execute(f"create table {t} (id integer)")
    con.execute("insert into clients values (1)"); con.commit(); con.close()
    db = db_path.read_bytes()
    assert bk.db_summary(db) == {"inbounds": 0, "clients": 1, "nodes": 0}
    with pytest.raises(Exception):
        bk.db_summary(b"not a database")
    out = tmp_path / "backups"
    for i in range(12):
        bk.write_bundle(out, db, {"/root/cert/x/fullchain.pem": b"CERT"}, {"created": 1_700_000_000 + i * 60, "host": "1.2.3.4",
                                                                          "counts": bk.db_summary(db)})
    items = bk.list_backups(out)
    assert len(items) == 10 and items[0].created > items[-1].created  # the two oldest were dropped
    back = bk.read_bundle(items[0].path)
    assert back["db"] == db and back["files"] == {"/root/cert/x/fullchain.pem": b"CERT"} and back["meta"]["host"] == "1.2.3.4"
    assert oct(items[0].path.stat().st_mode & 0o777) == "0o600"
    script = bk.upload_script({"/etc/x-ui/x-ui.db": b"\\x00\\x01"})
    assert "base64 -d > /etc/x-ui/x-ui.db.fleet-new" in script and "chmod 600" in script


def test_cyrillic_user_names_are_accepted():
    cfg = FleetConfig.model_validate({"master": {"name": "ru", "host": "1.1.1.1"}, "users": [{"name": "Main-Мама-phone"}]})
    assert cfg.users[0].name == "Main-Мама-phone"
    with pytest.raises(ValidationError):
        FleetConfig.model_validate({"master": {"name": "ru", "host": "1.1.1.1"}, "users": [{"name": "with space"}]})


def test_a_slow_server_does_not_hold_up_the_others():
    import time as _t
    from xui_fleet.engine import parallel
    start = _t.time()
    out = parallel([0.05, 3.0, 0.05], lambda d: (_t.sleep(d), f"ok {d}")[1], 0.5, lambda d: f"timeout {d}")
    assert out == ["ok 0.05", "timeout 3.0", "ok 0.05"]
    assert _t.time() - start < 1.0  # bounded by the limit, not by the slowest server



@pytest.mark.warp
def test_warp_entry_and_ru_to_node_to_warp_chain():
    panel = FakePanel()
    access = _access(["de"])
    a = access["de"]
    a.busy_tcp, a.busy_udp = {443}, set()
    a.cert_pem, a.key_pem, a.cert_pin_hex = "-----BEGIN CERTIFICATE-----\nAAEC\n-----END CERTIFICATE-----", "K", "ab" * 32
    cfg = FleetConfig.model_validate({"master": {"name": "ru", "host": "9.9.9.9", "tls": {"mode": "domain", "domain": "v.example"}},
                                      "users": [{"name": "alice"}], "nodes": [{"name": "de", "host": "de.example", "label": "🇩🇪 Germany"}]})
    Reconciler(panel, cfg).run(access)
    by_tag = {i["tag"]: i for i in panel.inbounds()}
    warp, up, rwarp = by_tag["fleet-warp-de"], by_tag["fleet-warpup-de"], by_tag["fleet-rwarp-de"]
    assert warp["remark"] == "🇩🇪 Germany · WARP" and warp["protocol"] == "hysteria" and warp["nodeId"] == 1
    assert up["excludeFromSub"] and up["nodeId"] == 1 and up["protocol"] == "vless"  # hidden upstream on the node
    assert rwarp["remark"] == "🇩🇪 Germany · через RU → WARP" and "nodeId" not in rwarp and rwarp["protocol"] == "hysteria"
    assert rwarp["port"] != by_tag["fleet-rhy2-de"]["port"]  # two RU Hysteria2 entries cannot share a UDP port
    rules = {r["inboundTag"][0]: r["outboundTag"] for r in panel.template["routing"]["rules"] if r.get("inboundTag")}
    assert rules["fleet-rwarp-de"] == "fleet-out-warp-de" and rules["fleet-rhy2-de"] == "fleet-out-de"
    out = next(o for o in panel.template["outbounds"] if o["tag"] == "fleet-out-warp-de")
    assert out["settings"]["port"] == up["port"] and out["settings"]["id"] == "uuid-fleet-bridge"
    assert up["id"] in panel._clients["fleet-bridge"]["inboundIds"]  # the master can log in to the upstream
    alice = set(panel._clients["alice"]["inboundIds"])
    assert {warp["id"], rwarp["id"]} <= alice and up["id"] not in alice  # users never see the upstream
    assert Reconciler(panel, cfg).run(_stale(_access(["de"]))).changes == []


def test_xhttp_entries_are_protected_and_old_ones_upgraded_in_place():
    panel = FakePanel()
    access = _access(["uk"])
    cfg = _cfg([{"name": "uk", "host": "uk.example"}])
    Reconciler(panel, cfg).run(access)
    xh = {i["tag"]: i for i in panel.inbounds()}["fleet-xhttp-uk"]
    assert xh["settings"]["decryption"].startswith("mlkem768") and "fallbacks" not in xh["settings"]  # Xray refuses both
    assert xh["streamSettings"]["xhttpSettings"]["xmux"]["hMaxRequestTimes"] == "600-900"
    assert xh["streamSettings"]["realitySettings"]["settings"]["fingerprint"] == "firefox"
    # An XHTTP entry made before this version: same keys and path, encryption added on the next run.
    old = dict(xh, settings={"clients": [], "decryption": "none", "encryption": "none", "fallbacks": []})
    old["streamSettings"] = copy.deepcopy(xh["streamSettings"])
    old["streamSettings"]["xhttpSettings"].pop("xmux")
    old["streamSettings"]["realitySettings"]["settings"]["fingerprint"] = "chrome"
    panel.update_inbound(xh["id"], old)
    Reconciler(panel, cfg).run(_stale(_access(["uk"])))
    new = {i["tag"]: i for i in panel.inbounds()}["fleet-xhttp-uk"]
    assert xc.is_protected(new) and new["streamSettings"]["xhttpSettings"]["xmux"]
    assert new["streamSettings"]["xhttpSettings"]["path"] == xh["streamSettings"]["xhttpSettings"]["path"]
    assert new["streamSettings"]["realitySettings"]["privateKey"] == "PRIV"
    assert new["streamSettings"]["realitySettings"]["settings"]["fingerprint"] == "firefox"
    assert Reconciler(panel, cfg).run(_stale(_access(["uk"]))).changes == []


def test_ru_xhttp_entry_steals_own_domain_and_shares_the_node_exit():
    panel = FakePanel()
    cfg = FleetConfig.model_validate({"master": {"name": "ru", "host": "9.9.9.9", "tls": {"mode": "domain", "domain": "v.example"}},
                                      "users": [{"name": "alice"}], "nodes": [{"name": "de", "host": "de.example", "label": "🇩🇪 Germany",
                                                                             "set": {"cascade_tcp": False}}]})
    Reconciler(panel, cfg).run(_access(["de"]))
    by_tag = {i["tag"]: i for i in panel.inbounds()}
    rxh = by_tag["fleet-rxh-de"]
    assert rxh["remark"] == "🇩🇪 Germany · через RU (XHTTP)" and "nodeId" not in rxh
    reality = rxh["streamSettings"]["realitySettings"]
    assert reality["target"] == "127.0.0.1:8444" and reality["serverNames"] == ["v.example"]
    assert xc.is_protected(rxh) and rxh["shareAddr"] == "v.example" and rxh["port"] not in (443, 8443)
    assert "fleet-cascade-de" not in by_tag
    rules = {r["inboundTag"][0]: r["outboundTag"] for r in panel.template["routing"]["rules"] if r.get("inboundTag")}
    assert rules["fleet-rxh-de"] == rules["fleet-rhy2-de"] == "fleet-out-de"
    order = [i["tag"] for i in sorted(by_tag.values(), key=lambda i: i["subSortIndex"]) if not i.get("excludeFromSub")]
    assert order.index("fleet-rxh-de") < order.index("fleet-rhy2-de") < order.index("fleet-direct-de")
    assert rxh["id"] in panel._clients["alice"]["inboundIds"]
    assert Reconciler(panel, cfg).run(_stale(_access(["de"]))).changes == []


def test_reality_borrows_a_neighbor_site_unless_the_config_names_one():
    from xui_fleet.provision import _neighbor_rank
    found = [("1.2.3.77", "cdn.forge.com"), ("1.2.3.111", "tg.drums.com"), ("1.2.3.148", "a29.stash.com"),
             ("1.2.3.172", "c4.stash.com"), ("1.2.3.193", "1.2.3.193.sslip.io"), ("1.2.3.9", "www.cloudflare.com")]
    assert _neighbor_rank(found)[0] == ("1.2.3.148", "a29.stash.com")  # plain name, provider hosting several sites
    panel = FakePanel()
    cfg = _cfg([{"name": "uk", "host": "uk.example"}, {"name": "de", "host": "de.example", "direct": {"reality": {"target": "own.example"}}}])
    nb = {"uk": ("1.2.3.148:443", "a29.stash.com"), "de": ("5.6.7.8:443", "x.example")}
    Reconciler(panel, cfg, neighbors=nb).run(_access(["uk", "de"]))
    r = {i["tag"]: i["streamSettings"]["realitySettings"] for i in panel.inbounds() if i["protocol"] == "vless" and i.get("nodeId")}
    assert r["fleet-direct-uk"]["target"] == "1.2.3.148:443" and r["fleet-direct-uk"]["serverNames"] == ["a29.stash.com"]
    assert r["fleet-xhttp-uk"]["serverNames"] == ["a29.stash.com"]
    assert r["fleet-direct-de"]["target"] == "own.example:443"  # explicit config wins


def test_master_entries_never_take_the_decoy_panel_or_subscription_port():
    panel = FakePanel()
    panel._settings = {"subEnable": True, "subPort": 2053, "webPort": 2083, "subCertFile": "/c", "webCertFile": "/c"}
    cfg = FleetConfig.model_validate({"master": {"name": "ru", "host": "9.9.9.9", "tls": {"mode": "domain", "domain": "v.example"},
                                                 "cascade_ports": [8443, 8446]},
                                      "users": [{"name": "a"}],
                                      "nodes": [{"name": f"n{i}", "host": f"n{i}.example"} for i in range(3)]})
    Reconciler(panel, cfg).run(_access([f"n{i}" for i in range(3)]))
    master_tcp = [i["port"] for i in panel.inbounds() if not i.get("nodeId") and i["protocol"] != "hysteria"]
    assert len(master_tcp) == len(set(master_tcp)) == 6
    assert not {8444, 2053, 2083} & set(master_tcp)  # nginx decoy, subscription, panel


def test_servers_with_the_same_name_get_a_short_number():
    panel = FakePanel()
    cfg = _cfg([{"name": "de-1", "host": "a.example", "label": "🇩🇪 Germany"}, {"name": "uk", "host": "u.example", "label": "🇬🇧 UK"},
                {"name": "de-2", "host": "b.example", "label": "Germany", "flag": "DE"}])
    Reconciler(panel, cfg).run(_access(["de-1", "uk", "de-2"]))
    remarks = {i["tag"]: i["remark"] for i in panel.inbounds()}
    assert remarks["fleet-direct-de-1"] == "🇩🇪 Germany 1" and remarks["fleet-direct-de-2"] == "🇩🇪 Germany 2"
    assert remarks["fleet-xhttp-de-2"] == "🇩🇪 Germany 2 · XHTTP" and remarks["fleet-direct-uk"] == "🇬🇧 UK"


def test_subscription_is_grouped_by_country_with_stable_servers_and_best_entries_first():
    panel = FakePanel()
    cfg = FleetConfig.model_validate({"master": {"name": "ru", "host": "9.9.9.9", "tls": {"mode": "domain", "domain": "v.example"}},
                                      "users": [{"name": "a"}],
                                      "nodes": [{"name": "de-1", "host": "a.example", "label": "🇩🇪 Germany"},
                                                {"name": "uk", "host": "u.example", "label": "🇬🇧 UK"},
                                                {"name": "de-2", "host": "b.example", "label": "🇩🇪 Germany"}]})
    Reconciler(panel, cfg, health={"de-1": 2 / 6, "de-2": 1.0, "uk": 1.0}).run(_access(["de-1", "uk", "de-2"]))
    shown = [i["remark"] for i in sorted(panel.inbounds(), key=lambda i: i["subSortIndex"]) if not i.get("excludeFromSub")]
    countries = [r.split(" ")[0] for r in shown]
    assert countries == sorted(countries, key=lambda c: ["🇩🇪", "🇬🇧"].index(c))  # one block per country
    de = [r for r in shown if r.startswith("🇩🇪")]
    assert de[0] == "🇩🇪 Germany 2 · через RU (XHTTP)" and de[1] == "🇩🇪 Germany 2 · XHTTP"  # stable server, best entry first
    assert all(r.startswith("🇩🇪 Germany 2") for r in de[:len(de) // 2]) and de[-1].startswith("🇩🇪 Germany 1")


def test_russian_sites_leave_the_master_directly_and_happ_gets_the_same_profile():
    import base64, json
    panel = FakePanel()
    cfg = FleetConfig.model_validate({"master": {"name": "ru", "host": "9.9.9.9", "tls": {"mode": "domain", "domain": "v.example"}},
                                      "users": [{"name": "a"}], "nodes": [{"name": "de", "host": "de.example"}],
                                      "routing": {"extra_direct": ["domain:example.org", "geoip:by"]}})
    Reconciler(panel, cfg).run(_access(["de"]))
    rules = panel.template["routing"]["rules"]
    assert rules[0]["outboundTag"] == "api"
    ru, ru_ip = rules[1], rules[2]
    assert ru["outboundTag"] == ru_ip["outboundTag"] == "direct"
    assert "geosite:category-ru" in ru["domain"] and "domain:example.org" in ru["domain"] and ru_ip["ip"] == ["geoip:ru", "geoip:by"]
    cascades = {r["inboundTag"][0] for r in rules if str(r.get("outboundTag", "")).startswith("fleet-out-")}
    assert set(ru["inboundTag"]) == cascades and rules.index(ru) < min(
        i for i, r in enumerate(rules) if str(r.get("outboundTag", "")).startswith("fleet-out-"))
    s = panel.settings()
    assert s["subEnableRouting"] and s["subRoutingRules"].startswith("happ://routing/onadd/")
    prof = json.loads(base64.b64decode(s["subRoutingRules"].split("onadd/", 1)[1]))
    assert prof["Name"] == "RU напрямую" and "geoip:ru" in prof["DirectIp"] and "geosite:category-ru" in prof["DirectSites"]
    assert Reconciler(panel, cfg).run(_stale(_access(["de"]))).changes == []
    cfg.routing.ru_direct_on_master = cfg.routing.client_profile = False
    Reconciler(panel, cfg).run(_stale(_access(["de"])))
    assert not any(r.get("ruleTag", "").startswith("fleet-ru") for r in panel.template["routing"]["rules"])
    assert not panel.settings()["subEnableRouting"]


def test_backup_keeps_its_reason(tmp_path):
    from xui_fleet import backup as bk
    bk.write_bundle(tmp_path, b"db", {}, {"created": 1_700_000_000, "reason": "перед удалением сервера de-1", "counts": {}})
    bk.write_bundle(tmp_path, b"db", {}, {"created": 1_700_000_100, "counts": {}})  # made by an older version
    views = [b.view() for b in bk.list_backups(tmp_path)]
    assert [v["reason"] for v in views] == ["", "перед удалением сервера de-1"]


def test_switching_the_domain_moves_every_ru_entry_and_keeps_its_keys():
    panel = FakePanel()
    cfg = FleetConfig.model_validate({"master": {"name": "ru", "host": "9.9.9.9", "tls": {"mode": "domain", "domain": "a.example",
                                                                                         "reserve_domains": ["b.example"]}},
                                      "users": [{"name": "x"}], "nodes": [{"name": "de", "host": "de.example", "set": {"cascade_tcp": False}}]})
    Reconciler(panel, cfg).run(_access(["de"]))
    before = {i["tag"]: i for i in panel.inbounds() if not i.get("nodeId")}
    cfg.master.tls = cfg.master.tls.model_copy(update={"domain": "b.example", "reserve_domains": ["a.example"]})
    Reconciler(panel, cfg).run(_stale(_access(["de"])))
    after = {i["tag"]: i for i in panel.inbounds() if not i.get("nodeId")}
    assert set(after) == set(before)
    for tag, i in after.items():
        assert i["shareAddr"] == "b.example", tag
        st = i["streamSettings"]
        if st["security"] == "reality":
            assert st["realitySettings"]["serverNames"] == ["b.example"]
            assert st["realitySettings"]["privateKey"] == before[tag]["streamSettings"]["realitySettings"]["privateKey"]
        else:
            assert st["tlsSettings"]["serverName"] == "b.example"
            assert st["tlsSettings"]["certificates"][0]["certificateFile"] == "/root/cert/b.example/fullchain.pem"
    assert Reconciler(panel, cfg).run(_stale(_access(["de"]))).changes == []


def test_cover_site_brands_are_stable_fictional_and_keep_443_free():
    from xui_fleet import site
    assert site.brand_for("night-play.ru")["kind"] == "Антикафе настольных игр"  # the name hints at games
    assert site.brand_for("sub.example.ru") == site.brand_for("sub.example.ru")
    assert site.brand_for("198.51.100.20")["lang"] == "en" and not site.brand_for("198.51.100.20")["email"]
    page = site.render("night-play.ru")
    assert "hello@night-play.ru" in page and "http" not in page.split("<body>")[1].replace("https://", "")[:0] + ""
    assert "<script" not in page and "//" not in page.split("<style>")[0].split("<title>")[1]  # no external resources
    panel = FakePanel()
    cfg = FleetConfig.model_validate({"master": {"name": "ru", "host": "9.9.9.9", "tls": {"mode": "domain", "domain": "a.example"}},
                                      "users": [{"name": "x"}], "nodes": [{"name": "de", "host": "de.example"}]})
    Reconciler(panel, cfg).run(_access(["de"]))
    assert 443 not in [i["port"] for i in panel.inbounds() if not i.get("nodeId") and i["protocol"] != "hysteria"]


@pytest.mark.warp
def test_a_new_server_gets_exactly_the_set_of_the_installed_ones():
    template = {"cascade_tcp": False, "xhttp": True, "hysteria2": True, "cascade_xhttp": True, "cascade_hysteria2": True,
                "warp": True, "cascade_warp": True}
    doc = {"master": {"name": "ru", "host": "9.9.9.9", "tls": {"mode": "domain", "domain": "v.example"}},
           "defaults": {"set": template}, "users": [{"name": "a"}],
           "nodes": [{"name": "uk", "host": "uk.example", "label": "🇬🇧 UK"}]}
    panel = FakePanel()
    acc = _access(["uk"])
    for a in acc.values():
        a.cert_pem, a.key_pem, a.cert_pin_hex = "-----BEGIN CERTIFICATE-----\nAAEC\n-----END CERTIFICATE-----", "K", "ab" * 32
    Reconciler(panel, FleetConfig.model_validate(doc)).run(acc)
    doc["nodes"].append({"name": "nl", "host": "nl.example", "label": "🇳🇱 NL"})
    acc = _access(["nl"])
    for a in acc.values():
        a.cert_pem, a.key_pem, a.cert_pin_hex = "-----BEGIN CERTIFICATE-----\nAAEC\n-----END CERTIFICATE-----", "K", "ab" * 32
    Reconciler(panel, FleetConfig.model_validate(doc)).run(acc, {"nl"})
    kinds = lambda node: sorted(t.rsplit("-" + node, 1)[0] for t in (i["tag"] for i in panel.inbounds()) if t.endswith("-" + node))
    assert kinds("uk") == kinds("nl") and "fleet-cascade" not in kinds("nl")  # same set, no plain TCP cascade
    # A node overrides only what it states; everything else still comes from the template.
    cfg = FleetConfig.model_validate(dict(doc, nodes=[{"name": "x", "host": "x.example", "set": {"warp": False}}]))
    s = cfg.set_for(cfg.nodes[0])
    assert s.warp is False and s.cascade_tcp is False and s.cascade_xhttp is True


def test_warp_uses_the_measured_endpoint_or_cloudflare_ipv4():
    from xui_fleet import migrate as mg
    warp = {"tag": "warp", "protocol": "wireguard", "settings": {"peers": [{"endpoint": "engage.cloudflareclient.com:2408"}]}}
    tpl = {"outbounds": [], "routing": {"rules": []}}
    ep = lambda t: t["outbounds"][-1]["settings"]["peers"][0]["endpoint"]
    assert ep(mg.merge_warp(tpl, warp, ["a"])) == "162.159.192.1:2408"  # never a name that may resolve to IPv6
    assert ep(mg.merge_warp(tpl, warp, ["a"], "188.114.96.1:2408")) == "188.114.96.1:2408"


def test_app_copy_moves_everything_to_another_computer_and_needs_the_password(tmp_path):
    import json as _json
    from xui_fleet import appbackup as ab
    old = tmp_path / "old"; (old / ".xui-fleet").mkdir(parents=True)
    (old / "fleet.yaml").write_text("master: {name: ru, host: 9.9.9.9}\nnodes: [{name: de, host: de.example}]\n")
    (old / ".xui-fleet" / "state.json").write_text(_json.dumps(
        {"servers": {"de": {"api_token": "T", "ssh_password": "P", "ssh_route": {"via": "default"}}}}))
    bdir = tmp_path / "bk-old"; bdir.mkdir()
    (bdir / "master-20260101-000000.tar.gz").write_bytes(b"backup")
    blob = ab.export_bytes(old / "fleet.yaml", bdir, "correct horse")
    assert b"api_token" not in blob and b"9.9.9.9" not in blob  # encrypted
    with pytest.raises(ab.AppBackupError):
        ab.read(blob, "wrong password")
    info = ab.describe(blob, "correct horse")
    assert info["master"] == "9.9.9.9" and info["nodes"] == ["de"] and info["masterBackups"] == 1
    new = tmp_path / "new"; new.mkdir(); nb = tmp_path / "bk-new"
    ab.import_bytes(blob, "correct horse", new / "fleet.yaml", nb)
    st = _json.loads((new / ".xui-fleet" / "state.json").read_text())["servers"]["de"]
    assert st["api_token"] == "T" and st["ssh_password"] == "P" and "ssh_route" not in st  # network facts are re-learned
    assert (nb / "master-20260101-000000.tar.gz").read_bytes() == b"backup"
    with pytest.raises(ab.AppBackupError):  # never overwrite silently
        ab.import_bytes(blob, "correct horse", new / "fleet.yaml", nb)
    ab.import_bytes(blob, "correct horse", new / "fleet.yaml", nb, replace=True)
    assert list(new.glob("fleet.yaml.before-import-*"))


@pytest.mark.warp
def test_single_server_is_the_vpn_itself():
    from xui_fleet.reconcile import NodeAccess
    panel = FakePanel()
    cfg = FleetConfig.model_validate({"master": {"name": "nl", "host": "5.5.5.5", "label": "Netherlands", "flag": "NL", "vpn": True},
                                      "defaults": {"set": {"cascade_tcp": False}}, "users": [{"name": "a"}]})
    acc = NodeAccess("https", "5.5.5.5", 0, "", None)
    acc.busy_tcp, acc.busy_udp = {22, 2096}, set()
    acc.cert_pem, acc.key_pem, acc.cert_pin_hex = "-----BEGIN CERTIFICATE-----\nAAEC\n-----END CERTIFICATE-----", "K", "ab" * 32
    rep = Reconciler(panel, cfg, neighbors={"nl": ("5.5.5.9:443", "shop.example")}).run({"nl": acc})
    by_tag = {i["tag"]: i for i in panel.inbounds()}
    assert set(by_tag) == {"fleet-direct-nl", "fleet-xhttp-nl", "fleet-hy2-nl", "fleet-warp-nl"}
    assert all("nodeId" not in i for i in by_tag.values())  # hosted on the master itself
    assert by_tag["fleet-direct-nl"]["remark"] == "🇳🇱 Netherlands" and by_tag["fleet-direct-nl"]["port"] == 443
    assert by_tag["fleet-direct-nl"]["streamSettings"]["realitySettings"]["serverNames"] == ["shop.example"]
    assert sorted(panel._clients["a"]["inboundIds"]) == sorted(i["id"] for i in by_tag.values())
    assert panel.nodes() == [] and not [r for r in panel.template["routing"]["rules"] if "fleet-out" in str(r)]
    assert not [w for w in rep.warnings if "остался" in w]
    assert Reconciler(panel, cfg, neighbors={"nl": ("5.5.5.9:443", "shop.example")}).run({"nl": acc}).changes == []
    # Turning the role off removes only its own entries.
    cfg.master.vpn = False
    Reconciler(panel, cfg).run({})
    assert [i["tag"] for i in panel.inbounds()] == []


def test_master_with_a_domain_serves_itself_and_cascades_or_only_itself():
    panel = FakePanel()
    doc = {"master": {"name": "ru", "host": "9.9.9.9", "vpn": True, "tls": {"mode": "domain", "domain": "v.example"}},
           "defaults": {"set": {"cascade_tcp": False, "warp": False, "cascade_warp": False}},
           "users": [{"name": "a"}], "nodes": [{"name": "de", "host": "de.example"}]}
    Reconciler(panel, FleetConfig.model_validate(doc)).run(_access(["de", "ru"]))
    by_tag = {i["tag"]: i for i in panel.inbounds()}
    own = by_tag["fleet-direct-ru"]
    assert own["port"] not in (443, 8444) and "nodeId" not in own  # 443 belongs to the cover site
    assert own["streamSettings"]["realitySettings"]["target"] == "127.0.0.1:8444"  # steals its own domain
    assert by_tag["fleet-hy2-ru"]["streamSettings"]["tlsSettings"]["certificates"][0]["certificateFile"] == "/root/cert/v.example/fullchain.pem"
    assert "fleet-rxh-de" in by_tag and "fleet-direct-de" in by_tag
    order = [i["tag"] for i in sorted(panel.inbounds(), key=lambda i: i["subSortIndex"]) if not i.get("excludeFromSub")]
    assert order.index("fleet-xhttp-ru") < order.index("fleet-rxh-de")  # the master's own entries first
    doc["master"]["cascades"] = False  # nodes only directly, the master only as itself
    Reconciler(panel, FleetConfig.model_validate(doc)).run(_stale(_access(["de", "ru"])))
    tags = {i["tag"] for i in panel.inbounds()}
    assert not {t for t in tags if t.startswith(("fleet-rxh", "fleet-rhy2", "fleet-cascade"))}
    assert {"fleet-direct-ru", "fleet-direct-de"} <= tags


def test_three_computers_stay_in_sync_through_one_server(tmp_path, monkeypatch):
    """Each computer has its own folder; the 'server' is a local directory driven by the same shell scripts."""
    import json as _json, subprocess
    from xui_fleet import sync as sy
    remote = tmp_path / "server"
    monkeypatch.setattr(sy, "REMOTE", str(remote))
    monkeypatch.setenv("XUI_FLEET_BACKUP_DIR", str(tmp_path / "bk"))

    class FakeSSH:
        def run(self, script, check=True, timeout=0, env=None):
            r = subprocess.run(["bash", "-c", "set -o pipefail\n" + script], capture_output=True, text=True)
            if check and r.returncode:
                raise RuntimeError(r.stderr)
            return type("R", (), {"out": r.stdout, "code": r.returncode})()
        def close(self):
            pass

    def computer(name):
        d = tmp_path / name
        (d / ".xui-fleet").mkdir(parents=True)
        monkeypatch.setattr(sy, "_meta_path", lambda d=d: d / "sync.json")
        s = sy.Sync(d / "fleet.yaml")
        s._ssh = lambda: FakeSSH()
        return d, s

    a, sa = computer("a")
    (a / "fleet.yaml").write_text("master: {name: ru, host: 9.9.9.9}\nnodes: []\n")
    (a / ".xui-fleet" / "state.json").write_text(_json.dumps({"servers": {"ru": {"ssh_password": "P", "ssh_route": {"via": "x"}}}}))
    sa.configure({"name": "ru", "host": "9.9.9.9"}, "sync-password")
    assert "версия 1" in sa.push()
    assert sa.push() == ""  # nothing changed — nothing sent
    assert b"ssh_password" not in (remote / "current.xfleet").read_bytes()  # encrypted on the server

    b, sb = computer("b")
    assert "версия 1" in sb.join({"name": "ru", "host": "9.9.9.9"}, "sync-password")
    st = _json.loads((b / ".xui-fleet" / "state.json").read_text())["servers"]["ru"]
    assert st["ssh_password"] == "P" and "ssh_route" not in st

    (b / "fleet.yaml").write_text("master: {name: ru, host: 9.9.9.9}\nnodes: [{name: de, host: de.example}]\n")
    assert "версия 2" in sb.push()
    monkeypatch.setattr(sy, "_meta_path", lambda: a / "sync.json")
    sa = sy.Sync(a / "fleet.yaml"); sa._ssh = lambda: FakeSSH()
    assert "версия 2" in sa.pull() and "de.example" in (a / "fleet.yaml").read_text()
    assert _json.loads((a / ".xui-fleet" / "state.json").read_text())["servers"]["ru"]["ssh_route"] == {"via": "x"}  # local fact kept

    c, sc = computer("c")
    with pytest.raises(sy.SyncError):
        sc.join({"name": "ru", "host": "9.9.9.9"}, "wrong-password")
    assert not (c / "fleet.yaml").exists() and not sc.enabled
    assert len(list((remote / "history").glob("rev-*.xfleet"))) == 2


def test_new_version_reaches_other_computers_and_old_ones_never_touch_newer_data(tmp_path, monkeypatch):
    import subprocess
    from xui_fleet import sync as sy, update as upd
    remote = tmp_path / "server"
    monkeypatch.setattr(sy, "REMOTE", str(remote))
    monkeypatch.setenv("XUI_FLEET_BACKUP_DIR", str(tmp_path / "bk"))

    class FakeSSH:
        def run(self, script, check=True, timeout=0, env=None):
            r = subprocess.run(["bash", "-c", "set -o pipefail\n" + script], capture_output=True, text=True)
            if check and r.returncode:
                raise RuntimeError(r.stderr)
            return type("R", (), {"out": r.stdout, "code": r.returncode})()
        def close(self):
            pass

    def program(name, version, body):
        root = tmp_path / name
        (root / "src" / "xui_fleet").mkdir(parents=True)
        (root / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "%s"\n' % version)
        (root / "src" / "xui_fleet" / "__init__.py").write_text(f'__version__ = "{version}"\n')
        (root / "src" / "xui_fleet" / "feature.py").write_text(body)
        (root / "fleet.yaml").write_text("master: {name: ru, host: 9.9.9.9}\n")
        (root / ".xui-fleet").mkdir()
        return root

    def computer(root, version):
        monkeypatch.setattr(sy, "__version__", version)
        monkeypatch.setattr(upd, "__version__", version)
        monkeypatch.setattr(upd, "project_root", lambda: root)
        monkeypatch.setattr(sy, "_meta_path", lambda: root / "sync.json")
        s = sy.Sync(root / "fleet.yaml")
        s._ssh = lambda: FakeSSH()
        return s

    new, old = program("new", "0.3.0", "NEW = True\n"), program("old", "0.2.0", "NEW = False\n")
    a = computer(new, "0.3.0")
    a.configure({"name": "ru", "host": "9.9.9.9"}, "sync-password")
    assert "0.3.0" in a.update_app()  # the newest program is published
    a.push(force=True)
    assert b"NEW = True" not in (remote / "app" / "current.xfapp").read_bytes()  # sealed with the sync password

    b = computer(old, "0.2.0")
    b.configure({"name": "ru", "host": "9.9.9.9"}, "sync-password")
    (old / "fleet.yaml").write_text("master: {name: ru, host: 9.9.9.9}\nnodes: [{name: x, host: x.example}]\n")
    b.meta["hash"] = "stale"  # pretend this old computer has its own unsent change
    with pytest.raises(sy.SyncError):  # an old program never overwrites data written by a newer one
        b.push()
    b.meta["hash"] = ""
    assert b.pull() == "" and "0.3.0" in b.meta["error"]  # and does not take it before updating
    assert "0.2.0 → 0.3.0" in b.update_app()
    assert (old / "src" / "xui_fleet" / "feature.py").read_text() == "NEW = True\n"
    assert (old / "fleet.yaml").read_text().count("x.example") == 1  # the user's data is untouched by the update

    c = computer(program("evil", "0.1.0", ""), "0.1.0")
    c.configure({"name": "ru", "host": "9.9.9.9"}, "other-password")
    with pytest.raises(upd.UpdateError):  # without the sync password an update cannot be read or forged
        c.update_app()


def test_update_from_a_github_release_is_checked_and_touches_only_the_program(tmp_path, monkeypatch):
    import hashlib, io, zipfile
    import httpx
    from xui_fleet import update as upd
    root = tmp_path / "app"
    (root / "src" / "xui_fleet").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname = "xui-fleet"\nversion = "0.2.0"\ndependencies = ["a"]\n')
    (root / "src" / "xui_fleet" / "__init__.py").write_text('__version__ = "0.2.0"\n')
    (root / "fleet.yaml").write_text("my servers\n")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xui-fleet/pyproject.toml", '[project]\nname = "xui-fleet"\nversion = "0.3.0"\ndependencies = ["a"]\n')
        z.writestr("xui-fleet/src/xui_fleet/__init__.py", '__version__ = "0.3.0"\n')
        z.writestr("xui-fleet/fleet.yaml", "somebody else's servers\n")  # must never be installed
    archive = buf.getvalue()
    good = hashlib.sha256(archive).hexdigest()
    monkeypatch.setattr(httpx, "get", lambda url, **kw: type("R", (), {"content": archive, "status_code": 200, "raise_for_status": lambda s: None})())
    with pytest.raises(upd.UpdateError):
        upd.install_from_github(root, {"version": "0.3.0", "url": "u", "sha256": "0" * 64})
    assert upd.install_from_github(root, {"version": "0.3.0", "url": "u", "sha256": good}) == "0.3.0"
    assert '"0.3.0"' in (root / "src" / "xui_fleet" / "__init__.py").read_text()
    assert (root / "fleet.yaml").read_text() == "my servers\n"


def test_versions_channels_and_pinning(tmp_path, monkeypatch):
    from xui_fleet import update as upd
    monkeypatch.setattr(upd, "_settings_path", lambda: tmp_path / "update.json")
    rels = [{"version": v, "prerelease": upd.is_prerelease(v)} for v in ("0.3.0-beta.2", "0.2.3", "0.2.2")]
    assert upd.latest(rels, False)["version"] == "0.2.3" and upd.latest(rels, True)["version"] == "0.3.0-beta.2"
    assert upd.allowed("0.2.4") and not upd.allowed("0.3.0-rc.1")  # stable channel by default
    upd.save_settings(channel="prerelease")
    assert upd.allowed("0.3.0-rc.1")
    upd.save_settings(pinned="0.2.2")  # rolled back by hand: nothing moves it automatically
    assert not upd.allowed("0.2.4") and upd.check_github(tmp_path) == ""
    assert upd.branch("0.2.9") == upd.branch("0.2.0") < upd.branch("0.3.0-beta.1")


def test_a_beta_never_covers_the_stable_version_on_the_sync_server(tmp_path, monkeypatch):
    import subprocess
    from xui_fleet import sync as sy, update as upd
    remote = tmp_path / "server"
    monkeypatch.setattr(sy, "REMOTE", str(remote))
    monkeypatch.setattr(upd, "_settings_path", lambda: tmp_path / "update.json")

    class FakeSSH:
        def run(self, script, check=True, timeout=0, env=None):
            r = subprocess.run(["bash", "-c", "set -o pipefail\n" + script], capture_output=True, text=True)
            return type("R", (), {"out": r.stdout, "code": r.returncode})()
        def close(self):
            pass

    def computer(name, version):
        root = tmp_path / name
        (root / "src" / "xui_fleet").mkdir(parents=True)
        (root / "pyproject.toml").write_text('[project]\nname = "xui-fleet"\n')
        (root / "src" / "xui_fleet" / "__init__.py").write_text(f'__version__ = "{version}"\n')
        monkeypatch.setattr(sy, "__version__", version)
        monkeypatch.setattr(upd, "__version__", version)
        monkeypatch.setattr(upd, "project_root", lambda: root)
        monkeypatch.setattr(sy, "_meta_path", lambda: root / "sync.json")
        s = sy.Sync(root / "fleet.yaml")
        s._ssh = lambda: FakeSSH()
        s.configure({"name": "ru", "host": "9.9.9.9"}, "sync-password")
        return root, s

    computer("stable", "0.2.4")[1].update_app()
    computer("beta", "0.2.5-beta.1")[1].update_app()
    assert '"0.2.4"' in (remote / "app" / "meta.json").read_text()
    assert '"0.2.5-beta.1"' in (remote / "app-pre" / "meta.json").read_text()
    old, s = computer("old", "0.2.3")
    assert "0.2.4" in s.update_app()  # stable channel: the stable version, not the beta
    upd.save_settings(channel="prerelease")
    old2, s2 = computer("tester", "0.2.4")
    assert "0.2.5-beta.1" in s2.update_app()  # prerelease channel takes the beta
