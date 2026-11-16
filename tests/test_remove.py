"""Removing servers: from xui-fleet only, or completely together with 3x-ui; the master too."""
import contextlib

import pytest
import yaml

from test_core import FakePanel, _access, _cfg, _no_waiting, _update_settings  # noqa: F401  (fake master API, no sleeps)
from xui_fleet import provision as pv
from xui_fleet import profiles as pf
from xui_fleet import service
from xui_fleet.engine import Fleet
from xui_fleet.reconcile import Reconciler


class FakeSSH:
    def __init__(self, name, log, fail=False):
        self.name, self.log, self.fail = name, log, fail

    def connect(self):
        if self.fail:
            raise OSError("timed out")
        return self

    def run(self, script, check=True, env=None, timeout=900):
        self.log.append((self.name, dict(env or {})))
        return type("R", (), {"out": "3x-ui остановлен и убран из автозапуска\nготово\n"})()

    def close(self):
        pass


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    """A master with two nodes, reconciled on the fake panel; SSH and backups are recorded, not done."""
    monkeypatch.setattr(service, "APP_DIR", tmp_path / "data")
    cfg = _cfg([{"name": "de", "host": "de.example"}, {"name": "nl", "host": "nl.example",
                                                       "tls": {"mode": "domain", "domain": "nl.example.com"}}])
    path = tmp_path / "fleet.yaml"
    path.write_text(yaml.safe_dump(cfg.model_dump(mode="json", exclude_none=True), allow_unicode=True), encoding="utf-8")
    panel = FakePanel()
    Reconciler(panel, cfg).run(_access(["de", "nl"]))
    panel.delete_node = lambda node_id: panel._nodes.__setitem__(slice(None), [n for n in panel._nodes if n["id"] != node_id])
    f = Fleet(path)
    f.state.update("ru", panel_port=2053)
    f.state.update("de", panel_port=40001)
    ssh_log, broken = [], set()

    @contextlib.contextmanager
    def master_panel(provision=True, wait=90):
        if f.panel_dead:
            raise RuntimeError(f"панель не ответила за {wait}с: GET /server/status: EOF")
        f.waits.append(wait)
        yield panel
    monkeypatch.setattr(f, "master_panel", master_panel)
    monkeypatch.setattr(f, "auto_backup", lambda **kw: ssh_log.append(("backup", kw.get("reason"))))
    monkeypatch.setattr(f, "backup_master", lambda reason: ssh_log.append(("backup", reason)))
    monkeypatch.setattr(f, "ssh", lambda server: FakeSSH(server.name, ssh_log, server.name in broken))
    for name in ("countries", "neighbors", "health"):
        monkeypatch.setattr(f, name, lambda: {})
    f.panel, f.ssh_log, f.broken, f.panel_dead, f.waits = panel, ssh_log, broken, False, []
    return f


def test_removing_a_node_from_xui_fleet_only_never_touches_the_server(fleet):
    report = fleet.remove_node("de")
    assert [n["name"] for n in fleet.panel.nodes()] == ["nl"]
    assert [e for e in fleet.ssh_log if e[0] != "backup"] == []  # no SSH at all
    assert fleet.ssh_log[0] == ("backup", "перед удалением сервера de")
    assert not fleet.state.server("de") and not report.warnings


def test_removing_a_node_completely_uninstalls_only_on_that_node_with_its_own_ports(fleet):
    ports = sorted(i["port"] for i in fleet.panel.inbounds() if i.get("nodeId") == 1)
    report = fleet.remove_node("de", purge=True)
    calls = [e for e in fleet.ssh_log if e[0] != "backup"]
    assert [c[0] for c in calls] == ["de"]
    env = calls[0][1]
    assert sorted(map(int, env["FLEET_PORTS"].split())) == sorted(set(ports + [40001]))
    assert env["FLEET_SSH_PORT"] == "22" and env["FLEET_SYNC"] == "0" and env["FLEET_DOMAINS"] == ""
    assert "de: 3x-ui удалён с сервера" in report.changes


def test_an_unreachable_node_is_still_removed_from_the_master_and_the_failure_is_reported(fleet):
    fleet.broken.add("de")
    report = fleet.remove_node("de", purge=True)
    assert [n["name"] for n in fleet.panel.nodes()] == ["nl"]
    assert any("не удалось подключиться по SSH" in w for w in report.warnings)


