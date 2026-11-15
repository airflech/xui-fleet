"""The «Входящие» tab: which inbounds are xui-fleet's, and what editing or deleting one means in fleet.yaml."""
import pytest
import yaml

from xui_fleet import xrayconf as xc
from xui_fleet.config import FleetConfig, disable_connection, set_direct_port

DOC = {"version": 1, "master": {"name": "ru", "host": "1.1.1.1", "vpn": True},
       "nodes": [{"name": "de-1", "host": "2.2.2.2", "direct": {"port": 443}}, {"name": "nl", "host": "3.3.3.3"}],
       "users": []}


@pytest.mark.parametrize("tag,kind", [
    ("fleet-direct-de-1", ("direct", "de-1")), ("fleet-warpup-de-1", ("warpup", "de-1")), ("fleet-warp-de-1", ("warp", "de-1")),
    ("fleet-rhy2-nl", ("rhy2", "nl")), ("fleet-rxh-ru", ("rxh", "ru")), ("inbound-443", None), ("fleet-out-de", None), ("", None)])
def test_xui_fleet_entries_are_told_apart_by_their_tag(tag, kind):
    assert xc.inbound_kind(tag) == kind


def _write(tmp_path):
    path = tmp_path / "fleet.yaml"
    path.write_text(yaml.safe_dump(DOC, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def test_deleting_an_entry_turns_that_kind_off_for_that_server_only(tmp_path):
    path = _write(tmp_path)
    disable_connection(path, "de-1", "xhttp")
    disable_connection(path, "de-1", "rhy2")
    disable_connection(path, "ru", "warp")  # the master's own set
    disable_connection(path, "nl", "direct")  # hidden, not deleted: the cascades lead through it
    cfg = FleetConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    de, nl, master = cfg.node("de-1"), cfg.node("nl"), cfg.local_node()
    assert not cfg.set_for(de).xhttp and not cfg.set_for(de).cascade_hysteria2 and cfg.set_for(de).hysteria2
    assert cfg.set_for(nl).xhttp and not cfg.direct_for(nl).enabled
    assert not cfg.set_for(master).warp and cfg.set_for(master).xhttp
    with pytest.raises(ValueError):
        disable_connection(path, "de-1", "warpup")
    with pytest.raises(KeyError):
        disable_connection(path, "nope", "xhttp")


def test_a_port_written_in_fleet_yaml_follows_a_change_made_on_the_page(tmp_path):
    path = _write(tmp_path)
    assert set_direct_port(path, "de-1", 8443)
    assert not set_direct_port(path, "nl", 8443)  # no port in fleet.yaml: the live one is kept anyway
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert doc["nodes"][0]["direct"]["port"] == 8443 and "direct" not in doc["nodes"][1]
