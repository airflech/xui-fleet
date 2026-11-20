"""Editing a user end to end against a panel that behaves like 3x-ui 3.11 over HTTP.

3x-ui 3.11 replaces the whole client record with the body of /clients/update (a field left out becomes empty: the
user is switched off, the comment, the term and the group are gone), refuses a body without the email and is strict
about types («id» is the client's key as a string, WireGuard lists are lists). A partial update looked fine on the
old fake panel and silently broke every edit on the real one — these tests use the real rules.
"""
import contextlib
import json
from urllib.parse import unquote

import httpx
import pytest
import yaml

from xui_fleet import service
from xui_fleet.engine import Fleet
from xui_fleet.panel import Panel

# What 3x-ui 3.11 starts a field at when the update body leaves it out.
ZERO = {"subId": "", "flow": "", "security": "", "password": "", "auth": "", "secret": "", "limitIp": 0, "limitHwid": 0,
        "totalGB": 0, "expiryTime": 0, "enable": False, "tgId": 0, "group": "", "comment": "", "reset": 0,
        "allowedIPs": [], "privateKey": "", "publicKey": "", "preSharedKey": "", "keepAlive": 0, "forwardedPorts": []}


class Panel311:
    """The client part of the 3x-ui 3.11 API, with its record shape and its update rules."""

    def __init__(self):
        self.records = {}
        self.updates = []

    def add(self, email, **fields):
        rec = dict(ZERO, id=len(self.records) + 1, email=email, uuid=f"uuid-{email}", subId=f"sub-{email}", auth=f"auth-{email}",
                   enable=True, createdAt=1, updatedAt=1, reverse=None)
        rec.update(fields)
        rec["allowedIPs"], rec["forwardedPorts"] = "", ""  # the record shows these lists as strings
        self.records[email] = rec

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.path.split("/panel/api", 1)[1])
        ok = lambda obj=None: httpx.Response(200, json={"success": True, "obj": obj})  # noqa: E731
        fail = lambda msg: httpx.Response(200, json={"success": False, "msg": msg})  # noqa: E731
        if path.startswith("/clients/get/"):
            rec = self.records.get(path.rsplit("/", 1)[1])
            return ok({"client": rec, "inboundIds": [1, 2], "externalLinks": [], "usedTraffic": 0}) if rec else fail("not found")
        if path.startswith("/clients/update/"):
            email = path.rsplit("/", 1)[1]
            body = json.loads(request.content)
            self.updates.append(body)
            if not body.get("email"):
                return fail("Something went wrong (client email is required)")
            for field, kind in (("id", str), ("allowedIPs", list), ("forwardedPorts", list), ("enable", bool)):
                if field in body and not isinstance(body[field], kind):
                    return fail(f"Something went wrong (json: cannot unmarshal x into Go struct field .{field} of type {kind.__name__})")
            old = self.records[email]
            new = dict(ZERO, id=old["id"], createdAt=old["createdAt"], updatedAt=2, reverse=None)  # replaced, not merged
            new.update({k: v for k, v in body.items() if k in ZERO or k == "email"})
            new["uuid"] = body.get("id") or ""
            new["allowedIPs"], new["forwardedPorts"] = "", ""
            self.records[email] = new
            return ok()
        return fail(f"unexpected {request.method} {path}")

    def panel(self) -> Panel:
        p = Panel("https://127.0.0.1:1/base", "token")
        p._http = httpx.Client(base_url=p.base_url + "/panel/api", transport=httpx.MockTransport(self.handle))
        return p


@pytest.fixture
def real():
    fake = Panel311()
    fake.add("anna", comment="телефон мамы", group="Friends", limitIp=2, totalGB=5 * 1024 ** 3, expiryTime=1_900_000_000_000)
    return fake


def test_changing_one_field_keeps_the_rest_of_the_record(real):
    real.panel().update_client("anna", {"comment": "новый"})
    rec = real.records["anna"]
    assert rec["comment"] == "новый"
    assert (rec["enable"], rec["group"], rec["limitIp"], rec["totalGB"], rec["expiryTime"]) == (True, "Friends", 2, 5 * 1024 ** 3, 1_900_000_000_000)
    assert (rec["uuid"], rec["subId"], rec["auth"]) == ("uuid-anna", "sub-anna", "auth-anna")  # links keep working


def test_an_unknown_client_is_refused_with_a_clear_message(real):
    with pytest.raises(Exception, match="не найден"):
        real.panel().update_client("nobody", {"comment": "x"})


@pytest.fixture
def fleet(tmp_path, monkeypatch, real):
    """The «Save» button of the user card: Fleet.edit_user and set_user_limits against the 3.11 panel."""
    monkeypatch.setattr(service, "APP_DIR", tmp_path / "data")
    path = tmp_path / "fleet.yaml"
    path.write_text(yaml.safe_dump({"version": 1, "master": {"name": "ru", "host": "1.1.1.1"}, "nodes": [],
                                    "users": [{"name": "anna", "comment": "телефон мамы"}]}, allow_unicode=True), encoding="utf-8")
    f = Fleet(path)
    panel = real.panel()

    @contextlib.contextmanager
    def master_panel(provision=True, wait=90):
        yield panel
    monkeypatch.setattr(f, "master_panel", master_panel)
    monkeypatch.setattr(f, "backup_before", lambda *a, **k: None)
    return f


def test_saving_the_user_card_stores_everything_and_breaks_nothing(fleet, real):
    """The dialog saves settings, then limits — two updates; the second must not undo the first."""
    body = {"comment": "работа", "totalGb": 10, "expiry": 2_000_000_000_000, "enabled": True}
    fleet.edit_user("anna", body)
    fleet.set_user_limits("anna", 3, 4)
    rec = real.records["anna"]
    assert (rec["comment"], rec["totalGB"], rec["expiryTime"], rec["enable"]) == ("работа", 10 * 1024 ** 3, 2_000_000_000_000, True)
    assert (rec["limitHwid"], rec["limitIp"], rec["group"]) == (3, 4, "Friends")
    assert (rec["uuid"], rec["subId"], rec["auth"]) == ("uuid-anna", "sub-anna", "auth-anna")
    assert all(u.get("email") == "anna" for u in real.updates)


def test_renewing_and_switching_off_and_on(fleet, real):
    fleet.edit_user("anna", {"comment": "телефон мамы", "totalGb": 5, "expiry": 2_100_000_000_000, "enabled": True})
    assert real.records["anna"]["expiryTime"] == 2_100_000_000_000 and real.records["anna"]["enable"] is True
    fleet.edit_user("anna", {"comment": "телефон мамы", "totalGb": 5, "expiry": 2_100_000_000_000, "enabled": False})
    assert real.records["anna"]["enable"] is False
    fleet.edit_user("anna", {"comment": "телефон мамы", "totalGb": 5, "expiry": 2_100_000_000_000, "enabled": True})
    assert real.records["anna"]["enable"] is True and real.records["anna"]["group"] == "Friends"
