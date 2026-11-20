"""«Сайты напрямую»: what the person types becomes rules the master's Xray and the apps read, a bad line is refused
before anything is written, and the list reaches both the master's rules and the subscription's routing profile."""
import pytest
import yaml

from xui_fleet import api, config
from xui_fleet import xrayconf as xc


def test_lines_are_written_the_way_xray_and_the_apps_read_them():
    got = config.normalize_direct(["steampowered.com", "https://store.steampowered.com/app/730", "*.valve.net",
                                   "full:a.com", "1.2.3.4", "10.0.0.0/8", "сайт.рф", "2001:db8::/32", "  ", "# заметка",
                                   "steampowered.com"])
    assert got == ["domain:steampowered.com", "domain:store.steampowered.com", "domain:valve.net", "full:a.com",
                   "1.2.3.4/32", "10.0.0.0/8", "domain:xn--80aswg.xn--p1ai", "2001:db8::/32"]


@pytest.mark.parametrize("bad", ["geosite:steam", "geoip:us", "привет", "a..b", "300.1.1.1/8"])
def test_what_could_stop_the_vpn_or_is_not_a_site_is_refused(bad):
    with pytest.raises(ValueError):
        config.normalize_direct([bad])


def test_a_category_written_into_fleet_yaml_by_hand_survives_a_save_from_the_page():
    assert config.normalize_direct(["geosite:apple", "x.com"], keep=["geosite:apple"]) == ["geosite:apple", "domain:x.com"]


def test_presets_are_valid_entries_and_go_to_the_right_rules():
    for p in config.DIRECT_PRESETS.values():
        assert config.normalize_direct(p["entries"]) == p["entries"]
    cs2 = config.DIRECT_PRESETS["cs2"]["entries"]
    domains, ips = xc.ru_direct_rules(["in-1"], cs2)
    assert "domain:counter-strike.net" in domains["domain"] and "155.133.224.0/21" in ips["ip"]
    assert "155.133.224.0/21" not in domains["domain"]
    prof = xc.ru_profile(cs2)
    assert "155.133.224.0/21" in prof["DirectIp"] and "domain:counter-strike.net" in prof["DirectSites"]


@pytest.fixture
def cfg_path(tmp_path):
    path = tmp_path / "fleet.yaml"
    path.write_text(yaml.safe_dump({"version": 1, "master": {"name": "ru", "host": "1.1.1.1"}, "nodes": [], "users": []}),
                    encoding="utf-8")
    return path


def test_the_list_is_saved_and_an_empty_one_removes_the_setting(cfg_path):
    config.set_extra_direct(cfg_path, ["steampowered.com"])
    assert config.load_config(cfg_path).routing.extra_direct == ["domain:steampowered.com"]
    config.set_extra_direct(cfg_path, [])
    assert "routing" not in yaml.safe_load(cfg_path.read_text(encoding="utf-8"))


class _App:
    def __init__(self, path):
        self.path = path
        self.started = []

        class job:
            running = False

            @staticmethod
            def start(title, fn, scope=""):
                self.started.append(title)
                return True
        self.job = job

    def fleet(self, log=None):
        from xui_fleet.engine import Fleet
        return Fleet(self.path)


def test_the_page_saves_through_apply_and_refuses_a_bad_line_before_writing(cfg_path):
    app = _App(cfg_path)
    before = cfg_path.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="geosite"):
        api.dispatch(app, "POST", "/api/routing/direct", {"entries": ["x.com", "geosite:steam"]})
    assert cfg_path.read_text(encoding="utf-8") == before and not app.started
    assert api.dispatch(app, "POST", "/api/routing/direct", {"entries": ["x.com", "1.2.3.0/24"]}) == {"ok": True}
    assert app.started == ["Сайты напрямую: 2 записи"]
    assert config.load_config(cfg_path).routing.extra_direct == ["domain:x.com", "1.2.3.0/24"]
    # the same set in another order: nothing to restart
    assert api.dispatch(app, "POST", "/api/routing/direct", {"entries": ["1.2.3.0/24", "x.com"]})["unchanged"] is True
    assert len(app.started) == 1
