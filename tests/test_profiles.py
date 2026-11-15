"""Profiles and leaving them: separate sets of servers, and wiping one (or everything) off this computer."""
import json

import pytest

from xui_fleet import profiles as pf
from xui_fleet import service
from xui_fleet import sync as sy

MASTER = "version: 1\nmaster:\n  name: ru\n  host: {host}\nnodes: []\nusers: []\n"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """The program folder (with the default fleet.yaml) and the data folder, both empty."""
    program, data = tmp_path / "program", tmp_path / "data"
    program.mkdir()
    data.mkdir()
    monkeypatch.setattr(service, "APP_DIR", data)
    monkeypatch.delenv("XUI_FLEET_BACKUP_DIR", raising=False)
    return program, data


def _fill_default(program, data):
    """Everything an installation of an earlier version keeps about its servers."""
    (program / "fleet.yaml").write_text(MASTER.format(host="1.1.1.1"), encoding="utf-8")
    (program / "fleet.yaml.before-import-20260101-000000").write_text("old")
    (program / ".xui-fleet").mkdir()
    (program / ".xui-fleet" / "state.json").write_text('{"servers": {"ru": {"api_token": "SECRET"}}}')
    (program / "fleet.example.yaml").write_text("example")  # part of the program, not of the profile
    (data / "backups").mkdir()
    (data / "backups" / "master-20260101-000000.tar.gz").write_bytes(b"db")
    for name in pf.SYNC_FILES:
        (data / name).write_text("{}")
    (data / "ui-token").write_text("tok")
    (data / "update.json").write_text("{}")


def test_an_existing_installation_is_the_default_profile_and_nothing_moves(home):
    program, data = home
    _fill_default(program, data)
    p = pf.Profiles(program / "fleet.yaml")
    assert p.active_config() == program / "fleet.yaml"
    assert [(x["id"], x["name"], x["configured"], x["master"]) for x in p.list()] == [("default", "Основной", True, "1.1.1.1")]
    assert not p.file.exists()  # nothing written until the user creates a second profile
    assert service.backup_dir(program / "fleet.yaml") == data / "backups"
    assert sy.Sync(program / "fleet.yaml").path == data / "sync.json"


def test_a_new_profile_has_its_own_files_backups_and_sync(home):
    program, data = home
    _fill_default(program, data)
    p = pf.Profiles(program / "fleet.yaml")
    pid = p.create("Клиенты · Европа")
    cfg = p.active_config()
    assert cfg == program / "profiles" / pid / "fleet.yaml" and not cfg.exists()  # the first-run screen
    assert pf.profile_of(cfg) == pid
    assert service.backup_dir(cfg) == data / "profiles" / pid / "backups"
    assert sy.Sync(cfg).path == data / "profiles" / pid / "sync.json"
    with pytest.raises(pf.ProfileError):
        p.create("клиенты · европа")  # names are unique, case aside
    p.switch("default")
    assert p.active_config() == program / "fleet.yaml"
    p.rename(pid, "Европа")
    assert [x["name"] for x in p.list()] == ["Основной", "Европа"]


def test_leaving_the_default_profile_wipes_its_data_but_not_the_program_or_other_profiles(home):
    program, data = home
    _fill_default(program, data)
    p = pf.Profiles(program / "fleet.yaml")
    other = p.create("Второй")
    (p.config_path(other)).write_text(MASTER.format(host="2.2.2.2"), encoding="utf-8")
    (data / "profiles" / other / "backups").mkdir(parents=True)
    (data / "profiles" / other / "backups" / "master-x.tar.gz").write_bytes(b"db2")
    p.switch("default")

    assert p.remove("default") == other  # the remaining profile opens
    assert not (program / "fleet.yaml").exists()
    assert not list(program.glob("fleet.yaml.*"))
    assert not (program / ".xui-fleet").exists()
    assert not (data / "backups").exists()
    assert not any((data / n).exists() for n in pf.SYNC_FILES)
    # Kept: the program, its update settings, the address of the page, the other profile.
    assert (program / "fleet.example.yaml").exists()
    assert (data / "ui-token").exists() and (data / "update.json").exists()
    assert p.config_path(other).exists() and (data / "profiles" / other / "backups" / "master-x.tar.gz").exists()
    assert [x["id"] for x in p.list()] == [other]


def test_leaving_the_last_profile_brings_back_the_first_run_screen(home):
    program, data = home
    _fill_default(program, data)
    p = pf.Profiles(program / "fleet.yaml")
    pid = p.create("Второй")
    p.config_path(pid).write_text(MASTER.format(host="2.2.2.2"), encoding="utf-8")
    p.remove("default")
    assert p.remove(pid) == "default"
    assert not (program / "profiles" / pid).exists() and not (data / "profiles" / pid).exists()
    assert p.active_config() == program / "fleet.yaml" and not p.active_config().exists()
    assert [(x["id"], x["configured"]) for x in p.list()] == [("default", False)]


def test_a_custom_backup_folder_is_emptied_of_master_backups_only(home, tmp_path, monkeypatch):
    program, data = home
    _fill_default(program, data)
    custom = tmp_path / "my-backups"
    custom.mkdir()
    (custom / "master-1.tar.gz").write_bytes(b"db")
    (custom / "notes.txt").write_text("mine")
    monkeypatch.setenv("XUI_FLEET_BACKUP_DIR", str(custom))
    pf.Profiles(program / "fleet.yaml").remove("default")
    assert not (custom / "master-1.tar.gz").exists() and (custom / "notes.txt").exists()