def test_removing_the_master_completely_can_take_the_nodes_and_their_domains_with_it(fleet):
    report = fleet.remove_master(purge=True, purge_nodes=True, sync_data=True)
    calls = {e[0]: e[1] for e in fleet.ssh_log if e[0] != "backup"}
    assert list(calls) == ["de", "nl", "ru"]  # the master last: the nodes are reached while it still answers
    assert calls["nl"]["FLEET_DOMAINS"] == "nl.example.com"
    assert calls["ru"]["FLEET_SYNC"] == "1" and calls["de"]["FLEET_SYNC"] == "0"
    assert calls["ru"]["FLEET_DOMAINS"] == "9.9.9.9"  # the installer's certificate for the IP goes too
    master_ports = {i["port"] for i in fleet.panel.inbounds() if not i.get("nodeId")} | {2053}
    assert set(map(int, calls["ru"]["FLEET_PORTS"].split())) == master_ports
    assert len(report.changes) == 3


def test_removing_the_master_from_xui_fleet_only_leaves_every_server_running(fleet):
    fleet.remove_master(purge=False)
    assert [e for e in fleet.ssh_log if e[0] != "backup"] == []


def test_uninstall_never_closes_ssh_and_rejects_odd_domains():
    seen = []
    pv.uninstall(FakeSSH("x", seen), [22, 443, 443, 0, 70000, 2053], ["ok.example", "bad;rm -rf /", ""], 22)
    env = seen[0][1]
    assert env["FLEET_PORTS"] == "443 2053" and env["FLEET_DOMAINS"] == "ok.example"


