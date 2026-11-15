"""User statistics: traffic, servers in use, devices and IPs, as read from the master's 3x-ui."""
import json

import httpx
import pytest

from xui_fleet import stats
from xui_fleet.panel import Panel

MASTER_GUID, DE_GUID = "m-guid", "de-guid"


def _client(email, up=0, down=0, limit_dev=0, limit_ip=0, quota=0, enable=True):
    return {"email": email, "enable": enable, "totalGB": quota, "limitHwid": limit_dev, "limitIp": limit_ip,
            "traffic": {"up": up, "down": down, "lastOnline": 1_700_000_000_000, "lastSubFetch": 1_700_000_100_000}}


def test_servers_are_named_and_an_unknown_guid_is_the_master():
    names = stats.server_names([{"guid": DE_GUID, "name": "de-1"}], "ru")
    assert names[DE_GUID] == "de-1" and names[MASTER_GUID] == "ru"


def test_summary_counts_traffic_servers_ips_and_flags_users_over_their_limit():
    clients = [_client("anna", up=1, down=9, limit_dev=2), _client("boris", limit_ip=1), _client("fleet-bridge"),
               _client("vera", enable=False)]
    names = stats.server_names([{"guid": DE_GUID, "name": "de-1"}], "ru")
    online = {MASTER_GUID: ["anna"], DE_GUID: ["anna", "boris"]}
    ips = {MASTER_GUID: {"boris": [{"ip": "1.1.1.1", "timestamp": 1}]},
           DE_GUID: {"boris": [{"ip": "2.2.2.2", "timestamp": 2}, {"ip": "1.1.1.1", "timestamp": 3}]}}
    rows = stats.summarize(clients, names, online, ips, {"anna": 3, "boris": None, "vera": 0}, hidden=["fleet-bridge"])
    assert set(rows) == {"anna", "boris", "vera"}  # the service client is not a user
    a, b = rows["anna"], rows["boris"]
    assert (a["used"], a["servers"], a["devices"], a["over"]) == (10, ["ru", "de-1"], 3, True)  # 3 devices, limit 2
    assert (b["ips"], b["servers"], b["devices"], b["over"]) == (2, ["de-1"], None, True)  # same IP twice counts once
    assert rows["vera"]["enabled"] is False and rows["vera"]["over"] is False


def test_no_limit_is_never_over_and_unknown_device_count_is_not_zero():
    rows = stats.summarize([_client("a")], {}, {}, {}, {"a": None})
    assert rows["a"]["over"] is False and rows["a"]["devices"] is None


def test_device_view_names_the_app_and_marks_3x_ui_own_requests():
    phone = stats.device_view({"id": 7, "deviceOs": "Android", "osVersion": "14", "deviceModel": "SM-A505FM",
                               "userAgent": "Happ/4.8.0/Android/17921395329", "firstSeen": 1, "lastSeen": 2})
    assert (phone["app"], phone["model"], phone["service"]) == ("Happ 4.8.0", "SM-A505FM", False)
    assert stats.device_view({"id": 8, "userAgent": "3x-ui-outbound-sub/1.0"})["service"] is True


@pytest.mark.parametrize("value,ok", [(0, 0), ("3", 3), (None, 0), ("", 0), (-1, None), (1001, None), ("x", None)])
def test_limits_are_whole_numbers_from_0_to_1000(value, ok):
    if ok is None:
        with pytest.raises(ValueError):
            stats.validate_limit(value, "устройств")
    else:
        assert stats.validate_limit(value, "устройств") == ok


def test_panel_calls_for_one_user_keep_any_name_in_one_path_segment():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.raw_path.decode(), request.content.decode() or None))
        return httpx.Response(200, json={"success": True, "obj": []})

    panel = Panel("http://panel", "tok")
    panel._http = httpx.Client(base_url="http://panel/panel/api", transport=httpx.MockTransport(handler))
    panel.client_devices("Мама/phone?x")
    panel.forget_device("Мама-phone", 5)
    panel.update_client("a.b@c", {"limitHwid": 2, "limitIp": 0})
    assert seen[0][1] == "/panel/api/clients/hwids/%D0%9C%D0%B0%D0%BC%D0%B0%2Fphone%3Fx"
    assert seen[1][:2] == ("DELETE", "/panel/api/clients/hwids/%D0%9C%D0%B0%D0%BC%D0%B0-phone/5")
    assert seen[2][1] == "/panel/api/clients/update/a.b@c" and json.loads(seen[2][2]) == {"limitHwid": 2, "limitIp": 0}
