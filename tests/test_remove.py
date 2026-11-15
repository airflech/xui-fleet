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
    def master_panel(provision=True):
        yield panel
    monkeypatch.setattr(f, "master_panel", master_panel)
    monkeypatch.setattr(f, "auto_backup", lambda **kw: ssh_log.append(("backup", kw.get("reason"))))
    monkeypatch.setattr(f, "ssh", lambda server: FakeSSH(server.name, ssh_log, server.name in broken))
    for name in ("countries", "neighbors", "health"):
        monkeypatch.setattr(f, name, lambda: {})
    f.panel, f.ssh_log, f.broken = panel, ssh_log, broken
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