def test_forgetting_the_network_keeps_the_master_backups(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(service, "APP_DIR", data)
    monkeypatch.delenv("XUI_FLEET_BACKUP_DIR", raising=False)
    program = tmp_path / "program"
    program.mkdir()
    p = pf.Profiles(program / "fleet.yaml")
    for pid in ("default", p.create("Тест")):
        cfg = p.config_path(pid)
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text("x")
        (cfg.parent / ".xui-fleet").mkdir()
        (cfg.parent / ".xui-fleet" / "state.json").write_text("{}")
        backups = service.backup_dir(cfg)
        backups.mkdir(parents=True)
        (backups / "master-1.tar.gz").write_bytes(b"db")
        (pf.data_dir(cfg) / "sync.json").write_text("{}")
        p.forget_network(pid)
        assert not cfg.exists() and not (cfg.parent / ".xui-fleet").exists()
        assert not (pf.data_dir(cfg) / "sync.json").exists()
        assert (backups / "master-1.tar.gz").exists()
    assert [x["id"] for x in p.list()] == ["default", p.list()[1]["id"]]  # both profiles stay


def test_inbounds_a_node_reports_back_during_removal_are_removed_again():
    panel = FakePanel()
    cfg = _cfg([{"name": "de", "host": "de.example"}])
    Reconciler(panel, cfg).run(_access(["de"]))
    returned = {"left": 1}

    def delete_node(node_id):
        # The node reports its direct entry back just before it is detached (seen on 3x-ui 3.11).
        panel._nodes[:] = [n for n in panel._nodes if n["id"] != node_id]
        if returned["left"]:
            returned["left"] -= 1
            panel._inbounds.append({"id": 99, "tag": "fleet-direct-de", "nodeId": node_id, "port": 443, "protocol": "vless"})
    panel.delete_node = delete_node
    rec = Reconciler(panel, cfg)
    rec.remove_node("de")
    assert [i["tag"] for i in panel.inbounds()] == [] or all(not i["tag"].endswith("-de") for i in panel.inbounds())
    assert not rec.report.warnings


def test_a_master_whose_panel_is_already_gone_is_removed_at_once_without_a_backup(fleet):
    fleet.panel_dead = True
    lines = []
    fleet.log = lines.append
    report = fleet.remove_master(purge=True, without_backup=True)  # the user agreed to go on without one
    assert ("backup", "перед удалением главного сервера") not in fleet.ssh_log  # nothing to back up
    assert [e[0] for e in fleet.ssh_log if e[0] != "backup"] == ["ru"]  # still cleaned over SSH
    assert any("не отвечает" in line and "без бэкапа" in line for line in lines)
    assert report.changes == ["ru: 3x-ui удалён с сервера"]


def test_the_master_panel_is_checked_briefly_before_a_long_wait(fleet):
    fleet.remove_master(purge=False)
    assert fleet.waits == [15] and ("backup", "перед удалением главного сервера") in fleet.ssh_log


def test_without_the_users_consent_nothing_is_removed_when_no_backup_can_be_made(fleet):
    from xui_fleet.engine import FleetError
    fleet.panel_dead = True
    with pytest.raises(FleetError, match="без бэкапа ничего не удаляю"):
        fleet.remove_master(purge=True)
    assert [e for e in fleet.ssh_log if e[0] != "backup"] == []  # the server was not touched


def test_a_failed_backup_on_a_live_panel_also_needs_consent(fleet, monkeypatch):
    from xui_fleet.engine import FleetError

    def broken(reason):
        raise RuntimeError("диск переполнен")
    monkeypatch.setattr(fleet, "backup_master", broken)
    with pytest.raises(FleetError, match="бэкап не удался"):
        fleet.remove_master(purge=True)
    assert fleet.remove_master(purge=True, without_backup=True).changes == ["ru: 3x-ui удалён с сервера"]


def test_the_page_can_ask_whether_the_panel_answers(fleet):
    assert fleet.master_probe()["alive"] is True
    fleet.panel_dead = True
    probe = fleet.master_probe()
    assert probe["alive"] is False and "не ответила" in probe["error"] and probe["lastBackup"] is None


def test_changing_the_masking_gives_one_server_new_keys_and_leaves_the_others(fleet, monkeypatch):
    import itertools
    counter = itertools.count()
    fleet.panel.new_x25519 = lambda: (lambda k: {"privateKey": f"PRIV{k}", "publicKey": f"PUB{k}"})(next(counter))
    monkeypatch.setattr(pv, "find_reality_neighbor", lambda ssh, exclude=(): ("10.0.0.7", "shop.example") if "10.0.0.5" in exclude else None)
    fleet.state.update("de", reality_neighbor={"ip": "10.0.0.5", "name": "old.example", "checked": 0})
    fleet.state.update("ru", api_token="t")  # a prepared master: backups are taken
    applied = []
    monkeypatch.setattr(fleet, "apply", lambda only=None, **kw: applied.append(only))

    def reality(tag):
        i = next(i for i in fleet.panel.inbounds() if i["tag"] == tag)
        rs = i["streamSettings"]["realitySettings"]
        return rs["privateKey"], rs["settings"]["publicKey"], rs["shortIds"][0]
    de_before, nl_before = reality("fleet-direct-de"), reality("fleet-direct-nl")
    msg = fleet.rotate_masking("de")
    de_after = reality("fleet-direct-de")
    assert de_after[0] != de_before[0] and de_after[1] != de_before[1] and de_after[2] != de_before[2]
    assert de_after[0].startswith("PRIV") and de_after[1] == "PUB" + de_after[0][4:]  # a matching pair
    assert reality("fleet-direct-nl") == nl_before  # other servers untouched
    assert fleet.state.get("de", "reality_neighbor")["name"] == "shop.example"  # a different site, not the old one
    assert applied == [{"de"}]  # the master's cascade picks up the new key, the node its new site
    assert ("backup", "перед сменой маскировки de") in fleet.ssh_log
    assert "обновить подписку" in msg and "shop.example" in msg


def test_a_warp_measurement_is_followed_by_an_xray_restart_even_when_the_entry_stays(fleet, monkeypatch):
    """Measuring entry points runs WARP with the server's own identity; Cloudflare then serves the test process and
    the running Xray stays cut off until it restarts — so it is restarted even when nothing in the template changed."""
    from xui_fleet import xrayconf as xc
    tag = xc.warp_tag(fleet.cfg.master.name)
    fleet.panel.add_inbound({"tag": tag, "port": 2083, "protocol": "hysteria", "settings": {"clients": []}, "streamSettings": {}})
    warp = {"tag": "warp", "protocol": "wireguard", "settings": {"secretKey": "k", "address": ["172.16.0.2/32"], "mtu": 1280,
                                                                   "peers": [{"publicKey": "p", "endpoint": "162.159.192.1:2408"}]}}
    fleet.panel.template.setdefault("outbounds", []).append(warp)
    fleet.panel.template.setdefault("routing", {}).setdefault("rules", []).append({"type": "field", "inboundTag": [tag], "outboundTag": "warp"})
    restarts = []
    fleet.panel.restart_xray = lambda: restarts.append(1)
    monkeypatch.setattr(pv, "pick_warp_endpoint", lambda ssh, w, candidates=None: ("162.159.192.1:2408", {"162.159.192.1:2408": 0.2}, {"162.159.192.1:2408": "ARN"}))
    monkeypatch.setattr(pv, "warp_added_ms", lambda ssh, w, ep: 12)
    fleet._master_ssh = object()
    fleet.ensure_master_warp(fleet.panel)
    assert fleet.state.get(fleet.cfg.master.name, "warp_endpoint")["colo"] == "ARN"
    assert restarts  # measured → restarted, though the entry is the same
    restarts.clear()
    fleet.ensure_master_warp(fleet.panel)  # the next time within a day: nothing measured, nothing restarted
    assert restarts == []
