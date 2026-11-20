import copy

import pytest
import yaml
from pydantic import ValidationError

from xui_fleet import xrayconf as xc
from xui_fleet.cli import EXAMPLE, _drop_node_from_yaml
from xui_fleet.config import FleetConfig, append_node, load_config
from xui_fleet.reconcile import Reconciler, Report, _free_port


def _bash() -> str:
    """The shell that plays the sync server. A bare "bash" on Windows starts WSL (System32 comes first), which
    neither sees C:/ paths nor this Python's files: use Git Bash there, or skip."""
    import os
    import shutil
    if os.name != "nt":
        return "bash"
    for found in [shutil.which("bash"), r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe"]:
        if found and "system32" not in found.lower() and os.path.exists(found):
            return found
    pytest.skip("нужен Git Bash: тест синхронизации запускает скрипты сервера в bash")


@pytest.fixture(autouse=True)
def _update_settings(tmp_path, monkeypatch):
    """Never touch this computer's real update settings; tests that install expect automatic updates on."""
    from xui_fleet import update as upd
    path = tmp_path / "update-settings.json"
    path.write_text('{"auto": true}')
    monkeypatch.setattr(upd, "_settings_path", lambda: path)


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

    hosts: list = []

    def post(self, path, json_body=None, form=None):
        """3x-ui hosts API: /hosts/list returns one object per group (hosts[] + inboundIds[]), as the real panel does."""
        import secrets as _s
        body = json_body or {}
        if path == "/hosts/add" or path.startswith("/hosts/update/"):
            gid = path.rsplit("/", 1)[1] if "update" in path else _s.token_hex(4)
            self.hosts = [h for h in self.hosts if h["groupId"] != gid]
            self.hosts.append(dict(body, groupId=gid, snis=[body.get("sni", "")]))
            self.calls.append("hosts")
            return [h for h in self.hosts if h["groupId"] == gid]
        if path.startswith("/hosts/del/"):
            self.hosts = [h for h in self.hosts if h["groupId"] != path.rsplit("/", 1)[1]]
            self.calls.append("hosts")
            return None
        raise AssertionError(path)

    def get(self, path):
        if path == "/hosts/list":
            return copy.deepcopy(self.hosts)
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

    def update_client(self, email, fields):
        self._clients[email].update(fields)
        self.calls.append("update_client")

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
    import os
    if os.name != "nt":  # Windows has no Unix modes (chmod only sets read-only); the user's profile folder guards it
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
    # «via RU · Hysteria2» goes to the node over the node's own Hysteria2 entry, not its Reality entry
    assert rules["fleet-rwarp-de"] == "fleet-out-warp-de" and rules["fleet-rhy2-de"] == "fleet-out-de"  # no checked Hysteria2 leg: Reality
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
            assert st["realitySettings"]["serverNames"] == ["b.example", "a.example"]  # both, the main one first
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
    monkeypatch.setattr(sy, "REMOTE", remote.as_posix())
    monkeypatch.setenv("XUI_FLEET_BACKUP_DIR", str(tmp_path / "bk"))

    class FakeSSH:
        def run(self, script, check=True, timeout=0, env=None):
            r = subprocess.run([_bash(), "-c", "set -o pipefail\n" + script], capture_output=True, text=True)
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
    monkeypatch.setattr(sy, "REMOTE", remote.as_posix())
    monkeypatch.setenv("XUI_FLEET_BACKUP_DIR", str(tmp_path / "bk"))

    class FakeSSH:
        def run(self, script, check=True, timeout=0, env=None):
            r = subprocess.run([_bash(), "-c", "set -o pipefail\n" + script], capture_output=True, text=True)
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
    rels = [{"version": v, "prerelease": upd.is_prerelease(v)} for v in ("0.3.0-beta.2", "0.2.3", "0.2.2")]
    monkeypatch.setattr(upd, "releases", lambda repo="", fresh=False: rels)  # never the real GitHub in tests
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
    monkeypatch.setattr(sy, "REMOTE", remote.as_posix())

    class FakeSSH:
        def run(self, script, check=True, timeout=0, env=None):
            r = subprocess.run([_bash(), "-c", "set -o pipefail\n" + script], capture_output=True, text=True)
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


def test_release_list_is_refetched_when_it_does_not_know_the_installed_version(monkeypatch):
    import httpx
    from xui_fleet import update as upd
    calls = []
    def fake(url, **kw):
        calls.append(url)
        tags = ["v0.2.5"] if len(calls) == 1 else ["v0.2.6", "v0.2.5"]
        data = [{"tag_name": t, "prerelease": False, "published_at": "", "body": "", "html_url": "",
                 "assets": [{"name": f"xui-fleet-{t[1:]}.zip", "browser_download_url": "u"}]} for t in tags]
        return type("R", (), {"status_code": 200, "json": lambda s: data})()
    monkeypatch.setattr(httpx, "get", fake)
    monkeypatch.setattr(upd, "__version__", "0.2.6")
    upd._cache.clear()
    assert [r["version"] for r in upd.releases()] == ["0.2.5"]  # GitHub had not published 0.2.6 yet
    assert upd.releases()[0]["version"] == "0.2.6" and len(calls) == 2  # the next look does not trust that list
    assert upd.releases()[0]["version"] == "0.2.6" and len(calls) == 2  # a list that knows it is cached


def test_merge3_keeps_both_sides_changes():
    from xui_fleet.sync import merge3
    base = {"nodes": [{"name": "a", "host": "1"}, {"name": "b", "host": "2"}], "users": [{"name": "u1"}], "master": {"label": "RU"}}
    local = {"nodes": [{"name": "a", "host": "1"}, {"name": "b", "host": "2"}, {"name": "new-here", "host": "3"}],
             "users": [{"name": "u1"}], "master": {"label": "RU"}}
    remote = {"nodes": [{"name": "a", "host": "1", "label": "UK"}], "users": [{"name": "u1"}, {"name": "u2"}], "master": {"label": "RU"}}
    c = []
    m = merge3(base, local, remote, "", c)
    assert [n["name"] for n in m["nodes"]] == ["a", "new-here"]  # b removed there, new-here added here
    assert m["nodes"][0]["label"] == "UK" and [u["name"] for u in m["users"]] == ["u1", "u2"] and c == []
    c = []
    m = merge3(base, dict(base, master={"label": "Here"}), dict(base, master={"label": "There"}), "", c)
    assert m["master"]["label"] == "Here" and c == ["master.label"]  # same field on both sides: this computer wins


def test_a_server_added_on_an_old_version_survives_the_new_version(tmp_path, monkeypatch):
    """Old computer adds a server while the newer branch already wrote its data: nothing is lost."""
    import json as _json, subprocess
    from xui_fleet import sync as sy
    remote = tmp_path / "server"
    monkeypatch.setattr(sy, "REMOTE", remote.as_posix())
    monkeypatch.setenv("XUI_FLEET_BACKUP_DIR", str(tmp_path / "bk"))

    class FakeSSH:
        def run(self, script, check=True, timeout=0, env=None):
            r = subprocess.run([_bash(), "-c", "set -o pipefail\n" + script], capture_output=True, text=True)
            if check and r.returncode:
                raise RuntimeError(r.stderr)
            return type("R", (), {"out": r.stdout, "code": r.returncode})()
        def close(self):
            pass

    def computer(name, version):
        d = tmp_path / name
        (d / ".xui-fleet").mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(sy, "__version__", version)
        monkeypatch.setattr(sy, "_meta_path", lambda d=d: d / "sync.json")
        s = sy.Sync(d / "fleet.yaml")
        s._ssh = lambda: FakeSSH()
        return d, s

    start = "master: {name: ru, host: 9.9.9.9}\nnodes:\n- {name: uk, host: uk.example}\nusers:\n- {name: alice}\n"
    new, sn = computer("new", "0.3.0")
    (new / "fleet.yaml").write_text(start)
    (new / ".xui-fleet" / "state.json").write_text(_json.dumps({"servers": {"uk": {"api_token": "T1"}}}))
    sn.configure({"name": "ru", "host": "9.9.9.9"}, "sync-password")
    sn.push(force=True)

    old, so = computer("old", "0.2.7")
    so.join({"name": "ru", "host": "9.9.9.9"}, "sync-password")  # both saw rev 1
    # The newer version changes things (rev 2) ...
    monkeypatch.setattr(sy, "__version__", "0.3.0")
    monkeypatch.setattr(sy, "_meta_path", lambda: new / "sync.json")
    sn = sy.Sync(new / "fleet.yaml"); sn._ssh = lambda: FakeSSH()
    (new / "fleet.yaml").write_text(start.replace("users:\n- {name: alice}\n", "users:\n- {name: alice}\n- {name: bob}\n"))
    assert "версия 2" in sn.push()
    # ... while the old one adds a server: it cannot send it over newer data, but keeps it.
    monkeypatch.setattr(sy, "__version__", "0.2.7")
    monkeypatch.setattr(sy, "_meta_path", lambda: old / "sync.json")
    so = sy.Sync(old / "fleet.yaml"); so._ssh = lambda: FakeSSH()
    (old / "fleet.yaml").write_text(start.replace("nodes:\n- {name: uk, host: uk.example}\n",
                                                  "nodes:\n- {name: uk, host: uk.example}\n- {name: de, host: de.example}\n"))
    with pytest.raises(sy.SyncError):
        so.push()
    assert "de.example" in (old / "fleet.yaml").read_text()  # still here
    # After the old computer is updated, the change is merged with the newer data instead of overwriting it.
    monkeypatch.setattr(sy, "__version__", "0.3.0")
    so = sy.Sync(old / "fleet.yaml"); so._ssh = lambda: FakeSSH()
    msg = so.push()
    assert "объединено" in msg
    text = (old / "fleet.yaml").read_text()
    assert "de.example" in text and "bob" in text and "uk.example" in text
    monkeypatch.setattr(sy, "_meta_path", lambda: new / "sync.json")
    sn = sy.Sync(new / "fleet.yaml"); sn._ssh = lambda: FakeSSH()
    assert "версия 3" in sn.pull() and "de.example" in (new / "fleet.yaml").read_text()
    assert _json.loads((new / ".xui-fleet" / "state.json").read_text())["servers"]["uk"]["api_token"] == "T1"



def test_by_default_a_new_version_is_offered_not_installed(tmp_path, monkeypatch):
    import httpx
    from xui_fleet import update as upd
    upd.save_settings(auto=False)
    monkeypatch.setattr(upd, "__version__", "0.2.7")
    monkeypatch.setattr(upd, "releases", lambda repo="", fresh=False: [{"version": "0.2.8", "prerelease": False},
                                                                      {"version": "0.3.0-beta.1", "prerelease": True}])
    monkeypatch.setattr(upd, "install_from_github", lambda root, info: pytest.fail("installed without asking"))
    assert upd.check_github(tmp_path) == "" and upd.settings()["available"] == "0.2.8"  # offered, beta not
    upd.save_settings(auto=True)
    monkeypatch.setattr(upd, "install_from_github", lambda root, info: info["version"])
    assert "0.2.8" in upd.check_github(tmp_path)


def test_sync_remembers_the_host_key_per_address(tmp_path, monkeypatch):
    """A first attempt with a wrong address must not block the right one afterwards."""
    from xui_fleet import sync as sy
    seen = {}
    class FakeSSH:
        def __init__(self, name, host, cfg, state):
            seen.setdefault("names", []).append(name)
        def connect(self):
            return self
    monkeypatch.setattr(sy, "SSH", FakeSSH)
    monkeypatch.setattr(sy, "_meta_path", lambda: tmp_path / "sync.json")
    s = sy.Sync(tmp_path / "fleet.yaml")
    for host in ("198.51.100.10", "203.0.113.10"):
        s.meta = {"server": {"name": "sync", "host": host}, "password": "x" * 8}
        s._ssh()
    assert seen["names"] == ["198.51.100.10:22", "203.0.113.10:22"]



def test_reserve_domain_through_3xui_hosts_switches_without_touching_entries():
    panel = FakePanel()
    panel.hosts = []
    panel._settings = {"subEnable": True, "subPort": 2096, "subPath": "/s/", "subCertFile": "/c", "webCertFile": "/c",
                       "remarkTemplate": "{{INBOUND}}-{{EMAIL}}"}
    doc = {"master": {"name": "ru", "host": "9.9.9.9", "tls": {"mode": "domain", "domain": "a.example", "reserve_domains": ["b.example"]}},
           "users": [{"name": "x"}], "nodes": [{"name": "de", "host": "de.example", "set": {"cascade_tcp": False}}]}
    Reconciler(panel, FleetConfig.model_validate(doc)).run(_access(["de"]))
    master = {i["tag"]: i for i in panel.inbounds() if not i.get("nodeId")}
    assert master["fleet-rxh-de"]["streamSettings"]["realitySettings"]["serverNames"] == ["a.example", "b.example"]
    groups = {h["tags"][0]: h for h in panel.hosts}
    assert groups["xui-fleet-main"]["isDisabled"] and groups["xui-fleet-reserve"]["isDisabled"]  # main mode: links as before
    assert len(panel.hosts) == 2  # one group per address, as the real panel lists them
    covered = set(groups["xui-fleet-reserve"]["inboundIds"])
    assert covered == {i["id"] for t, i in master.items() if t.startswith(("fleet-rxh", "fleet-rhy2", "fleet-rwarp", "fleet-cascade"))}
    keys = {t: i["streamSettings"].get("realitySettings", {}).get("privateKey") for t, i in master.items()}
    for mode, main_on, reserve_on in (("both", True, True), ("reserve", False, True), ("main", False, False)):
        doc["master"]["tls"]["address_mode"] = mode
        Reconciler(panel, FleetConfig.model_validate(doc)).run(_stale(_access(["de"])))
        on = {h["tags"][0] for h in panel.hosts if not h["isDisabled"]}
        assert ("xui-fleet-main" in on, "xui-fleet-reserve" in on) == (main_on, reserve_on), mode
        assert {t: i["streamSettings"].get("realitySettings", {}).get("privateKey")
                for t, i in ((i["tag"], i) for i in panel.inbounds() if not i.get("nodeId"))} == keys  # entries untouched
    assert "{{INBOUND}}{{HOST}}" in panel.settings()["remarkTemplate"]  # the two links are told apart by name
    assert Reconciler(panel, FleetConfig.model_validate(doc)).run(_stale(_access(["de"]))).changes == []
    doc["master"]["tls"]["address_mode"] = "both"
    Reconciler(panel, FleetConfig.model_validate(doc)).run(_stale(_access(["de"])))
    assert Reconciler(panel, FleetConfig.model_validate(doc)).run(_stale(_access(["de"]))).changes == []  # idempotent when on


def test_backups_keep_the_newest_and_one_per_day(tmp_path):
    from xui_fleet import backup as bk
    names = [f"master-202611{d:02d}-{h:02d}0000.tar.gz" for d in range(1, 16) for h in (9, 12, 18)]
    for n in names:
        (tmp_path / n).write_bytes(b"x")
    bk.rotate(tmp_path, keep=10, days=30)
    left = sorted(p.name for p in tmp_path.glob("master-*"))
    newest = sorted(names, reverse=True)[:10]
    assert set(newest) <= set(left)  # a burst of changes keeps its recent restore points
    days = {n[7:15] for n in left}
    assert len(days) == 15  # and every day before it keeps its last state
    assert all(f"master-202611{d:02d}-180000.tar.gz" in left for d in range(1, 16))
    assert len(left) == 10 + 11  # 10 newest span days 12..15; days 1..11 keep one each


def test_link_check_uses_its_own_service_client():
    panel = FakePanel()
    cfg = _cfg([{"name": "de", "host": "de.example"}])
    report = Reconciler(panel, cfg).run(_access(["de"]))
    tags = {i["tag"]: i for i in panel.inbounds()}
    assert sorted(panel._clients["fleet-check"]["inboundIds"]) == sorted(i["id"] for i in tags.values())
    assert "fleet-check" not in report.subscriptions and "alice" in report.subscriptions
    assert cfg.clients.service_emails == {"fleet-bridge", "fleet-check"}


def test_history_turns_counters_into_days_and_probes_into_uptime(tmp_path, monkeypatch):
    import time as _t
    from xui_fleet import history as hs
    from xui_fleet import service
    monkeypatch.setattr(service, "APP_DIR", tmp_path)
    h = hs.History(tmp_path / "fleet.yaml")
    now = _t.time()
    for k, lat in enumerate([20, 22, None, 25]):  # one probe failed: 75% up
        h.add_health({"t": int(now - (3 - k) * 300), "n": {"uk": lat}, "m": 1})
    got = h.health(24)["nodes"]["uk"]
    assert got["uptime"] == 75.0 and got["median"] == 22 and [p[1] for p in got["points"]] == [20, 22, None, 25]
    # traffic: cumulative counters; a reset in the panel (counter went down) counts from zero
    # the node's own entries also carry what came via RU (the bridge): direct is the rest
    for k, (alice, ru, d) in enumerate([(100, 10, 15), (160, 30, 40), (40, 35, 50)]):
        h.add_traffic({"t": int(now - (2 - k) * 60), "u": {"alice": alice}, "r": {"uk": {"ru": ru, "d": d, "rb": ru}}})
    tr = h.traffic(7)
    assert sum(tr["users"]["alice"]) == 60 + 40  # +60, then reset to 40
    assert sum(tr["routes"]["uk"]["ru"]) == 25 and sum(tr["routes"]["uk"]["d"]) == (25 - 20) + (10 - 5)
    sample = hs.traffic_sample(
        [{"email": "alice", "traffic": {"up": 1, "down": 2}}, {"email": "fleet-check", "traffic": {"up": 9, "down": 9}}],
        [{"tag": "fleet-rxh-uk", "up": 5, "down": 5}, {"tag": "fleet-xhttp-uk", "nodeId": 3, "up": 1, "down": 1},
         {"tag": "fleet-warpup-uk", "nodeId": 3, "up": 50, "down": 50}, {"tag": "mine", "up": 7, "down": 7}],
        {3: "uk"}, {"fleet-check", "fleet-bridge"})
    assert sample["u"] == {"alice": 3} and sample["r"] == {"uk": {"ru": 10, "d": 2, "rb": 10}}  # service traffic left out


def test_health_counts_a_node_with_xray_down_as_a_down_route():
    from xui_fleet import history as hs
    row = hs.health_sample([{"name": "uk", "status": "online", "latencyMs": 20},
                            {"name": "de", "status": "online", "latencyMs": 30, "xrayError": "failed to start"},
                            {"name": "fr", "status": "offline"}, {"name": "off", "enable": False}], {"xray": {"state": "running"}})
    assert row["n"] == {"uk": 20, "de": None, "fr": None} and row["m"] == 1


def test_history_file_is_pruned_about_once_a_day_not_on_every_sample(tmp_path):
    from xui_fleet import history as hs
    path, day, now = tmp_path / "health.jsonl", 86400, 2_000_000_000
    path.write_text("".join(f'{{"t":{now - k * 300}}}\n' for k in range(14 * 288, -1, -1)), encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    hs._prune(path, 14, now=now)  # holds exactly 14 days: nothing to drop, nothing rewritten
    assert path.read_text(encoding="utf-8") == before
    path.write_text('{"t":%d}\n' % (now - 16 * day) + before, encoding="utf-8")  # a line past the limit and a day
    hs._prune(path, 14, now=now)
    rows = hs._read(path, 0)
    assert rows[0]["t"] >= now - 14 * day and len(rows) == 14 * 288 + 1


def test_reserve_only_mode_follows_the_domains(tmp_path):
    import yaml
    from xui_fleet.config import set_master_domain
    path = tmp_path / "fleet.yaml"

    def write(mode, reserves=("b.example",)):
        tls = {"mode": "domain", "domain": "a.example", "reserve_domains": list(reserves), "address_mode": mode}
        path.write_text(yaml.safe_dump({"master": {"name": "ru", "host": "9.9.9.9", "tls": tls}, "nodes": []}), encoding="utf-8")

    def mode():
        return (yaml.safe_load(path.read_text(encoding="utf-8"))["master"]["tls"]).get("address_mode", "main")
    write("reserve")
    set_master_domain(path, "a.example", ["c.example"])  # another reserve: still only the reserve
    assert mode() == "reserve"
    write("reserve")
    set_master_domain(path, "b.example", ["a.example"])  # switched to the reserve: a.example (blocked) is the reserve now
    assert mode() == "main"
    write("reserve")
    set_master_domain(path, "a.example", [])  # reserve removed: links cannot stay on it
    assert mode() == "main"
    write("both")
    set_master_domain(path, "a.example", [])
    assert mode() == "main"
    write("both")
    set_master_domain(path, "b.example", ["a.example"])  # both links stay meaningful
    assert mode() == "both"


def test_incy_gets_the_same_ru_profile_and_its_time_only_moves_on_change(monkeypatch):
    from xui_fleet import reconcile as rc
    panel = FakePanel()
    cfg = _cfg([{"name": "de", "host": "de.example"}])
    monkeypatch.setattr(rc, "_now", lambda: 1_800_000_000)
    Reconciler(panel, cfg).run(_access(["de"]))
    s = panel.settings()
    happ, incy = xc.profile_from_link(s["subRoutingRules"]), xc.profile_from_link(s["subIncyRoutingRules"])
    assert s["subIncyEnableRouting"] is True and s["subIncyRoutingRules"].startswith("incy://routing/onadd/")
    assert {k: v for k, v in happ.items() if k != "RouteOrder"} == {k: v for k, v in incy.items() if k != "LastUpdated"}
    assert incy["LastUpdated"] == "1800000000"
    monkeypatch.setattr(rc, "_now", lambda: 1_900_000_000)
    assert Reconciler(panel, cfg).run(_stale(_access(["de"]))).changes == []  # same rules: INCY is not asked to reload
    doc = cfg.model_dump()
    doc["routing"]["extra_direct"] = ["domain:example.org"]
    Reconciler(panel, FleetConfig.model_validate(doc)).run(_stale(_access(["de"])))
    assert xc.profile_from_link(panel.settings()["subIncyRoutingRules"])["LastUpdated"] == "1900000000"
    doc["routing"]["client_profile"] = False
    Reconciler(panel, FleetConfig.model_validate(doc)).run(_stale(_access(["de"])))
    assert panel.settings()["subIncyRoutingRules"] == "" and panel.settings()["subIncyEnableRouting"] is False


def test_allowed_apps_setting(tmp_path):
    from xui_fleet.config import set_allowed_apps
    path = tmp_path / "fleet.yaml"
    path.write_text("master: {name: ru, host: 9.9.9.9}\nnodes: []\n", encoding="utf-8")
    set_allowed_apps(path, ["happ", "incy"])
    assert load_config(path).clients.allowed_apps == ["happ", "incy"]
    set_allowed_apps(path, [])
    assert "clients" not in yaml.safe_load(path.read_text(encoding="utf-8")) and load_config(path).clients.allowed_apps == []
    with pytest.raises(ValidationError):
        FleetConfig.model_validate({"master": {"name": "ru", "host": "9.9.9.9"}, "clients": {"allowed_apps": ["v2rayng"]}})


def test_sub_gate_conf_lets_only_the_apps_through_and_tells_the_others():
    import base64, re
    from urllib.parse import unquote
    from xui_fleet import provision as pv
    conf = pv.sub_gate_conf(2096, 12096, "/root/cert/a/fullchain.pem", "/root/cert/a/privkey.pem", ["happ", "incy"],
                            ["203.0.113.5", "10.0.0.2"], ipv6=True)
    assert "listen 2096 ssl;" in conf and "listen [::]:2096 ssl;" in conf and "proxy_pass https://127.0.0.1:12096;" in conf
    assert "~*(^|[^a-z0-9])happ([^a-z0-9]|$) 1;" in conf and "~*(^|[^a-z0-9])incy([^a-z0-9]|$) 1;" in conf
    assert "\\" not in conf.split("location @xf_sub_app_denied")[0]  # no backslashes for nginx to misread
    for ip in ("127.0.0.1", "::1", "203.0.113.5", "10.0.0.2"):
        assert f"    {ip} 1;" in conf  # the master itself (link check) is never turned away
    assert "proxy_set_header X-Real-IP $remote_addr;" in conf  # HWID and IP limits still see the user
    for ua, ok in (("Happ/3.2.1", True), ("INCY/1.4 (iOS)", True), ("v2rayNG/1.8.5", False), ("Hiddify", False),
                   ("happy-client", False)):
        pats = re.findall(r"~\*(\S+) 1;", conf.split("map $remote_addr")[0])
        assert any(re.search(p, ua, re.I) for p in pats) == ok, ua
    body = re.search(r'location @xf_sub_app_denied \{.*?return 200 "([^"]+)";', conf, re.S).group(1)
    lines = base64.b64decode(body).decode().splitlines()
    assert len(lines) == 2 and all(line.startswith("vless://") for line in lines)
    assert "не поддерживается" in unquote(lines[0]) and "Happ или INCY" in unquote(lines[1])
    assert re.search(r'add_header announce "base64:[A-Za-z0-9+/=]+" always;', conf)
    # the browser page is its own file (a `return` string has a length limit nginx enforces)
    assert "root /etc/nginx/xui-fleet-subpage;" in conf and "rewrite ^ /index.html break;" in conf
    page = pv.unsupported_page(["happ", "incy"])
    assert '"' not in page and "happ.su" in page
    # nginx puts nothing into the page: it reads its own address in the browser, so no request data is echoed
    assert "$" not in page and "\\" not in page
    assert "data-scheme='happ://add/'" in page and "data-scheme='incy://import/'" in page and "location.href" in page
    assert "Установите приложение" in page and "Скопировать ссылку" in page  # the steps a newcomer needs
    named = pv.unsupported_page(["happ"], 2096, title='Мой "VPN" $x')
    assert "<h1>Мой &quot;VPN&quot; x</h1>" in named and '"' not in named and "$" not in named  # the name cannot break nginx
    assert "fallback-url" not in conf  # one domain: nowhere to fall back to
    two = pv.sub_gate_conf(2096, 12096, "/c", "/k", ["happ", "incy"], [], ipv6=False, domains=["a.example", "b.example"])
    assert "    a.example b.example;\n    b.example a.example;" in two  # each domain points at the other
    assert 'default "https://$xf_sub_alt:2096";' in two
    assert "add_header fallback-url $xf_sub_fallback always;" in two and "add_header profile-web-page-url $xf_sub_fallback always;" in two
    path_re = re.search(r'"~\^\(\?<xfp>(.+?)\)\$" \$xfp;', two).group(1)
    for uri, ok in (("/s/abc123", True), ("/s/abc-1_2/x", True), ("/s/abc?x=<script>", False), ("/s/%3Cb%3E", False)):
        assert bool(re.fullmatch(path_re, uri)) == ok, uri
    fb = re.search(r'"~(\^https\?://\[\^/\]\+/\.)"', two).group(1)
    assert re.match(fb, "https://b.example:2096/s/abc") and not re.match(fb, "/s/abc") and not re.match(fb, "https://b.example:2096")
    plain = pv.sub_gate_conf(2096, 12096, "", "", ["happ"], [], ipv6=False)
    assert "listen 2096;" in plain and "[::]" not in plain and "proxy_pass http://127.0.0.1:12096;" in plain and "incy" not in plain.split("server {")[0]


def _gate_fleet(tmp_path, apps, monkeypatch, reload_ok=True):
    from xui_fleet import provision as pv
    from xui_fleet.engine import Fleet
    from xui_fleet.state import State
    calls = []
    fail = {}
    monkeypatch.setattr(pv, "sub_gate_facts", lambda ssh: {"ips": ["203.0.113.5"], "ipv6": False, "free": 12096})
    written = {}

    def write(ssh, conf, page=""):
        if fail.get("write"):
            raise RuntimeError("nginx -t: too long parameter")
        calls.append(("write", conf))
        changed, written["conf"] = written.get("conf") != conf, conf
        return changed
    monkeypatch.setattr(pv, "write_sub_gate", write)
    monkeypatch.setattr(pv, "reload_nginx_on", lambda ssh, port: calls.append(("reload", port)) or (reload_ok and not fail.get("nginx")))
    monkeypatch.setattr(pv, "remove_sub_gate", lambda ssh: calls.append(("remove",)) or True)
    fleet = Fleet.__new__(Fleet)
    fleet.cfg = FleetConfig.model_validate({"master": {"name": "ru", "host": "203.0.113.5",
                                                       "tls": {"mode": "domain", "domain": "a.example"}},
                                            "clients": {"allowed_apps": apps}})
    fleet.state = State(tmp_path / "state.json")
    fleet.log = lambda _m: None
    fleet.fail = fail
    return fleet, calls


def test_sub_gate_moves_3xui_behind_nginx_and_back(tmp_path, monkeypatch):
    from xui_fleet.engine import public_sub_port
    panel = FakePanel()
    panel._settings = {"subEnable": True, "subPort": 2096, "subPath": "/s/", "subCertFile": "/c", "subKeyFile": "/k"}
    fleet, calls = _gate_fleet(tmp_path, ["happ", "incy"], monkeypatch)
    assert fleet.sub_gate_drift(panel.settings())
    assert "только Happ или INCY" in fleet.ensure_sub_gate(panel, ssh=None)
    s = panel.settings()
    assert (s["subListen"], s["subPort"], s["subURI"]) == ("127.0.0.1", 12096, "https://a.example:2096/s/")
    assert public_sub_port(s) == 2096 and fleet.sub_gate_drift(s) is None
    assert [c[0] for c in calls] == ["write", "reload"] and ("reload", 2096) in calls
    assert fleet.state.get("ru", "sub_gate")["port"] == 2096
    calls.clear()
    assert fleet.ensure_sub_gate(panel, ssh=None) is None and [c[0] for c in calls] == ["write"]  # in place: no restart, no reload
    fleet.cfg = FleetConfig.model_validate({"master": fleet.cfg.master.model_dump()})
    assert fleet.sub_gate_drift(panel.settings())
    calls.clear()
    fleet.ensure_sub_gate(panel, ssh=None)
    s = panel.settings()
    assert (s["subListen"], s["subPort"], s["subURI"]) == ("", 2096, "") and calls == [("remove",)]
    assert fleet.state.get("ru", "sub_gate") is None and fleet.ensure_sub_gate(panel, ssh=None) is None


def test_sub_gate_rolls_back_when_nginx_cannot_take_the_port(tmp_path, monkeypatch):
    panel = FakePanel()
    panel._settings = {"subEnable": True, "subPort": 2096, "subPath": "/s/", "subCertFile": "/c", "subKeyFile": "/k"}
    before = panel.settings()
    fleet, calls = _gate_fleet(tmp_path, ["happ", "incy"], monkeypatch, reload_ok=False)
    with pytest.raises(Exception):
        fleet.ensure_sub_gate(panel, ssh=None)
    assert panel.settings() == before and ("remove",) in calls and fleet.state.get("ru", "sub_gate") is None


def test_github_rate_limit_is_explained(monkeypatch):
    import time as _t
    import httpx
    from xui_fleet import update as upd
    reset = int(_t.time()) + 600
    fake = type("R", (), {"status_code": 403, "headers": httpx.Headers({"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)})})()
    monkeypatch.setattr(httpx, "get", lambda url, **kw: fake)
    upd._cache.clear()
    with pytest.raises(upd.UpdateError) as e:
        upd.releases("owner/repo", fresh=True)
    msg = str(e.value)
    assert "лимит запросов" in msg and _t.strftime("%H:%M", _t.localtime(reset)) in msg and "через 10 мин" in msg
    assert upd.github_refusal(404, {}) == "GitHub ответил 404"
    assert upd.github_refusal(403, {}) == "GitHub ответил 403"  # a plain refusal is not called a rate limit
    assert "Повторите через 2 мин" in upd.github_refusal(429, {"Retry-After": "120"})


def test_sub_gate_taken_over_without_moving_3xui_again(tmp_path, monkeypatch):
    """The gate was set up elsewhere (this computer has no record of it): 3x-ui stays on its port, no restart."""
    panel = FakePanel()
    panel._settings = {"subEnable": True, "subPort": 12096, "subListen": "127.0.0.1", "subURI": "https://a.example:2096/s/",
                       "subPath": "/s/", "subCertFile": "/c", "subKeyFile": "/k"}
    fleet, calls = _gate_fleet(tmp_path, ["happ", "incy"], monkeypatch)
    monkeypatch.setattr("xui_fleet.provision.sub_gate_facts", lambda ssh: {"ips": [], "ipv6": False, "free": 12097})
    panel.calls = []
    fleet.ensure_sub_gate(panel, ssh=None)
    assert panel.settings()["subPort"] == 12096 and "update_settings" not in panel.calls
    assert fleet.state.get("ru", "sub_gate") == {"port": 2096, "internal": 12096, "uri": "https://a.example:2096/s/", "apps": ["happ", "incy"]}


def test_new_url_only_for_the_domain_the_master_moved_from():
    import re
    from xui_fleet import provision as pv
    plain = pv.sub_gate_conf(2096, 12096, "/c", "/k", ["happ", "incy"], [], ipv6=False, domains=["b.example", "a.example"])
    assert "new-url" not in plain  # no move: nobody is redirected (a reserve copy is never pulled back)
    moved = pv.sub_gate_conf(2096, 12096, "/c", "/k", ["happ", "incy"], [], ipv6=False, domains=["b.example", "a.example"],
                             moved_from="a.example")
    assert '    a.example "https://b.example:2096";' in moved and "b.example \"https" not in moved.split("map $host $xf_sub_moved")[1].split("}")[0].replace('a.example "https://b.example', "")
    assert "add_header new-url $xf_sub_new_url always;" in moved
    assert "new-url" not in pv.sub_gate_conf(2096, 12096, "/c", "/k", ["happ"], [], ipv6=False, domains=["b.example", "a.example"],
                                             moved_from="b.example")  # moved to itself: nothing to say
    assert "new-url" not in pv.sub_gate_conf(2096, 12096, "/c", "/k", ["happ"], [], ipv6=False, domains=["b.example"],
                                             moved_from="gone.example")  # a domain no longer served


def test_vpn_name_goes_to_the_panel_title(tmp_path):
    from xui_fleet.config import set_vpn_name
    path = tmp_path / "fleet.yaml"
    path.write_text("master: {name: ru, host: 9.9.9.9}\nnodes: []\nusers: [{name: alice}]\n", encoding="utf-8")
    set_vpn_name(path, "  Семейный   VPN ")
    cfg = load_config(path)
    assert cfg.clients.vpn_name == "Семейный VPN"
    panel = FakePanel()
    panel._settings = {"subEnable": True, "subPort": 2096, "subPath": "/s/", "subCertFile": "/c", "webCertFile": "/c", "subTitle": "manual"}
    Reconciler(panel, cfg).run(_access([]))
    assert panel.settings()["subTitle"] == "Семейный VPN"
    set_vpn_name(path, "")
    assert load_config(path).clients.vpn_name == ""
    panel._settings["subTitle"] = "set in the panel"
    Reconciler(panel, load_config(path)).run(_access([]))
    assert panel.settings()["subTitle"] == "set in the panel"  # no name in the settings: the panel's own stays
    with pytest.raises(ValidationError):
        FleetConfig.model_validate({"master": {"name": "ru", "host": "9.9.9.9"}, "clients": {"vpn_name": "x" * 41}})


def test_warp_added_delay_is_half_the_handshake_difference():
    from xui_fleet import provision as pv

    class FakeSSH:
        def __init__(self, out):
            self.out, self.script = out, ""

        def run(self, script, check=True, timeout=0, env=None):
            self.script = script
            return type("R", (), {"out": self.out})()
    # through WARP: the first request pays for the tunnel handshake and is left out
    ssh = FakeSSH("w:204:0.900 w:204:0.100 w:204:0.104 w:204:0.098 d:204:0.040 d:204:0.044 d:204:0.042 d:204:0.050")
    assert pv.warp_added_ms(ssh, {"protocol": "wireguard", "settings": {"peers": [{}]}}, "188.114.96.1:2408") == 29
    assert '"endpoint": "188.114.96.1:2408"' in ssh.script and "socks5h://127.0.0.1:39995" in ssh.script
    assert pv.warp_added_ms(FakeSSH("w:000:0 w:000:0 d:204:0.04 d:204:0.04"), {"settings": {"peers": [{}]}}, "") is None
    assert pv.warp_added_ms(FakeSSH("w:204:0.5 w:204:0.01 w:204:0.01 d:204:0.05 d:204:0.05"), {"settings": {"peers": [{}]}}, "") == 0


def test_master_rtts_parse_the_connect_times(tmp_path):
    from xui_fleet.engine import Fleet
    from xui_fleet.state import State

    class FakeSSH:
        script = ""

        def run(self, script, check=True, timeout=0, env=None):
            FakeSSH.script = script
            return type("R", (), {"out": "uk ping 15.232\nde tcp 0.045 0.061 0.049\nfr tcp 0.000 0.000 0.000\n"})()
    fleet = Fleet.__new__(Fleet)
    fleet.cfg = FleetConfig.model_validate({"master": {"name": "ru", "host": "9.9.9.9"},
                                            "nodes": [{"name": "uk", "host": "198.51.100.1"}, {"name": "de", "host": "198.51.100.2"},
                                                      {"name": "fr", "host": "198.51.100.3"}]})
    fleet._master_ssh = FakeSSH()
    assert fleet._master_rtts() == {"uk": 15, "de": 49}  # ping where it answers, else TCP; unreachable: no figure
    assert "ping -c 4" in FakeSSH.script and "telnet://198.51.100.1:443" in FakeSSH.script
    fleet._master_ssh = None
    assert fleet._master_rtts() == {}


def test_ping_output_in_any_language():
    from xui_fleet.ui import parse_ping
    ru = "Ответ от 1.2.3.4: число байт=32 время=17мс TTL=56\r\nОтвет от 1.2.3.4: число байт=32 время<1мс TTL=56".encode("cp866")
    en = b"Reply from 1.2.3.4: bytes=32 time=17ms TTL=56\r\nReply from 1.2.3.4: bytes=32 time<1ms TTL=56"
    lin = b"64 bytes from 1.2.3.4: icmp_seq=1 ttl=56 time=15.2 ms\n64 bytes from 1.2.3.4: icmp_seq=2 ttl=56 time=15.4 ms"
    assert parse_ping(ru) == [17.0, 1.0] and parse_ping(en) == [17.0, 1.0] and parse_ping(lin) == [15.2, 15.4]
    assert parse_ping("Превышен интервал ожидания для запроса.".encode("cp866")) == []


def test_last_warp_check_is_read_back_while_fresh(tmp_path, monkeypatch):
    import time as _t
    from xui_fleet import history as hs
    from xui_fleet import service
    monkeypatch.setattr(service, "APP_DIR", tmp_path)
    h = hs.History(tmp_path / "fleet.yaml")
    assert h.last_warp() == {}
    now = int(_t.time())
    h.add_health({"t": now - 700, "n": {"uk": 15}, "m": 1, "w": {"uk": 120, "de": 90}})
    h.add_health({"t": now - 300, "n": {"uk": 15}, "m": 1, "w": {"uk": None}})
    h.add_health({"t": now - 10, "n": {"uk": 15}, "m": 1})  # this round had no WARP check
    assert h.last_warp() == {"at": now - 300, "nodes": {"uk": None}, "added": {}, "reality": {}}
    assert h.last_warp(max_age=60) == {}  # too old: unknown, not "down"


def test_history_takes_the_masters_ping_and_the_warp_check(tmp_path, monkeypatch):
    from xui_fleet import history as hs
    from xui_fleet import service
    monkeypatch.setattr(service, "APP_DIR", tmp_path)

    class FakeFleet:
        master_status = {"xray": {"state": "running"}}
        master_rtts = {"uk": 15}

        def status(self):
            return [{"name": "uk", "status": "online", "latencyMs": 40}, {"name": "de", "status": "online", "latencyMs": 44},
                    {"name": "fr", "status": "offline"}], []

        def probe_warp(self):
            self.warp_added = {"uk": 9}
            return {"uk": 120, "de": None}
    monkeypatch.setattr(hs, "TRAFFIC_EVERY", 10**11)  # only health and WARP are due
    h = hs.History(tmp_path / "fleet.yaml")
    import time as _t
    now = _t.time()
    assert hs.record(FakeFleet(), h, now=now) == ["health", "warp"]
    row = hs._read(h.dir / "health.jsonl", 0)[-1]
    assert row["n"] == {"uk": 15, "de": 44, "fr": None}  # ping where measured, the panel's figure otherwise
    w = hs._read(h.dir / "warp.jsonl", 0)[-1]
    assert w["w"] == {"uk": 120, "de": None} and w["wa"] == {"uk": 9}
    assert h.last_warp(max_age=10**11)["added"] == {"uk": 9}
    # health just taken, WARP never checked (e.g. history from an older version): WARP is due at once
    (h.dir / "warp.jsonl").unlink()
    assert hs.record(FakeFleet(), h, now=now + 30) == ["warp"]


def test_map_routes_come_from_the_real_entries():
    from xui_fleet.ui import real_routes
    nodes = [{"id": 1, "name": "uk"}, {"id": 2, "name": "de"}, {"id": 3, "name": "fr"}]
    inb = [
        {"tag": "fleet-rxh-uk", "enable": True}, {"tag": "fleet-xhttp-uk", "nodeId": 1, "enable": True},
        {"tag": "fleet-warp-uk", "nodeId": 1, "enable": True},
        # de: via RU switched off, direct hidden from subscriptions, its WARP only via RU
        {"tag": "fleet-rhy2-de", "enable": False}, {"tag": "fleet-direct-de", "nodeId": 2, "excludeFromSub": True},
        {"tag": "fleet-hy2-de", "nodeId": 2, "enable": True}, {"tag": "fleet-rwarp-de", "enable": True},
        {"tag": "fleet-warpup-de", "nodeId": 2, "enable": True},  # internal: not a way in
        # the master is a VPN itself
        {"tag": "fleet-direct-ru", "enable": True}, {"tag": "fleet-warp-ru", "enable": True},
        {"tag": "someone-elses", "enable": True},
    ]
    r = real_routes("ru", nodes, inb)
    assert r["nodes"]["uk"] == {"cascade": True, "direct": True, "warp": True}
    assert r["nodes"]["de"] == {"cascade": True, "direct": True, "warp": True}  # rwarp is a way via RU too
    assert r["nodes"]["fr"] == {"cascade": False, "direct": False, "warp": False}  # set up, nothing offered
    assert r["master"] == {"vpn": True, "warp": True}
    only_hidden = real_routes("ru", nodes[:1], [{"tag": "fleet-rxh-uk", "excludeFromSub": True}, {"tag": "fleet-hy2-uk", "nodeId": 1, "enable": False}])
    assert only_hidden["nodes"]["uk"] == {"cascade": False, "direct": False, "warp": False} and not only_hidden["master"]["vpn"]


def test_cascade_entry_names_follow_the_masters_name():
    """The master need not be in Russia: "через RU" only when it is called RU (the default label keeps the names
    users already have)."""
    assert xc.remark_cascade_xhttp("🇬🇧 UK") == "🇬🇧 UK · через RU (XHTTP)"
    assert xc.remark_cascade_xhttp("🇬🇧 UK", "NL") == "🇬🇧 UK · через NL (XHTTP)"
    assert xc.remark_cascade_warp("🇩🇪 Germany", "Дом") == "🇩🇪 Germany · через Дом → WARP"
    panel = FakePanel()
    cfg = FleetConfig.model_validate({"master": {"name": "hub", "host": "9.9.9.9", "label": "NL",
                                                 "tls": {"mode": "domain", "domain": "a.example"}},
                                      "nodes": [{"name": "de", "host": "de.example"}], "users": [{"name": "alice"}]})
    Reconciler(panel, cfg).run(_access(["de"]))
    names = [i["remark"] for i in panel.inbounds() if not i.get("nodeId")]
    assert names and all("через RU" not in r for r in names) and any("через NL" in r for r in names)


def test_warp_leg_is_the_ping_to_cloudflare(tmp_path, monkeypatch):
    from xui_fleet.engine import Fleet
    from xui_fleet.state import State

    class FakeSSH:
        def __init__(self, out):
            self.out = out

        def connect(self):
            return self

        def run(self, script, check=True, timeout=0, env=None):
            assert "ping -c 10" in script and "162.159.195.1" in script and ":2408" not in script
            return type("R", (), {"out": self.out})()

        def close(self):
            pass
    fleet = Fleet.__new__(Fleet)
    fleet.cfg = FleetConfig.model_validate({"master": {"name": "ru", "host": "9.9.9.9"},
                                            "nodes": [{"name": "uk", "host": "198.51.100.1"}, {"name": "de", "host": "198.51.100.2"},
                                                      {"name": "fr", "host": "198.51.100.3"}]})
    fleet.state = State(tmp_path / "state.json")
    fleet.state.update("uk", warp_endpoint={"endpoint": "162.159.195.1:2408"})
    fleet.state.update("de", warp_endpoint={"endpoint": "162.159.195.1:2408"})  # no ping reply there
    replies = {"uk": "5.02\n12.4\n1.95\n2.33\n30.5\n1.24\n3.86\n", "de": ""}  # spikes do not move the median (3.86 → 4)
    fleet.ssh = lambda node: FakeSSH(replies[node.name])
    assert fleet.warp_pings(list(fleet.cfg.nodes)) == {"uk": 4}  # fr: entry unknown; de: no reply → left out


def test_master_warp_entry_without_the_vpn_role():
    """master.warp: one entry on the master whose traffic leaves through WARP from the master itself — no other
    own entries; every user gets it; switching it off removes it (cascades stay)."""
    from xui_fleet.ui import real_routes
    panel = FakePanel()
    doc = {"master": {"name": "ru", "host": "9.9.9.9", "warp": True, "tls": {"mode": "domain", "domain": "v.example"}},
           "defaults": {"set": {"cascade_tcp": False, "warp": False, "cascade_warp": False}},
           "users": [{"name": "a"}], "nodes": [{"name": "de", "host": "de.example"}]}
    rep = Reconciler(panel, FleetConfig.model_validate(doc)).run(_access(["de", "ru"]))
    assert not [w for w in rep.warnings if "остался" in w]  # its own WARP entry is not a leftover of a node
    by_tag = {i["tag"]: i for i in panel.inbounds()}
    assert "fleet-warp-ru" in by_tag and "nodeId" not in by_tag["fleet-warp-ru"]  # on the master itself
    assert not {"fleet-direct-ru", "fleet-xhttp-ru", "fleet-hy2-ru"} & set(by_tag)  # not the whole VPN role
    assert "fleet-warp-de" not in by_tag  # the template has no WARP: nodes get none
    assert by_tag["fleet-warp-ru"]["id"] in panel._clients["a"]["inboundIds"]
    r = real_routes("ru", panel.nodes(), panel.inbounds())
    assert r["master"] == {"vpn": False, "warp": True}  # its WARP entry alone is not the VPN role
    assert Reconciler(panel, FleetConfig.model_validate(doc)).run(_stale(_access(["de", "ru"]))).changes == []
    doc["master"]["warp"] = False
    Reconciler(panel, FleetConfig.model_validate(doc)).run(_stale(_access(["de", "ru"])))
    tags = {i["tag"] for i in panel.inbounds()}
    assert "fleet-warp-ru" not in tags and "fleet-rxh-de" in tags


def test_master_warp_rule_and_cascade_rules_keep_one_order():
    """merge_warp puts the master's WARP rule right after the API rule; merge_template must leave it there,
    or the two keep swapping them and the master always looks out of sync («Исправить · 1»)."""
    from xui_fleet import migrate as mg
    tpl = {"outbounds": [{"tag": "direct"}, {"tag": "blocked"}],
           "routing": {"rules": [{"type": "field", "inboundTag": ["api"], "outboundTag": "api"},
                                 {"type": "field", "outboundTag": "blocked", "ip": ["geoip:private"]}]}}
    routes = {"fleet-rxh-de": {"tag": "fleet-out-de", "protocol": "vless"}}
    one = xc.merge_template(tpl, routes, ru_direct=True)
    two = mg.merge_warp(one, {"tag": "warp", "protocol": "wireguard", "settings": {"peers": [{"endpoint": "1.1.1.1:2408"}]}},
                        ["fleet-warp-ru"], "162.159.195.1:2408")
    assert xc.merge_template(two, routes, ru_direct=True) == two  # nothing to fix any more
    order = [r.get("outboundTag") for r in two["routing"]["rules"]]
    assert order[:2] == ["api", "warp"] and order.index("fleet-out-de") > 1
    assert mg.merge_warp(two, two["outbounds"][-1], ["fleet-warp-ru"], "162.159.195.1:2408") == two


def test_warp_entry_never_leads_out_through_russia_when_another_works():
    """From Moscow the fastest WARP entry lands in Cloudflare DME: sites then see a Russian address (YouTube
    bot check, Telegram media stalls). The fastest entry that leaves Cloudflare abroad wins."""
    from xui_fleet import provision as pv
    answers = {"188.114.96.1:2408": ("0.30", "AMS"), "162.159.195.1:2408": ("0.10", "DME"), "188.114.97.1:2408": ("0.25", "FRA"),
               "162.159.192.1:2408": ("9", "")}

    class FakeSSH:
        def run(self, script, check=False, timeout=0):
            ep = next(e for e in answers if e in script)
            t, colo = answers[ep]
            ok = "204" if t != "9" else "000"
            return type("R", (), {"out": f"{ok}:{t} {ok}:{t} {ok}:{t} " + (f"colo={colo}" if colo else "")})()
    best, scores, colos = pv.pick_warp_endpoint(FakeSSH(), {"settings": {"peers": [{}]}}, list(answers))
    assert best == "188.114.97.1:2408" and colos["162.159.195.1:2408"] == "DME" and scores["162.159.192.1:2408"] == 99.0
    # only Russian ones work: take the fastest of them rather than leave WARP without an entry
    only_ru = {"162.159.195.1:2408": ("0.10", "DME"), "188.114.98.1:2408": ("0.12", "LED")}
    answers.clear(); answers.update(only_ru)
    assert pv.pick_warp_endpoint(FakeSSH(), {"settings": {"peers": [{}]}}, list(only_ru))[0] == "162.159.195.1:2408"



def test_a_rejected_gate_config_never_takes_the_subscription_down(tmp_path, monkeypatch):
    """nginx refused the new config (too long a page): the previous one keeps serving, nothing is removed. Only if
    nginx does not answer at all does the subscription go back to 3x-ui on the public port — never to nobody."""
    import pytest
    panel = FakePanel()
    panel._settings = {"subEnable": True, "subPort": 2096, "subPath": "/s/", "subCertFile": "/c", "subKeyFile": "/k"}
    fleet, calls = _gate_fleet(tmp_path, ["happ", "incy"], monkeypatch)
    fleet.ensure_sub_gate(panel, ssh=None)  # behind nginx now
    assert panel.settings()["subListen"] == "127.0.0.1"
    calls.clear()
    fleet.fail["write"] = True
    with pytest.raises(RuntimeError):
        fleet.ensure_sub_gate(panel, ssh=None)
    assert ("remove",) not in calls and panel.settings()["subListen"] == "127.0.0.1"  # the working gate stays
    calls.clear()
    fleet.fail["nginx"] = True  # and now nginx does not answer even with the old config
    with pytest.raises(RuntimeError):
        fleet.ensure_sub_gate(panel, ssh=None)
    s = panel.settings()
    assert ("remove",) in calls and s["subListen"] == "" and int(s["subPort"]) == 2096  # 3x-ui answers itself again


def test_support_link_reaches_the_apps_the_invitation_and_the_page():
    from xui_fleet import provision as pv
    panel = FakePanel()
    panel._settings = {"subEnable": True, "subPort": 2096, "subPath": "/s/", "subCertFile": "/c", "webCertFile": "/c"}
    doc = {"master": {"name": "ru", "host": "9.9.9.9"}, "users": [{"name": "x"}], "nodes": [{"name": "de", "host": "de.example"}],
           "clients": {"support_url": "@family_help"}}
    cfg = FleetConfig.model_validate(doc)
    assert cfg.clients.support_url == "https://t.me/family_help"
    Reconciler(panel, cfg).run(_access(["de"]))
    assert panel.settings()["subSupportUrl"] == "https://t.me/family_help"  # → support-url, the «Support» button
    page = pv.unsupported_page(["happ"], title="VPN", support=cfg.clients.support_url)
    assert "href='https://t.me/family_help'" in page and '"' not in page
    import pytest
    with pytest.raises(Exception):
        FleetConfig.model_validate(dict(doc, clients={"support_url": "javascript:alert(1)"}))


def test_what_a_person_sees_when_the_subscription_ends_is_russian_and_leads_to_support():
    from xui_fleet.reconcile import STOCK_DEPLETED, STOCK_EXPIRED
    panel = FakePanel()
    panel._settings = {"subEnable": True, "subPort": 2096, "subPath": "/s/", "subCertFile": "/c", "webCertFile": "/c",
                       "subExpiredTemplate": STOCK_EXPIRED, "subTrafficDepletedTemplate": "мой текст", "subUpdates": 12,
                       "subHappNotificationExpire": False, "subHappSubExpireButtonLink": ""}
    doc = {"master": {"name": "ru", "host": "9.9.9.9"}, "users": [{"name": "x"}], "nodes": [{"name": "de", "host": "de.example"}],
           "clients": {"support_url": "@help_me"}}
    cfg = FleetConfig.model_validate(doc)
    Reconciler(panel, cfg).run(_access(["de"]))
    s = panel.settings()
    assert s["subExpiredTemplate"].startswith("⛔ Подписка закончилась")
    assert s["subTrafficDepletedTemplate"] == "мой текст"  # written by someone in the panel: left alone
    assert s["subHappNotificationExpire"] is True and s["subHappSubExpireButtonLink"] == "https://t.me/help_me"
    assert s["subUpdates"] == 4
    assert "subHostSortMode" not in s  # a panel without the setting is left alone
    assert Reconciler(panel, cfg).run(_stale(_access(["de"]))).changes == []  # and it stays that way



def test_with_main_and_reserve_addresses_the_main_entries_come_first():
    panel = FakePanel()
    panel.hosts = []
    panel._settings = {"subEnable": True, "subPort": 2096, "subPath": "/s/", "subCertFile": "/c", "webCertFile": "/c",
                       "subHostSortMode": "inbound-first"}
    doc = {"master": {"name": "ru", "host": "9.9.9.9", "tls": {"mode": "domain", "domain": "a.example", "reserve_domains": ["b.example"],
                                                               "address_mode": "both"}},
           "users": [{"name": "x"}], "nodes": [{"name": "de", "host": "de.example"}]}
    Reconciler(panel, FleetConfig.model_validate(doc)).run(_access(["de"]))
    assert panel.settings()["subHostSortMode"] == "hosts-first"
    doc["master"]["tls"]["address_mode"] = "main"
    Reconciler(panel, FleetConfig.model_validate(doc)).run(_stale(_access(["de"])))
    assert panel.settings()["subHostSortMode"] == "inbound-first"



def test_via_ru_hysteria2_reaches_the_node_over_its_own_hysteria2_entry():
    """The RU → node leg of «via RU · Hysteria2» must not depend on the node's Reality entry (whose borrowed site may
    die): it dials the node's Hysteria2 entry with the bridge's own auth; the Reality-based ways stay on Reality."""
    from xui_fleet import xrayconf as xc
    panel = FakePanel()
    access = _access(["de"])
    a = access["de"]
    a.busy_tcp, a.busy_udp = {443}, set()
    a.cert_pem, a.key_pem, a.cert_pin_hex = "-----BEGIN CERTIFICATE-----\nAAEC\n-----END CERTIFICATE-----", "K", "ab" * 32
    cfg = FleetConfig.model_validate({"master": {"name": "ru", "host": "9.9.9.9", "tls": {"mode": "domain", "domain": "v.example"}},
                                      "users": [{"name": "alice"}], "nodes": [{"name": "de", "host": "de.example"}]})
    leg = {"ok": False}
    panel.test_outbound = lambda o: {"success": leg["ok"]}
    Reconciler(panel, cfg).run(access)
    tags = {i["tag"]: i for i in panel.inbounds()}
    # the node's entry holds the bridge's password (the panel makes it when the bridge is attached)
    hy = next(i for i in panel._inbounds if i["tag"] == xc.hy2_tag("de"))
    hy["settings"]["clients"] = [{"email": cfg.clients.bridge_email, "auth": "BRIDGE-AUTH"}]
    rules = lambda: {tuple(r.get("inboundTag") or []): r["outboundTag"] for r in panel.template["routing"]["rules"] if r.get("inboundTag")}
    rep = Reconciler(panel, cfg).run(_stale(access))
    assert rules()[(xc.rhy2_tag("de"),)] == xc.out_tag("de")  # the leg does not pass (e.g. UDP 443 cut): Reality stays
    assert any("Hysteria2 от главного сервера до сервера не проходит" in w for w in rep.warnings)
    hy["enable"] = False  # switched off on the server: nothing to try, no warning about the network
    rep = Reconciler(panel, cfg).run(_stale(access))
    assert not any("Hysteria2 от главного" in w for w in rep.warnings)
    hy["enable"] = True
    leg["ok"] = True
    Reconciler(panel, cfg).run(_stale(access))
    assert rules()[(xc.rhy2_tag("de"),)] == xc.out_hy2_tag("de")
    assert rules()[(xc.rxh_tag("de"),)] == xc.out_tag("de")  # XHTTP via RU keeps the Reality leg
    out = next(o for o in panel.template["outbounds"] if o["tag"] == xc.out_hy2_tag("de"))
    bridge = panel._clients[cfg.clients.bridge_email]
    assert out["protocol"] == "hysteria" and out["streamSettings"]["hysteriaSettings"]["auth"] == "BRIDGE-AUTH"
    assert out["streamSettings"]["tlsSettings"]["pinnedPeerCertSha256"] == "ab" * 32
    assert tags[xc.hy2_tag("de")]["id"] in bridge["inboundIds"]
    assert int(out["settings"]["port"]) == int(tags[xc.hy2_tag("de")]["port"]) and out["settings"]["address"] == "de.example"
    assert Reconciler(panel, cfg).run(_stale(access)).changes == []  # and it stays put


def test_reality_failing_twice_in_a_row_asks_for_a_new_masking_site(tmp_path, monkeypatch):
    """The 5-minute check: one failed Reality check is noise; two in a row on the same server is a dead masking site
    (or a block) — the app then switches the site (heal_reality), at most once an hour."""
    import time as _t
    from xui_fleet import history as hs
    from xui_fleet import service
    monkeypatch.setattr(service, "APP_DIR", tmp_path)
    h = hs.History(tmp_path / "fleet.yaml")

    class F:
        reality, warp_added, master_status = {}, {}, {}

        def probe_warp(self):
            return {}

        def status(self):
            return [], []
    f = F()
    now = _t.time()
    h.add_health({"t": int(now + 400), "n": {}, "m": 1})  # health and traffic not due: only the WARP/Reality check runs
    h.add_traffic({"t": int(now + 400), "u": {}, "r": {}})
    f.reality = {"uk": None, "de": 40}
    assert not [d for d in hs.record(f, h, now=now) if d.startswith("reality-dead")]  # first failure: wait
    f.reality = {"uk": None, "de": None}
    assert [d for d in hs.record(f, h, now=now + 300) if d.startswith("reality-dead")] == ["reality-dead:uk"]  # de failed once
    assert h.last_warp()["reality"] == {"uk": None, "de": None}
    # WARP failing twice on a server whose Reality is fine asks for fix_warp; on one whose Reality is dead it does not
    f.reality = {"uk": None, "de": 40}
    f.probe_warp = lambda: {"uk": None, "de": None}
    hs.record(f, h, now=now + 600)
    got = hs.record(f, h, now=now + 900)
    assert [d for d in got if d.startswith("warp-dead")] == ["warp-dead:de"] and "reality-dead:uk" in got


def test_route_rtts_around_vpn_and_fake_answers(monkeypatch):
    """A VPN in TUN mode answers every TCP connection itself in 0–1 ms: figures from it are fake. The measurement
    goes through the physical interface (macOS); when an address that does not exist «answers», nothing is shown."""
    import types
    from xui_fleet import ui, ssh
    cfg = _cfg([{"name": "de", "host": "de.example"}])
    seen = []
    monkeypatch.setattr(ssh, "_physical_interface", lambda: "en0")
    monkeypatch.setattr(ui, "icmp_rtt", lambda host, count=3, iface=None: seen.append(iface) or (None if host == ui.FAKE_PROBE else 9))
    monkeypatch.setattr(ui, "tcp_rtt", lambda host, ports, tries=3, timeout=2.0, iface=None: None)
    got = ui.route_rtts(cfg)
    assert got["master"] == 9 and set(seen) == {"en0"} and not got.get("vpn")
    monkeypatch.setattr(ui, "tcp_rtt", lambda host, ports, tries=3, timeout=2.0, iface=None: 1)  # everything «answers»
    got = ui.route_rtts(cfg)
    assert got["vpn"] is True and got["master"] is None and all(v is None for v in got["nodes"].values())


def test_stock_remark_template_loses_the_account_name():
    from xui_fleet.reconcile import STOCK_REMARK
    panel = FakePanel()
    panel._settings = {"subEnable": True, "subPort": 2096, "subPath": "/s/", "remarkTemplate": STOCK_REMARK}
    cfg = _cfg([{"name": "de", "host": "de.example"}])
    Reconciler(panel, cfg).run(_access(["de"]))
    assert panel.settings()["remarkTemplate"] == "{{INBOUND}}|📊{{TRAFFIC_LEFT}}|⏳{{DAYS_LEFT}}D"
    panel._settings["remarkTemplate"] = "{{INBOUND}} · {{EMAIL}}"  # written by hand: left alone
    Reconciler(panel, cfg).run(_stale(_access(["de"])))
    assert panel.settings()["remarkTemplate"] == "{{INBOUND}} · {{EMAIL}}"


def test_dns_answer_parsing_for_the_check_around_a_vpn():
    import struct
    from xui_fleet.ui import parse_dns_a
    q = struct.pack(">HHHHHH", 7, 0x8180, 1, 2, 0, 0) + b"\x01a\x07example\x00" + struct.pack(">HH", 1, 1)
    cname = b"\xc0\x0c" + struct.pack(">HHIH", 5, 1, 60, 2) + b"\xc0\x0c"
    a = b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 60, 4) + bytes([77, 105, 146, 193])
    assert parse_dns_a(q + cname + a, 7) == "77.105.146.193"
    assert parse_dns_a(q + cname + a, 8) is None and parse_dns_a(b"\x00", 7) is None