def test_wipe_all_forgets_every_profile(home):
    program, data = home
    _fill_default(program, data)
    (data / "last-job.json").write_text('{"id": 1, "title": "Удаление сервера fr-1"}')
    (data / "xui-fleet.log").write_text("ru 1.1.1.1")
    p = pf.Profiles(program / "fleet.yaml")
    pid = p.create("Второй")
    p.config_path(pid).write_text(MASTER.format(host="2.2.2.2"), encoding="utf-8")
    (data / "profiles" / pid).mkdir(parents=True)
    (data / "profiles" / pid / "sync.json").write_text("{}")

    p.wipe_all()
    assert not (program / "fleet.yaml").exists() and not (program / "profiles").exists()
    assert not (data / "profiles").exists() and not (data / "backups").exists()
    assert not p.file.exists() and not (data / "last-job.json").exists()
    assert (data / "xui-fleet.log").read_text() == ""
    assert (data / "ui-token").exists() and (program / "fleet.example.yaml").exists()
    assert [(x["id"], x["configured"]) for x in p.list()] == [("default", False)]


def test_a_broken_or_foreign_registry_falls_back_to_the_default_profile(home):
    program, _data = home
    p = pf.Profiles(program / "fleet.yaml")
    p.file.write_text('{"active": "../../etc", "profiles": [{"id": "../../etc", "name": "x"}]}')
    assert p.active_config() == program / "fleet.yaml"
    p.file.write_text("not json")
    assert [x["id"] for x in p.list()] == ["default"]
    with pytest.raises(pf.ProfileError):
        p.switch("../../etc")


def test_the_command_line_follows_the_active_profile(home):
    program, data = home
    _fill_default(program, data)
    p = pf.Profiles(program / "fleet.yaml")
    assert pf.resolve(program / "fleet.yaml") == program / "fleet.yaml"
    pid = p.create("Второй")
    assert pf.resolve(program / "fleet.yaml") == p.config_path(pid)
    other = program / "elsewhere.yaml"
    other.write_text("x")
    assert pf.resolve(program / "profiles" / pid / "fleet.yaml") == program / "profiles" / pid / "fleet.yaml"


def test_the_interface_asks_for_the_profile_name_and_never_wipes_during_an_operation(home):
    from xui_fleet.ui import App
    program, data = home
    _fill_default(program, data)
    app = App(program / "fleet.yaml")
    assert app.overview()["profile"]["name"] == "Основной"

    with pytest.raises(ValueError):
        app.profile_change("remove", {"id": "default", "confirm": "основ"})
    assert (program / "fleet.yaml").exists()

    app.job.running = True
    with pytest.raises(RuntimeError):
        app.profile_change("remove", {"id": "default", "confirm": "Основной"})
    app.job.running = False

    res = app.profile_change("create", {"name": "Тест"})
    assert res["profile"]["name"] == "Тест" and app.overview()["configured"] is False
    app.profile_change("switch", {"id": "default"})
    res = app.profile_change("remove", {"id": "default", "confirm": "  основной "})
    assert res["profile"]["name"] == "Тест" and not (program / "fleet.yaml").exists()

    with pytest.raises(ValueError):
        app.profile_change("wipe", {"confirm": "да"})
    app.profile_change("wipe", {"confirm": "Стереть"})
    assert not (data / "last-job.json").exists()  # nothing about the servers is left behind
    view = app.overview()
    assert view["configured"] is False and [p["id"] for p in view["profiles"]] == ["default"]
    assert json.loads(json.dumps(view))  # the page gets plain JSON


def test_a_setting_that_fails_to_apply_is_not_kept(home, monkeypatch):
    """The app never shows a setting the servers do not have: fleet.yaml comes back when applying fails."""
    import threading
    import time
    import urllib.request
    from http.server import ThreadingHTTPServer
    from xui_fleet.ui import App, make_handler
    program, data = home
    yaml = ("version: 1\nmaster:\n  name: ru\n  host: 1.1.1.1\n  tls:\n    mode: domain\n    domain: a.example\n"
            "    reserve_domains: [b.example]\nnodes: []\nusers: []\n")
    (program / "fleet.yaml").write_text(yaml, encoding="utf-8")
    app = App(program / "fleet.yaml")

    class Broken:
        def apply(self, *a, **kw):
            assert "address_mode: both" in (program / "fleet.yaml").read_text()  # applied with the new setting
            raise RuntimeError("мастер недоступен")
    monkeypatch.setattr(app, "fleet", lambda log=None: Broken())
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/api/master/addresses", method="POST",
                                     data=b'{"mode": "both"}', headers={"X-Fleet-Token": app.token,
                                                                        "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
        for _ in range(100):
            if not app.job.running:
                break
            time.sleep(0.05)
    finally:
        server.shutdown()
    view = app.job.view()
    assert view["ok"] is False and any("вернул прежнюю" in line for line in view["lines"])
    assert (program / "fleet.yaml").read_text() == yaml
    assert view["age"] is not None and view["age"] >= 0  # the header knows how long ago it ended
